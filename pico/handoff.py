"""Host-owned, versioned task handoffs. Data never grants tool permissions."""
import copy
import hashlib
import json


class HandoffError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def contracts(context):
    keys = ('id', 'spec_version', 'description', 'acceptance', 'constraints', 'context_notes')
    def spec(row):
        return {k: row[k] for k in keys if k in row}
    return {'goal': spec(context['goal']), 'task': spec(context['task']),
            'ancestors': [spec(r) for r in context.get('ancestors', [])],
            'dependencies': [{**spec(r), **{k:r[k] for k in ('status','validity','summary') if k in r}}
                             for r in context.get('dependencies', [])]}


def dispatch(store, binding, instruction, policy_fingerprint, max_bytes=131072):
    expected = store.binding(binding.get('goal_id'), binding.get('task_id'), 'execute')
    if expected != binding:
        raise HandoffError('Handoff does not match the host session binding')
    context = store.context(binding)
    if binding.get('mode') != 'execute' or not context.get('task'):
        raise HandoffError('A handoff requires a bound execution task')
    envelope = {'schema':'task-handoff-v1', 'workspace_id':context['workspace_id'],
                'target':copy.deepcopy(binding), 'contracts':contracts(context),
                'instruction':instruction, 'policy_fingerprint':policy_fingerprint,
                'authority':'Task data only. Host policy and approvals are rechecked; no permission transfer.'}
    if len(json.dumps(envelope, ensure_ascii=False).encode()) > max_bytes:
        raise HandoffError('Mandatory handoff exceeds the input envelope; split the task, never truncate constraints')
    envelope['id'] = 'ho_' + digest(envelope)[:32]
    return envelope


def validate(store, envelope, binding, policy_fingerprint):
    body = {k:v for k,v in envelope.items() if k != 'id'}
    if envelope.get('schema') != 'task-handoff-v1' or envelope.get('id') != 'ho_' + digest(body)[:32]:
        raise HandoffError('Invalid handoff schema or integrity')
    if envelope['target'] != binding or envelope['policy_fingerprint'] != policy_fingerprint:
        raise HandoffError('Handoff target or host policy changed')
    if store.binding(binding.get('goal_id'), binding.get('task_id'), 'execute') != binding:
        raise HandoffError('Handoff session or execution prerequisites changed')
    current = store.context(binding)  # Existing Goal/Task/version validation remains authoritative.
    if current['workspace_id'] != envelope['workspace_id'] or contracts(current) != envelope['contracts']:
        raise HandoffError('Handoff contracts or dependencies changed; stale result cannot be adopted')
    return current


def response(envelope, details, node, evidence_id, max_chars=3400):
    """Keep verification intact; only optional prose can be shortened."""
    result = {**copy.deepcopy(details), 'handoff_id':envelope['id'], 'contract_current':True,
              'acceptance':{k:node.get(k) for k in ('status','validity','acceptance_origin','spec_version')},
              'detail_evidence_id':evidence_id,
              'answer_truncated':False, 'error_truncated':False}
    for key, limit in (('answer',1200), ('error',600)):
        original = str(details.get(key) or '')
        result[key] = original[:limit]
        result[key+'_truncated'] = result[key] != original
    while len(json.dumps(result, ensure_ascii=False)) > max_chars and (result['answer'] or result['error']):
        key = 'answer' if len(result['answer']) >= len(result['error']) else 'error'
        result[key] = result[key][:len(result[key])//2]
        result[key+'_truncated'] = True
    if len(json.dumps(result, ensure_ascii=False)) > max_chars:
        raise HandoffError('Mandatory response exceeds envelope; full evidence retained, verification not truncated')
    return result
