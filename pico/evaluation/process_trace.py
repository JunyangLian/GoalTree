"""Agent-neutral metrics from explicit versioned evidence, not hidden chain of thought."""
import hashlib
import math
import re


def ratio(n, d):
    return {'numerator': n, 'denominator': d, 'value': n/d if d else None}


def evidence_metrics(events, gold):
    """Events already normalized by an adapter; exact source text + version required.

    read: {kind:'read', path, start, end, source_sha256, ok}
    context: {kind:'context', request_id, snippets:[{path, line, text, source_sha256}]}
    edit: {kind:'edit', changed:bool}. Only pre-edit evidence is graded.
    A missing context event is missing observation, never a measured zero.
    """
    required = {(s['path'], s['line'], s['text'], s['source_sha256']) for s in gold['lines']}
    files = set(gold['required_files'])
    ordered, seen, acquired, delivered = [], set(), set(), set()
    reads = repeats = contexts = 0
    opportunities = missing = 0
    pending = set()
    for event in events:
        if event['kind'] == 'edit' and event.get('changed'):
            break
        if event['kind'] == 'read' and event.get('ok'):
            if event['path'] not in ordered:
                ordered.append(event['path'])
            reads += 1
            signature = (event['path'], event['start'], event['end'], event['source_sha256'])
            repeats += signature in seen
            seen.add(signature)
            found = {(x['path'], x['line'], x['text'], x['source_sha256'])
                     for x in event.get('snippets', [])} & required
            acquired |= found
            pending |= found
        elif event['kind'] == 'context':
            contexts += 1
            visible = {(x['path'], x['line'], x['text'], x['source_sha256']) for x in event['snippets']}
            delivered |= visible & required
            opportunities += len(pending)
            missing += len(pending-visible)
            pending.clear()
    return {'recall_at_5': ratio(len(files & set(ordered[:5])), len(files)),
        'necessary_evidence_coverage': ratio(len(delivered), len(required)) if contexts else None,
        'immediate_context_loss': ratio(missing, opportunities) if contexts else None,
        'repeated_reads': ratio(repeats, reads), 'retrieved_files': ordered,
        'context_observations': contexts, 'unobserved_pending_evidence': len(pending),
        'scope': 'pre-first-edit exact versioned gold; missing observations unassessed; no semantic adoption inference'}


def source_snippets(path, text, source_text):
    """Produce typed spans only from independently supplied exact source lines.

    Parser adapters must establish path/line attribution. No substring-based
    inference from arbitrary prose, test output or an agent's final statement.
    """
    sha = hashlib.sha256(source_text.encode()).hexdigest()
    lines = source_text.splitlines()
    result = []
    for number, content in text:
        if 1 <= number <= len(lines) and lines[number-1] == content:
            result.append({'path': path, 'line': number, 'text': content, 'source_sha256': sha})
    return result


def normalize_read(agent, call_id, args, output, source_text):
    """Normalize actual CodeTrail/ApeCode numbered read output with provenance.

    source_text is read by the external observer at the tool boundary, never by
    evaluating agent-supplied Python. Unknown output formats yield no spans.
    """
    if agent not in {'codetrail', 'apecode'}:
        raise ValueError('Unsupported read adapter')
    path = args['path'].replace('\\', '/')
    while path.startswith('./'):
        path = path[2:]
    if path.startswith('/') or '..' in path.split('/') or ':' in path:
        raise ValueError('Noncanonical task path')
    pattern = r'^\s*(\d+): (.*)$' if agent == 'codetrail' else r'^\s*(\d+)\t(.*)$'
    parsed = []
    rendered = {}
    for line in output.splitlines():
        match = re.match(pattern, line)
        if match:
            number, content = int(match[1]), match[2]
            parsed.append((number, content))
            rendered[number] = line
    snippets = source_snippets(path, parsed, source_text)
    for snippet in snippets:
        snippet['rendered'] = rendered[snippet['line']]
    start = int(args.get('start', 1)) if agent == 'codetrail' else max(1, int(args.get('start_line', 1)))
    end = int(args.get('end', 200)) if agent == 'codetrail' else start+max(1, min(2000, int(args.get('num_lines', 200))))-1
    event = {'kind': 'read', 'call_id': call_id, 'path': path, 'start': start, 'end': end,
        'source_sha256': hashlib.sha256(source_text.encode()).hexdigest(),
        'ok': bool(snippets), 'snippets': snippets}
    return event


def normalize_context(request_id, messages, reads):
    """Only exact attributed tool-result lines in the actual outbound request.

    Summary/prose paraphrases are intentionally unassessed by this exact-span
    proxy. Do not use conversation-before-compaction instead of wire messages.
    """
    by_id = {r['call_id']: r for r in reads}
    if len(by_id) != len(reads):
        raise ValueError('Duplicate tool IDs: ambiguous context provenance')
    snippets = []
    for message in messages:
        if message.get('role') != 'tool' or not isinstance(message.get('content'), str):
            continue
        read = by_id.get(message.get('tool_call_id'))
        if read:
            lines = set(message['content'].splitlines())
            snippets.extend({k: s[k] for k in ('path', 'line', 'text', 'source_sha256')}
                for s in read['snippets'] if s['rendered'] in lines)
    return {'kind': 'context', 'request_id': request_id, 'snippets': snippets,
            'coverage_scope': 'exact attributed read_file tool-result spans only; search and paraphrases not assessed'}


def efficiency(rows):
    """Keep missing usage visible and include failed runs in costs."""
    values = [r.get('elapsed_ms') for r in rows]
    elapsed = sorted(v for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0)
    def percentile(q):
        if not elapsed:
            return None
        rank = (len(elapsed)-1)*q
        lo, hi = math.floor(rank), math.ceil(rank)
        return elapsed[lo]+(elapsed[hi]-elapsed[lo])*(rank-lo)
    tokens = [r.get('total_tokens') for r in rows]
    observed = [v for v in tokens if type(v) is int and v >= 0]
    complete = len(observed) == len(rows) and bool(rows)
    successes = sum(bool(r.get('functional_passed')) for r in rows)
    return {'runs': len(rows), 'successes': successes,
        'tokens_observed': sum(observed) if observed else None,
        'token_observed_runs': len(observed), 'token_complete': complete,
        'tokens_per_success': sum(observed)/successes if complete and successes else None,
        'elapsed_observed_runs': len(elapsed), 'p50_ms': percentile(.5), 'p95_ms': percentile(.95),
        'currency_cost': None, 'currency_cost_status': 'not_observed', 'overall_score': None}
