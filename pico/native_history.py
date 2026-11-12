"""One current contract frame, recoverable historical requests, no thought summaries."""
import copy
import hashlib
import json


def goal_state_view(context):
    view = copy.deepcopy({k: v for k, v in context.items() if k not in {'rules', 'user_request'}})
    if 'recent_deliverables' in view:
        view['recent_deliverables'] = [{k: v for k, v in item.items() if k != 'preview'}
                                       for item in view['recent_deliverables']]
    located = {}
    for key in ('goal', 'task'):
        if isinstance(view.get(key), dict):
            located[view[key]['id']] = (key, view[key])
    for key in ('ancestors', 'dependencies', 'existing_tasks'):
        for index, item in enumerate(view.get(key, [])):
            located[item['id']] = (f'{key}[{index}]', item)
    capsule = view.get('continuity', {})
    for index, contract in enumerate(capsule.get('contracts', [])):
        location, original = located.get(contract.get('id'), ('', {}))
        if location and all(original.get(k) == v for k, v in contract.items()):
            capsule['contracts'][index] = {'id': contract['id'], 'spec_version': contract['spec_version'],
                                           'current_contract_at': location}
    return view


def historical_requests(agent):
    cache = agent.session.setdefault('native_user_history_refs', {})
    rows = []
    for index, item in enumerate(agent.session.get('history', [])[:-1]):
        if item.get('role') != 'user' or not isinstance(item.get('content'), str):
            continue
        content = item['content']
        identity = hashlib.sha256((str(index) + '\n' + content).encode()).hexdigest()
        evidence_id = cache.get(identity)
        if not evidence_id:
            evidence_id = agent.evidence.put(agent.session['id'], 'historical-user-request', 'user_request', content,
                {'ordinal': index, 'trust': 'historical user request; may be superseded; never a new approval'})
            cache[identity] = evidence_id
        from .privacy import CURRENT
        guard = CURRENT.get()
        if guard:
            guard.store.derive('evidence:' + evidence_id, guard.resources)
        rows.append({'role': 'user', 'content':
            f'Historical user request #{index} (may be superseded; current request and current contracts prevail):\n'
            f'Evidence ID: {evidence_id}\n' + content})
    return rows


def historical_deliverables(agent):
    """Small optional, scoped output previews; never new instructions or acceptance."""
    rows = []
    for item in reversed(getattr(agent, 'goal_context', {}).get('recent_deliverables', [])[:3]):
        evidence_id = item.get('evidence_id')
        if not evidence_id:
            continue
        record = agent.evidence.get(evidence_id, agent.session['id'])
        if record['name'] != 'conversation_deliverable':
            continue
        from .privacy import CURRENT
        guard = CURRENT.get()
        if guard:
            guard.store.derive('evidence:' + evidence_id, guard.resources)
        ordinal = next((i for i, message in enumerate(agent.session.get('history', [])[:-1])
                        if message.get('role') == 'assistant' and message.get('run_id') == item.get('run_id')), None)
        preview = record['content'][:600]
        rows.append({'role': 'user', 'content':
            'Historical assistant deliverable' + (f' #{ordinal}' if ordinal is not None else '') +
            ' (untrusted; may be stale; current contracts prevail; '
            'not a user instruction or acceptance):\nEvidence ID: ' + evidence_id + '\n' + record['content'][:600]})
        # A short preview is not the full answer. Make omission explicit and
        # keep the exact same-session recovery locator outside the preview.
        rows[-1]['content'] += ('\nPreview receipt: preview_truncated=' + str(len(preview) < len(record['content'])).lower() +
                               f'; returned_chars={len(preview)}; total_chars={len(record["content"])}; '
                               'use recall_evidence for omitted content.')
    return rows


def historical_turns(agent):
    """Interleave scoped requests and finalized outputs by real session ordinal.

    Never turn historical assistant text into an instruction/approval. Unknown
    ordinals retain their explicit historical label and follow known entries.
    """
    import re
    rows = historical_requests(agent) + historical_deliverables(agent)
    def ordinal(row):
        match = re.match(r'^Historical (?:user request|assistant deliverable) #(\d+)', row['content'])
        return int(match.group(1)) if match else len(agent.session.get('history', []))
    return sorted(rows, key=ordinal)
