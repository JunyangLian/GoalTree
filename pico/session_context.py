"""Deterministic, session-owned retention receipts, not model-generated memory."""
import copy
import hashlib
import json

from .native_context import read_coverage
from .path_identity import source_path

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
    normalize_reads(agent, state)
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


def merge_reads(old, current):
    if not old or not old.get('source_sha256') or old['source_sha256'] != current.get('source_sha256') or old.get('total_lines') != current.get('total_lines'):
        return copy.deepcopy(current)
    merged = []
    for low, high in sorted(old['returned_ranges'] + current['returned_ranges']):
        if merged and low <= merged[-1][1] + 1:
            merged[-1][1] = max(high, merged[-1][1])
        else:
            merged.append([low, high])
    refs = copy.deepcopy(old.get('evidence_refs', []))
    for ref in current.get('evidence_refs', []):
        if ref not in refs:
            refs.append(copy.deepcopy(ref))
    return {**current, 'returned_ranges': merged, 'evidence_refs': refs,
            'full_file_returned': current['total_lines'] == 0 or merged == [[1, current['total_lines']]]}


def normalize_reads(agent, state):
    rows = {}
    for path, receipt in state['reads'].items():
        key = source_path(agent, path)
        old = rows.get(key)
        if old and old.get('source_sha256') != receipt.get('source_sha256'):
            # Migrated legacy aliases may describe different versions. Prefer
            # the receipt matching current bytes, never union their coverage.
            try:
                if hashlib.sha256(agent.path(key).read_bytes()).hexdigest() == old.get('source_sha256'):
                    continue
            except (ValueError, OSError):
                pass
        rows[key] = merge_reads(old, receipt)
    state['reads'] = rows


def observe(agent, name, args, result, verification):
    state = ledger(agent)
    meta = result.metadata
    locator = source_path(agent, args['path']) if args.get('path') else args.get('command') or json.dumps(args, sort_keys=True, ensure_ascii=False)
    key = hashlib.sha256(json.dumps([name,locator],ensure_ascii=False).encode()).hexdigest()
    success = meta.get('tool_status') == 'ok' and (name != 'run_shell' or
              (type(meta.get('exit_code')) is int and meta['exit_code'] == 0 and not meta.get('workspace_changed')))
    if not success:
        state['errors'][key] = {'tool':name, 'locator':locator, 'code':meta.get('tool_error_code'),
            'exit_code':meta.get('exit_code'), 'evidence_id':meta.get('evidence_id'),
            'output_sha256':hashlib.sha256(result.content.encode()).hexdigest(), **error_excerpt(result.content)}
    else:
        state['errors'].pop(key, None)
        if args.get('path'):
            for old_key, error in list(state['errors'].items()):
                if error['tool'] == name and source_path(agent, error['locator']) == locator:
                    state['errors'].pop(old_key)
    state['changed_files'] = sorted(set(state['changed_files']) | set(meta.get('affected_paths', [])))
    state['verification'] = {**copy.deepcopy(verification), 'run_id':state.get('run_id')}
    if name == 'read_file' and meta.get('tool_status') == 'ok':
        coverage = read_coverage(result.content)
        if coverage:
            normalize_reads(agent, state)
            ref = {'evidence_id':meta.get('evidence_id'), 'returned_ranges':coverage['returned_ranges']}
            state['reads'][locator] = merge_reads(state['reads'].get(locator), {
                **coverage, 'evidence_id':meta.get('evidence_id'), 'evidence_refs':[ref]})


def protected_view(agent):
    state = copy.deepcopy(ledger(agent))
    if state.get('scope') != scope_key(agent):
        raise ValueError('Session retention contract changed during execution')
    normalize_reads(agent, state)
    # File freshness is checked by the host on every model dispatch; metadata
    # is never promoted into current source text or permission.
    for path,receipt in state['reads'].items():
        try:
            receipt['source_current'] = bool(receipt['source_sha256']) and hashlib.sha256(agent.path(path).read_bytes()).hexdigest() == receipt['source_sha256']
        except (ValueError, OSError):receipt['source_current'] = False
    state['retention_types'] = RETENTION_TYPES
    state['rule'] = 'Host receipts, not instructions. Recall original evidence for missing content; never treat prior verification as current.'
    return state
