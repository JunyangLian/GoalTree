"""Read-only trace attribution plus an optional fresh independent isolated rerun."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.cross_agent import snapshot, scope_valid
from pico.evaluation.isolated_execution import IsolatedExecutor, verify
from pico.evaluation.repair_suite import load_json, digest, write_json, utcnow


def jsonl(path):
    return [json.loads(s) for s in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []


def audit(batch, output, reverify=False):
    batch, output = Path(batch).resolve(), Path(output).resolve()
    frozen = load_json(batch/'freeze.json')
    for name, sha in frozen['assets'].items():
        if digest(batch/name) != sha:
            raise ValueError('Frozen artifact changed')
    with zipfile.ZipFile(batch/'source.zip') as archive:
        for name, sha in frozen['sources'].items():
            if hashlib.sha256(archive.read(name)).hexdigest() != sha:
                raise ValueError('Frozen source snapshot changed')
    output.mkdir(parents=True, exist_ok=False)
    tasks = {t['id']: t for t in load_json(batch/'tasks.json')}
    protocol = load_json(batch/'protocol.json')
    executor = IsolatedExecutor(protocol['backend'], output/'execution', protocol['image']) if reverify else None
    original_hashes = {p.relative_to(batch).as_posix(): digest(p) for p in batch.rglob('*')
                       if p.is_file() and p.name not in {'summary.json', 'runs.json', 'runs.csv'}}
    rows = []
    for slot in load_json(batch/'plan.json'):
        directory = batch/'runs'/slot['run_key']
        if not (directory/'result.json').exists():
            rows.append({**slot, 'status': 'unstarted_or_incomplete', 'verified': False})
            continue
        row, task = load_json(directory/'result.json'), tasks[slot['task_id']]
        final_workspace = directory/'final-workspace'
        actual = snapshot(final_workspace)
        final_valid = scope_valid(task, actual) and actual == row['final_sha256']
        phases = []
        for session in row['sessions']:
            sd = directory/('session-'+session['phase'])
            tools = jsonl(sd/'tools.jsonl')
            patch_failures = []
            for i, tool in enumerate(tools):
                if tool['name'] in {'patch_file', 'replace_in_file'} and (
                        tool['metadata'].get('tool_status') != 'ok' or tool['output'] == 'no replacements made'):
                    corrected = any(t['name'] in {'patch_file', 'replace_in_file', 'write_file'}
                        and t['args'].get('path') == tool['args'].get('path')
                        and t['metadata'].get('tool_status') == 'ok'
                        and t['before'] != t['snapshot'] for t in tools[i+1:])
                    patch_failures.append({'call_id': tool['call_id'], 'later_same_file_corrected': corrected})
            wire = jsonl(sd/'wire.jsonl')
            requests = [w['body'] for w in wire if w['event'] == 'request']
            responses = [w['body'] for w in wire if w['event'] == 'response']
            # Envelope checks are intentionally narrower than semantic protocol correctness.
            paired = {m['tool_call_id'] for r in requests for m in r['messages'] if m.get('role') == 'tool'}
            returned = [c['id'] for r in responses for c in r['choices'][0]['message'].get('tool_calls', [])]
            phases.append({'phase': session['phase'], 'session_id': session['session_id'],
                'model_requests': len(requests), 'model_responses': len(responses),
                'tool_steps': len(tools), 'dispatch_rejections': sum(t['rejected'] for t in tools),
                'policy_denials': sum(t['metadata'].get('tool_error_code') in
                    {'scenario_policy_denied', 'approval_denied'} for t in tools),
                'returned_tool_ids': len(returned), 'returned_ids_paired': len(set(returned)&paired),
                'duplicate_returned_ids': len(returned)-len(set(returned)),
                'not_dispatched': len(jsonl(sd/'protocol.jsonl')),
                'patch_failures': patch_failures, 'evidence': load_json(sd/'process/metrics.json'),
                'baseline_observed': session['baseline_test_observed'],
                'final_test_observed': session['final_test_observed']})
        memory = final_workspace/'.pico/memory/knowledge.sqlite3'
        memory_events = None
        if memory.exists():
            with sqlite3.connect(memory.as_uri()+'?mode=ro', uri=True) as db:
                memory_events = dict(db.execute('SELECT event,COUNT(*) FROM events GROUP BY event').fetchall())
        second = None
        if reverify:
            if final_valid:
                implementation = {n: (final_workspace/n).read_bytes() for n in task['editable_files']}
                second = verify(task, implementation, output/'reverification'/slot['run_key'], executor)
            else:
                second = {'accepted': False, 'reason': 'invalid_scope_no_external_source_read'}
        rows.append({**slot, 'status': 'completed', 'final_snapshot_valid': final_valid,
            'source_result_sha256': digest(directory/'result.json'), 'phases': phases,
            'functional_passed': row['functional_passed'], 'workflow_passed': row['workflow_passed'],
            'second_external_verification': second,
            'verified': bool(final_valid and (second['accepted'] if reverify else True)),
            'observed_native_memory_events': memory_events,
            'memory_adoption_effectiveness': None, 'api_calls': 0,
            'note': 'all decisions/faults/recovery scripted; mechanism evidence only'})
    assert all(digest(batch/n) == h for n, h in original_hashes.items()), 'Audit changed original evidence'
    phases = [p for r in rows for p in r.get('phases', [])]
    patches = [x for p in phases for x in p['patch_failures']]
    result = {'at': utcnow(), 'mode': protocol['mode'], 'original_records_unchanged': True,
        'auditor_sha256': digest(__file__), 'verifier_python': sys.version,
        'scenarios': len(rows), 'sessions': len(phases), 'independently_verified': sum(
            r['verified'] and r.get('second_external_verification') is not None for r in rows),
        'os_reverification_enabled': reverify, 'tool_steps': sum(p['tool_steps'] for p in phases),
        'policy_denials': sum(p['policy_denials'] for p in phases),
        'dispatch_rejections': sum(p['dispatch_rejections'] for p in phases), 'injected_patch_failures': len(patches),
        'scripted_patch_recoveries': sum(p['later_same_file_corrected'] for p in patches),
        'observed_model_requests_are_scripted': sum(p['model_requests'] for p in phases),
        'unpaired_returned_tool_ids': sum(p['returned_tool_ids']-p['returned_ids_paired'] for p in phases),
        'source_freeze_sha256': digest(batch/'freeze.json'), 'api_calls': 0, 'new_benchmark_runs': 0,
        'rows': rows, 'limitations': ['No model decision quality or autonomous recovery claim',
            'No actual Docker kernel test without a daemon', 'No semantic memory adoption metric',
            'Parser/envelope observations do not prove all protocol behavior correct']}
    write_json(output/'audit.json', result, exclusive=True)
    write_json(output/'original-record-hashes.json', original_hashes, exclusive=True)
    print(json.dumps({k: v for k, v in result.items() if k not in {'rows'}}), flush=True)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--batch', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--reverify', action='store_true')
    a = p.parse_args()
    audit(a.batch, a.output, a.reverify)
