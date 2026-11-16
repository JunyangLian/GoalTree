"""Task output requirements: quoted data, never verification or authority."""
import copy
import hashlib

KINDS = {'artifact', 'evidence', 'execution'}
FIELDS = {'title', 'description', 'acceptance'}


def normalize(value):
    """Stored/operator representation; model proposals use prepare()."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'kind', 'format', 'required_in', 'provenance'}:
        raise ValueError('Invalid output_contract fields')
    kind = value.get('kind')
    form = value.get('format', '')
    fields = value.get('required_in', [])
    if not isinstance(kind, str) or kind not in KINDS or not isinstance(form, str) or len(form) > 160 or '\n' in form or '\r' in form:
        raise ValueError('output_contract needs kind artifact/evidence/execution and bounded single-line format')
    if (not isinstance(fields, list) or any(not isinstance(f, str) or f not in FIELDS for f in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError('required_in contains unique title/description/acceptance fields only')
    if kind != 'artifact' and (form or fields):
        raise ValueError('Only artifact contracts declare delivery format/required_in')
    if fields and not form:
        raise ValueError('required_in needs an explicit format label')
    provenance = value.get('provenance')
    if provenance is not None:
        if (not isinstance(provenance, dict) or set(provenance) != {'origin', 'source_id', 'sha256', 'quote'}
                or provenance['origin'] not in {'user_request', 'read_evidence'}
                or any(not isinstance(v, str) for v in provenance.values())
                or len(provenance['source_id']) > 160 or len(provenance['quote']) > 2000
                or len(provenance['sha256']) != 64 or any(c not in '0123456789abcdef' for c in provenance['sha256'])):
            raise ValueError('Invalid host output-contract provenance')
    return {'kind': kind, 'format': form, 'required_in': list(fields), 'provenance': copy.deepcopy(provenance)}


def check_fields(node):
    value = normalize(node.get('output_contract'))
    if not value:
        return
    missing = [name for name in value['required_in'] if value['format'] not in node.get(name, '')]
    if missing:
        raise ValueError('Committed output format ' + repr(value['format']) + ' must appear literally in ' +
                         ', '.join(missing) + '. Repair the requested fields; do not silently rename or discard the contract.')


def prepare(agent, value, previous=None):
    """Validate a model declaration against actual same-session input/evidence.

    This proves a literal label was observed, not that a document is correct or
    that a file is a trusted instruction/permission source.
    """
    if value is None:
        if previous and previous.get('kind') == 'artifact':
            raise ValueError('An agent cannot erase an artifact contract; archive removed scope through existing controls')
        return None
    if not isinstance(value, dict) or set(value) - {'kind', 'format', 'required_in', 'source', 'quote'}:
        raise ValueError('Provide output_contract kind, format, required_in, source and quote; host provenance cannot be forged/copied')
    result = normalize({k: v for k, v in value.items() if k not in {'source', 'quote'}})
    if previous and previous.get('kind') == 'artifact' and result['kind'] != 'artifact':
        raise ValueError('Keep the committed artifact contract; scope removal uses existing archive controls')
    if previous and set(previous.get('required_in', [])) - set(result['required_in']):
        raise ValueError('Do not silently weaken committed required_in fields')
    if not result['format']:
        if previous and previous.get('format'):
            raise ValueError('An existing explicit format cannot be silently cleared')
        return result
    source, quote = value.get('source'), value.get('quote')
    if not isinstance(source, str) or not isinstance(quote, str) or not 1 <= len(quote) <= 2000 or result['format'] not in quote:
        raise ValueError('An explicit output format needs source=request or a read_file Evidence ID and an exact quote containing its literal label')
    if source == 'request':
        state = getattr(agent, 'current_task_state', None)
        text = getattr(state, 'user_request', '')
        source_id = getattr(state, 'run_id', '')
        sha = hashlib.sha256(text.encode('utf-8')).hexdigest()
        origin = 'user_request'
    else:
        row = agent.evidence.get(source, agent.session['id'])
        if row['name'] != 'read_file' or row['metadata'].get('tool_status') != 'ok':
            raise ValueError('An output requirement needs actual read_file evidence, not an agent summary or failed read')
        metadata = row['metadata']
        path, sha = metadata.get('args', {}).get('path'), metadata.get('source_sha256')
        if not path or not sha or hashlib.sha256(agent.path(path).read_bytes()).hexdigest() != sha:
            raise ValueError('The requirement source changed or has no verified source hash; read its current contents first')
        text, source_id, origin = row['content'], source, 'read_evidence'
    if not source_id or quote not in text:
        raise ValueError('Output contract quote is not in the actual current request/read evidence; preserve the literal user label')
    result['provenance'] = {'origin': origin, 'source_id': source_id, 'sha256': sha, 'quote': quote}
    return result


def prepare_step(agent, proposal, previous=None):
    result = copy.deepcopy(proposal)
    old = previous or {}
    if 'output_contract' in proposal:
        result['output_contract'] = prepare(agent, proposal['output_contract'], old.get('output_contract'))
    check_fields({**old, **result})
    return result
