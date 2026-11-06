"""Independent revalidation and descriptive cohorts for the real GLM dev pilot."""
import argparse
import csv
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
import hashlib
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.cross_agent import snapshot, scope_valid
from pico.evaluation.isolated_execution import IsolatedExecutor, verify
from pico.evaluation.repair_suite import load_json, digest, write_json, utcnow
from pico.providers.glm_protocol import measured_zero_reasoning
from scripts.glm_live_support import Budget


def lines(path):
    return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def audit_batch(batch, output):
    frozen = load_json(batch/'freeze.json')
    for name, sha in frozen['assets'].items(): assert digest(batch/name) == sha, name
    with zipfile.ZipFile(batch/'source.zip') as archive:
        for name, sha in frozen['sources'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == sha, name
    originals = {p.relative_to(batch).as_posix(): digest(p) for p in batch.rglob('*') if p.is_file()}
    tasks = {t['id']: t for t in load_json(batch/'tasks.json')}
    executor = IsolatedExecutor('appcontainer', output/'execution')
    rows = []
    for slot in load_json(batch/'plan.json'):
        directory = batch/'runs'/slot['run_key']
        if not (directory/'result.json').exists():
            rows.append({**slot, 'cohort': batch.name, 'started': directory.exists(),
                'functional_passed': False, 'workflow_passed': False, 'failure_category': 'unstarted_or_incomplete'})
            continue
        result, task = load_json(directory/'result.json'), tasks[slot['task_id']]
        workspace = directory/'final-workspace'
        valid = snapshot(workspace) == result['final_sha256'] and scope_valid(task, snapshot(workspace))
        if valid:
            independent = verify(task, {n: (workspace/n).read_bytes() for n in task['editable_files']},
                                 output/'external'/slot['run_key'], executor)
        else: independent = {'accepted': False, 'reason': 'scope_invalid_no_source_overlay'}
        session = result.get('session') or {}
        wire = lines(directory/'session-b/wire.jsonl')
        requests = [e['body'] for e in wire if e['event'] == 'request']
        responses = [e['body'] for e in wire if e['event'] == 'response']
        tools = lines(directory/'session-b/tools.jsonl')
        zero = sum(measured_zero_reasoning(r['choices'][0]['message'], r.get('usage') or {}) for r in responses)
        provided = sum(isinstance(r['choices'][0]['message'].get('reasoning_content'), str) for r in responses)
        protocol = lines(directory/'session-b/protocol.jsonl')
        cause = result['failure_category']
        if 'missing reasoning_content' in (result.get('error') or '') and zero:
            cause = 'harness_rejected_measured_zero_reasoning_turn'
        row = {**slot, 'cohort': batch.name, 'started': True,
            'functional_passed': result['functional_passed'], 'workflow_passed': result['workflow_passed'],
            'external_reverification_passed': independent['accepted'], 'snapshot_valid': valid,
            'independent_verifier': independent, 'failure_category': cause, 'error': result.get('error'),
            'tool_steps': len(tools), 'model_requests': len(requests), 'model_responses': len(responses),
            'input_tokens': result.get('input_tokens'), 'output_tokens': result.get('output_tokens'),
            'elapsed_ms': result['elapsed_ms'], 'zero_reasoning_responses': zero,
            'responses_with_reasoning_text_field': provided, 'protocol_observations': protocol,
            'policy_denials': sum(t['metadata'].get('tool_error_code') in {'scenario_policy_denied', 'approval_denied'} for t in tools),
            'process_evidence': session.get('process_evidence'), 'source_result_sha256': digest(directory/'result.json')}
        rows.append(row)
    assert all(digest(batch/name) == sha for name, sha in originals.items()), 'Original evidence changed'
    write_json(output/'original-hashes.json', originals, exclusive=True)
    write_json(output/'audit.json', rows, exclusive=True)
    return rows


def export(output):
    output.mkdir(parents=True, exist_ok=False)
    audit_root = ROOT/'artifacts/glm-independent-audit-2026-11-06'
    audit_root.mkdir(parents=True, exist_ok=False)
    all_rows, cohorts = [], []
    for name in ('glm-process-dev-pilot-2026-11-06', 'glm-process-dev-pilot-v2-2026-11-06'):
        batch = ROOT/'artifacts'/name
        rows = audit_batch(batch, audit_root/name)
        write_json(output/(name+'-audit.json'), rows, exclusive=True)
        all_rows += rows
        cohorts.append({'cohort': name, 'freeze_sha256': digest(batch/'freeze.json'), 'tasks': 2,
            'started': sum(r['started'] for r in rows), 'functional': sum(r['functional_passed'] for r in rows),
            'workflow': sum(r['workflow_passed'] for r in rows),
            'external_reverified': sum(r.get('external_reverification_passed', False) for r in rows),
            'by_agent': {a: {'runs': len(g), 'functional': sum(r['functional_passed'] for r in g),
                'workflow': sum(r['workflow_passed'] for r in g), 'tool_steps': sum(r.get('tool_steps', 0) for r in g),
                'input_tokens': sum(r['input_tokens'] for r in g) if all(type(r.get('input_tokens')) is int for r in g) else None,
                'output_tokens': sum(r['output_tokens'] for r in g) if all(type(r.get('output_tokens')) is int for r in g) else None}
                for a in ('codetrail', 'apecode') if (g := [r for r in rows if r['agent'] == a])}})
    window = ROOT/'artifacts/glm-budget-window-2026-11-06'
    charged, pending, events = Budget(window).state()
    write_json(output/'budget-ledger.json', events, exclusive=True)
    write_json(output/'budget-config.json', load_json(window/'budget.json'), exclusive=True)
    summary = {'at': utcnow(), 'model': 'glm-5.3-flash', 'base_url': 'https://open.bigmodel.cn/api/paas/v4',
        'mode': 'real model, two synthetic development tasks; one run per task/agent/cohort',
        'unique_tasks': 2, 'cohorts': cohorts, 'total_started_runs': sum(r['started'] for r in all_rows),
        'tariff_upper_estimate_cny': str(charged), 'unknown_usage_reserved_cny': str(pending),
        'max_window_cny': '1', 'actual_account_debit_cny': None,
        'successful_api_responses_including_preflight': sum(e['event'] == 'settled' for e in events),
        'limitations': ['Known dev tasks, not holdout, SWE-bench or whole repositories',
            'No statistical or single-factor superiority claim', 'Mutable provider model alias; exact weights unverified',
            'Functional test passing is not complete-workflow success', 'All initial failures and cohorts retained']}
    write_json(output/'summary.json', summary, exclusive=True)
    write_json(output/'runs.json', all_rows, exclusive=True)
    with (output/'runs.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, ['cohort', 'run_key', 'agent', 'task_id', 'started', 'functional_passed',
            'workflow_passed', 'external_reverification_passed', 'tool_steps', 'model_requests', 'input_tokens',
            'output_tokens', 'elapsed_ms', 'zero_reasoning_responses', 'policy_denials', 'failure_category'],
            extrasaction='ignore', lineterminator='\n')
        writer.writeheader(); writer.writerows(all_rows)
    for name in ('glm-native-protocol-2026-11-06', 'glm-live-pilot-2026-11-06', 'glm-standard-availability-2026-11-06'):
        directory = ROOT/'artifacts'/name
        result_file = next((directory/n for n in ('status.json', 'availability.json') if (directory/n).exists()), None)
        if result_file: write_json(output/(name+'-status.json'), load_json(result_file), exclusive=True)
    for name in ('glm-zero-fix-targeted-v2-2026-11-06.xml', 'glm-live-full-v2-2026-11-06.xml'):
        root = ET.parse(ROOT/'artifacts'/name)
        assert not root.findall('.//failure') and not root.findall('.//error') and not root.findall('.//skipped')
        shutil.copyfile(ROOT/'artifacts'/name, output/name)
    # Store complete raw evidence locally. Keys and authorization headers never enter it.
    archive = ROOT/'artifacts/glm-real-evidence-2026-11-06.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
        directories = [ROOT/'artifacts'/c['cohort'] for c in cohorts] + [window,
            ROOT/'artifacts/glm-native-protocol-2026-11-06', ROOT/'artifacts/glm-live-pilot-2026-11-06',
            ROOT/'artifacts/glm-standard-availability-2026-11-06']
        for directory in directories:
            for p in sorted(directory.rglob('*')):
                if p.is_file(): z.write(p, directory.name+'/'+p.relative_to(directory).as_posix())
        for name in ('UPSTREAM_NOTICE.md', 'LICENSE'):
            if (ROOT/name).exists(): z.write(ROOT/name, 'legal/'+name)
    write_json(output/'raw-evidence.json', {'local_path': archive.relative_to(ROOT).as_posix(),
        'sha256': digest(archive), 'original_records_not_modified': True}, exclusive=True)
    old = load_json(ROOT/'validation/process-controller-2026-11-06/historical-evidence-check.json')
    for name, sha in old.items(): assert digest(ROOT/name) == sha
    write_json(output/'historical-evidence-check.json', old, exclusive=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


def describe(output):
    """Derive detailed diagnostics without modifying a frozen run or prior audit."""
    rows = load_json(output/'runs.json')
    events = load_json(output/'budget-ledger.json')
    owners = {e['id']: e.get('owner', '') for e in events if e['event'] == 'reserved'}
    cohorts, diagnostics = {}, []
    for row in rows:
        batch = ROOT/'artifacts'/row['cohort']
        directory = batch/'runs'/row['run_key']/'session-b'
        missing = []
        replay_errors = []
        generated = {}
        replayed = set()
        response_number = 0
        for event in lines(directory/'wire.jsonl'):
            body = event['body']
            if event['event'] == 'response':
                response_number += 1
                choice = body['choices'][0]
                message = choice['message']
                calls = message.get('tool_calls') or []
                if calls and isinstance(message.get('reasoning_content'), str):
                    generated[tuple(c['id'] for c in calls)] = message['reasoning_content']
                if not isinstance(message.get('reasoning_content'), str):
                    tokens = ((body.get('usage') or {}).get('completion_tokens_details') or {}).get('reasoning_tokens')
                    missing.append({'response_number': response_number, 'reasoning_tokens': tokens,
                        'classification': 'measured_zero' if measured_zero_reasoning(message, body.get('usage') or {})
                        else 'positive_usage_missing_text' if type(tokens) is int and tokens > 0 else 'unknown_usage_missing_text',
                        'native_tool_calls': len(calls)})
            else:
                for message in body.get('messages', []):
                    calls = message.get('tool_calls') or []
                    identifiers = tuple(c['id'] for c in calls)
                    if message.get('role') == 'assistant' and identifiers in generated:
                        replayed.add(identifiers)
                        if message.get('reasoning_content') != generated[identifiers]:
                            replay_errors.append({'call_ids': list(identifiers), 'reason': 'generated_text_changed_or_missing'})
        detailed_failure = row['failure_category']
        if row.get('error') and 'missing reasoning_content' in row['error']:
            detailed_failure = ('harness_rejected_measured_zero_reasoning_turn'
                if missing and missing[-1]['classification'] == 'measured_zero'
                else 'provider_reasoning_usage_without_text_rejected')
        diagnostics.append({'cohort': row['cohort'], 'run_key': row['run_key'],
            'failure_category': detailed_failure, 'missing_reasoning_responses': missing,
            'replayed_reasoning_tool_turns': len(replayed), 'reasoning_replay_errors': replay_errors,
            'scope': 'Exact replay of returned reasoning on native tool turns observed in subsequent requests; no inference about unreturned text'})
        group = cohorts.setdefault(row['cohort'], {'tariff_upper_estimate_cny': '0', 'agents': {}})
        agent = group['agents'].setdefault(row['agent'], {'runs': 0, 'tool_steps': 0, 'model_requests': 0,
            'input_tokens': 0, 'output_tokens': 0, 'elapsed_ms': [], 'policy_denials': 0,
            'protocol_rejections': 0, 'process_evidence': {}})
        agent['runs'] += 1
        for key in ('tool_steps', 'model_requests', 'input_tokens', 'output_tokens', 'policy_denials'):
            agent[key] = None if agent[key] is None or row[key] is None else agent[key]+row[key]
        agent['elapsed_ms'].append(row['elapsed_ms'])
        agent['protocol_rejections'] += sum(p['event'] == 'response_not_executable' for p in row['protocol_observations'])
        for key in ('recall_at_5', 'necessary_evidence_coverage', 'immediate_context_loss', 'repeated_reads'):
            metric = row['process_evidence'][key]
            value = agent['process_evidence'].setdefault(key, {'numerator': 0, 'denominator': 0})
            for part in ('numerator', 'denominator'): value[part] += metric[part]
    for cohort, group in cohorts.items():
        group['tariff_upper_estimate_cny'] = str(sum((Decimal(e['tariff_upper_estimate_cny'])
            for e in events if e['event'] == 'settled' and cohort in owners[e['id']]), Decimal(0)))
    first, second = [ROOT/'artifacts'/c for c in cohorts]
    checks = {'same_tasks_sha256': digest(first/'tasks.json') == digest(second/'tasks.json'),
        'same_plan_sha256': digest(first/'plan.json') == digest(second/'plan.json'),
        'same_model_configuration': load_json(first/'protocol.json')['model'] == load_json(second/'protocol.json')['model'],
        'same_external_agent_and_worker': load_json(first/'external.json') == load_json(second/'external.json')}
    assert all(checks.values()), 'Controlled cohort assets differ'
    write_json(output/'analysis.json', {'cohorts': cohorts, 'controlled_checks': checks,
        'run_diagnostics': diagnostics}, exclusive=True)
    audit_root = ROOT/'artifacts/glm-independent-audit-2026-11-06'
    archive = ROOT/'artifacts/glm-independent-audit-evidence-2026-11-06.zip'
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(audit_root.rglob('*')):
            if p.is_file(): z.write(p, p.relative_to(audit_root).as_posix())
    write_json(output/'independent-audit-evidence.json', {'local_path': archive.relative_to(ROOT).as_posix(),
        'sha256': digest(archive)}, exclusive=True)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--analyze', action='store_true', help='Describe an existing export without altering frozen records')
    a = p.parse_args()
    if a.analyze: describe(a.output.resolve())
    else:
        export(a.output.resolve())
        describe(a.output.resolve())
