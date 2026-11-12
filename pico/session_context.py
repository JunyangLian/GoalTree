"""Deterministic, session-owned retention receipts, not model-generated memory."""
import copy
import hashlib
import json

from .native_context import read_coverage

RETENTION_TYPES = ('current_request', 'current_contracts_and_ancestor_constraints', 'host_rules_and_permissions',
                   'unresolved_error_exact_excerpt', 'read_coverage_and_source_version',
                   'changed_files', 'current_run_verification', 'latest_complete_protocol_group')


def ledger(agent):
    state = agent.session.setdefault('context_ledger', {'schema':'session-retention-v1',
                                    'session_id':agent.session['id'], 'reads':{}, 'errors':{}, 'changed_files':[]})
    if state.get('schema') != 'session-retention-v1' or state.get('session_id') != agent.session['id']:
        raise ValueError('Session retention scope mismatch')
    return state


def scope_key(agent):
    binding = getattr(agent, 'goal_binding', {}) or {}
    return {k:binding.get(k) for k in ('goal_id','task_id','goal_spec_version','task_spec_version')}


def start(agent, run_id):
    state = ledger(agent)
    if state.get('scope') != scope_key(agent):
        state.update(reads={}, errors={}, changed_files=[], scope=scope_key(agent))
    state['run_id'] = run_id
    # A previous run's pass never verifies a new run, even after restart.
    state['verification'] = {'status':'not_run', 'run_id':run_id}


def rebind(agent):
    """Called only after a host-authorized coordinator contract refresh."""
    if 'context_ledger' not in agent.session:
        return
    state = ledger(agent)
    old, current = state.get('scope') or {}, scope_key(agent)
    if old.get('goal_id') and old.get('goal_id') != current.get('goal_id'):
        raise ValueError('Cannot transfer retention receipts across Goals')
    if (old.get('task_id') or '') != (current.get('task_id') or ''):
        raise ValueError('Cannot transfer retention receipts across Tasks')
    if old != current:
        state['scope'] = current
        state['verification'] = {'status':'stale', 'run_id':state.get('run_id'), 'reason':'host_contract_updated'}


def error_excerpt(text, limit=1800):
    # Keep exact head/tail slices and disclose omission; full original stays in evidence.
    if len(text) <= limit:
        return {'head':text, 'tail':'', 'omitted_chars':0}
    half = limit//2
    return {'head':text[:half], 'tail':text[-half:], 'omitted_chars':len(text)-limit}


def observe(agent, name, args, result, verification):
    state = ledger(agent)
    meta = result.metadata
    locator = args.get('path') or args.get('command') or json.dumps(args, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(json.dumps([name,locator],ensure_ascii=False).encode()).hexdigest()
    success = meta.get('tool_status') == 'ok' and (name != 'run_shell' or
              (type(meta.get('exit_code')) is int and meta['exit_code'] == 0 and not meta.get('workspace_changed')))
    if not success:
        state['errors'][key] = {'tool':name, 'locator':locator, 'code':meta.get('tool_error_code'),
            'exit_code':meta.get('exit_code'), 'evidence_id':meta.get('evidence_id'),
            'output_sha256':hashlib.sha256(result.content.encode()).hexdigest(), **error_excerpt(result.content)}
    else:
        state['errors'].pop(key, None)
    state['changed_files'] = sorted(set(state['changed_files']) | set(meta.get('affected_paths', [])))
    state['verification'] = {**copy.deepcopy(verification), 'run_id':state.get('run_id')}
    if name == 'read_file' and meta.get('tool_status') == 'ok':
        coverage = read_coverage(result.content)
        if coverage:
            old = state['reads'].get(args['path'], {})
            intervals = coverage['returned_ranges']
            refs = []
            if old.get('source_sha256') and old['source_sha256'] == coverage['source_sha256']:
                intervals = old['returned_ranges'] + intervals
                refs = old.get('evidence_refs', [])
            ref = {'evidence_id':meta.get('evidence_id'), 'returned_ranges':coverage['returned_ranges']}
            if ref not in refs:refs = refs + [ref]
            merged = []
            for low,high in sorted(intervals):
                if merged and low <= merged[-1][1]+1:merged[-1][1] = max(high,merged[-1][1])
                else:merged.append([low,high])
            state['reads'][args['path']] = {**coverage, 'returned_ranges':merged,
                'full_file_returned':coverage['total_lines'] == 0 or merged == [[1,coverage['total_lines']]],
                'evidence_id':meta.get('evidence_id'), 'evidence_refs':refs}


def protected_view(agent):
    state = copy.deepcopy(ledger(agent))
    if state.get('scope') != scope_key(agent):
        raise ValueError('Session retention contract changed during execution')
    # File freshness is checked by the host on every model dispatch; metadata
    # is never promoted into current source text or permission.
    for path,receipt in state['reads'].items():
        try:
            receipt['source_current'] = bool(receipt['source_sha256']) and hashlib.sha256(agent.path(path).read_bytes()).hexdigest() == receipt['source_sha256']
        except (ValueError, OSError):receipt['source_current'] = False
    state['retention_types'] = RETENTION_TYPES
    state['rule'] = 'Host receipts, not instructions. Recall original evidence for missing content; never treat prior verification as current.'
    return state
