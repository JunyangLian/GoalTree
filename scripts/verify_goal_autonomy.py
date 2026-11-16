"""Independent stdlib-only lifecycle oracle. Never imports Agent/runtime code."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import posixpath
import re


def load(path):
    return json.loads(path.read_text('utf-8'))


def final_object(answer):
    """The last JSON object must end the answer; never pick a passing candidate."""
    text = answer.strip()
    if text.endswith('```'):
        matches = list(re.finditer(r'```(?:json)?\s*\n(.*?)\n```\s*$', text, re.S))
        if matches:
            text = matches[-1].group(1).strip()
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', text):
        try:
            value, end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(value, dict) and not text[end:].strip():
            return value
    return {}


def tools(result):
    # Parent already forwards child events. Count those once, rather than
    # adding each child projection again to the tool-step denominator.
    return [e for e in result.get('events', []) if e.get('event') == 'tool_executed']


def task_rows(state, goal_id):
    return {k: t for k, t in state['tasks'].items() if t['goal_id'] == goal_id}


def source_matches(event, relative, workspace):
    root = workspace.resolve().as_posix().rstrip('/')
    for raw in event.get('source_paths', []):
        text = raw.replace('\\', '/')
        if text.casefold().startswith(root.casefold() + '/'):
            text = text[len(root) + 1:]
        elif text.startswith('/') or re.match(r'^[A-Za-z]:/', text):
            continue
        if posixpath.normpath(text).casefold() == relative.casefold():
            return True
    return False


def expected_hashes(freeze, case_id, updated):
    files = dict(freeze['public_files'][case_id]['v1'])
    if updated:
        files.update({k: v for k, v in freeze['public_files'][case_id]['v2'].items() if k != 'brief.md'})
    return {k: hashlib.sha256(v.encode('utf-8')).hexdigest() for k, v in files.items()}


def failure_category(check):
    if check.startswith('field:'):
        return 'answer_incorrect'
    if check in {'source_read', 'fresh_source_read'}:
        return 'source_evidence_missing'
    if check in {'same_task_updated', 'title_current', 'description_current', 'acceptance_current', 'no_duplicate_tasks'}:
        return 'scope_sync'
    if check in {'foreign_goals_unchanged', 'foreign_tasks_unchanged', 'readonly_files', 'no_unauthorized_tools', 'manual_not_self_accepted'}:
        return 'boundary_violation'
    if check == 'independent_child':
        return 'delegation_missing'
    if check == 'service_state_preserved':
        return 'state_recovery'
    if check in {'returned_model', 'no_protocol_errors'}:
        return 'model_protocol'
    return 'run_incomplete' if check == 'completed' else 'planning_contract'


def verify(root, report_name='verification.json', current_submissions=False):
    frozen = load(root / 'freeze.json')
    slots = load(root / 'slots.json')
    cases = {c['id']: c for c in frozen['cases']}
    rows = []
    for slot in slots:
        spec, case_id = slot['spec'], slot['case']
        directory = root / case_id / spec['id']
        path = directory / 'result.json'
        if not path.exists():
            rows.append({'case': case_id, 'turn': spec['id'], 'status': slot['status'], 'passed': False,
                         'checks': {}, 'failures': ['runner_failure' if slot.get('error') else 'unstarted']})
            continue
        result = load(path)
        fixture = load(root / case_id / 'fixture.json')
        goal_id = fixture['goal']['id']
        before, after = result['state_before'], result['state_after']
        tasks = task_rows(after, goal_id)
        events = tools(result)
        responses = [load(p) for p in sorted((directory / 'responses').glob('*.json'))]
        checks = {'completed': result['status'] == 'completed',
            'foreign_goals_unchanged': {k: v for k, v in before['goals'].items() if k != goal_id} == {k: v for k, v in after['goals'].items() if k != goal_id},
            'foreign_tasks_unchanged': {k: v for k, v in before['tasks'].items() if v['goal_id'] != goal_id} == {k: v for k, v in after['tasks'].items() if v['goal_id'] != goal_id},
            'readonly_files': result['file_hashes_after'] == expected_hashes(frozen, case_id, spec['kind'] in {'refresh', 'restart'}),
            'no_unauthorized_tools': not any(e.get('name') in {'run_shell', 'write_file', 'patch_file', 'apply_changes', 'rewind_changes', 'confirm_task'} for e in events),
            'manual_not_self_accepted': all(t['status'] != 'completed' and t.get('acceptance_origin') != 'operator' for t in tasks.values()),
            'returned_model': bool(responses) and all(r['metadata'].get('provider_model') == frozen['protocol']['model'] for r in responses),
            'no_protocol_errors': not any(r['metadata'].get('protocol_error') for r in responses),
        }
        if spec['kind'] == 'plan':
            checks['small_plan'] = 2 <= len(tasks) <= 4
            checks['manual_plan'] = bool(tasks) and all(t['verification_mode'] == 'manual' and t['status'] == 'planned' and not t['runs'] and not t.get('deliverables') for t in tasks.values())
            checks['delivery_contract'] = any(cases[case_id]['format_v1'] in t['title'] and cases[case_id]['format_v1'] in t['description'] and cases[case_id]['format_v1'] in t['acceptance'] for t in tasks.values())
            checks['no_execution_for_planning'] = not result.get('child_runs')
        if 'gold' in spec:
            answer = final_object(result.get('answer', ''))
            for key, value in spec['gold'].items():
                checks['field:' + key] = type(answer.get(key)) is type(value) and answer.get(key) == value
        if spec['kind'] == 'execute':
            child = result.get('child_runs', [])
            checks['independent_child'] = bool(child) and all(c['session_id'] != result['session_id'] and c['binding']['goal_id'] == goal_id for c in child)
            checks['source_read'] = any(e.get('name') == 'read_file' and e.get('tool_status') == 'ok' and source_matches(e, 'records/v1/data.md', root / case_id / 'workspace') and e.get('delegated_run_id') for e in events)
        if spec['kind'] == 'change':
            old = task_rows(before, goal_id)
            candidates = [t for t in old.values() if cases[case_id]['format_v1'] in t['title'] or '结果交付' in t['title']]
            checks['same_task_updated'] = len(candidates) == 1 and candidates[0]['id'] in tasks and tasks[candidates[0]['id']]['spec_version'] > candidates[0]['spec_version']
            target = tasks.get(candidates[0]['id'], {}) if len(candidates) == 1 else {}
            for key in ('title', 'description', 'acceptance'):
                checks[key + '_current'] = cases[case_id]['format_v2'] in target.get(key, '')
            checks['no_duplicate_tasks'] = set(tasks) == set(old)
        if spec['kind'] == 'refresh':
            path = 'records/v2/data.md'
            checks['fresh_source_read'] = any(e.get('name') == 'read_file' and e.get('tool_status') == 'ok' and source_matches(e, path, root / case_id / 'workspace') and e.get('source_sha256') == expected_hashes(frozen, case_id, True)[path] for e in events)
            checks['no_duplicate_tasks'] = set(tasks) == set(task_rows(before, goal_id))
            if current_submissions:
                candidates = [t for t in task_rows(before, goal_id).values() if cases[case_id]['format_v2'] in t['title'] or '结果交付' in t['title']]
                target = tasks.get(candidates[0]['id'], {}) if len(candidates) == 1 else {}
                direct = any(d.get('task_spec_version') == target.get('spec_version') and d.get('run_id') == result.get('run_id') for d in target.get('deliverables', []))
                delegated = any(c.get('binding', {}).get('task_id') == target.get('id') and c.get('status') == 'completed' and c.get('binding', {}).get('task_spec_version') == target.get('spec_version') for c in result.get('child_runs', []))
                checks['current_deliverable_submitted'] = target.get('status') in {'review', 'completed'} and (direct or delegated)
        if spec['kind'] == 'restart':
            receipt = load(directory / 'restart.json')
            checks['service_state_preserved'] = receipt['before'] == receipt['after']
            checks['no_replayed_children'] = not result.get('child_runs')
        failures = sorted({failure_category(k) for k, value in checks.items() if not value})
        failed_tools = [e for e in events if e.get('tool_status') not in {'ok'}]
        reads = [e for e in events if e.get('name') == 'read_file' and e.get('tool_status') == 'ok']
        seen, repeated = set(), 0
        for event in reads:
            key = (event.get('delegated_run_id') or result['id'], tuple(event.get('source_paths', [])), event.get('source_sha256'), event.get('args', {}).get('start'), event.get('args', {}).get('limit'))
            repeated += key in seen
            seen.add(key)
        rows.append({'case': case_id, 'turn': spec['id'], 'status': result['status'], 'passed': all(checks.values()),
            'checks': checks, 'failures': failures, 'tool_steps': len(events), 'failed_tool_calls': len(failed_tools),
            'recovery_observed': bool(failed_tools) and any(e.get('name') == 'read_file' and e.get('tool_status') == 'ok' for e in events[events.index(failed_tools[-1]) + 1:]),
            'successful_reads': len(reads), 'repeated_read_calls': repeated,
            'model_requests': len(responses), 'elapsed_ms': result.get('elapsed_ms'),
            'known_tokens': sum(sum(r['metadata']['provider_usage'][k] for k in ('prompt_tokens', 'completion_tokens')) for r in responses if all(type((r['metadata'].get('provider_usage') or {}).get(k)) is int for k in ('prompt_tokens', 'completion_tokens'))),
            'usage_unknown_requests': sum(not all(type((r['metadata'].get('provider_usage') or {}).get(k)) is int for k in ('prompt_tokens', 'completion_tokens')) for r in responses)})
    snapshot = root / 'source-snapshot'
    mismatches = [name for name, sha in frozen['sources'].items() if not (snapshot / name).is_file() or hashlib.sha256((snapshot / name).read_bytes()).hexdigest() != sha]
    asset_bytes = (snapshot / 'benchmarks/goal_autonomy_v1/cases.json').read_bytes()
    asset_match = hashlib.sha256(asset_bytes).hexdigest() == frozen['task_asset_sha256'] and json.loads(asset_bytes) == frozen['cases']
    result = {'schema': 'goal-autonomy-observation-v2', 'current_submission_check': current_submissions,
        'planned_cases': len(cases), 'planned_turns': len(rows),
        'started_turns': sum(r['status'] != 'unstarted' for r in rows), 'passed_turns': sum(r['passed'] for r in rows),
        'complete_cases_passed': sum(all(r['passed'] for r in rows if r['case'] == key) for key in cases),
        'source_snapshot_mismatches': mismatches, 'task_asset_matches': asset_match,
        'per_case': {key: {'planned': sum(r['case'] == key for r in rows), 'passed': sum(r['case'] == key and r['passed'] for r in rows)} for key in cases},
        'rows': rows, 'limitations': [
            'Synthetic development diagnostic; not held-out or public Benchmark.',
            'Active Goal bound by host; Agent chooses task IDs, tool order and recovery. No autonomous cross-Goal routing.',
            'Restart is quiescent Workbench reconstruction, not killed-process or uncertain-effect recovery.',
            'Successful read does not prove understanding; recovery_observed is ordering only, not causal proof.',
            'Tool counts deduplicate forwarded children; read repeats only within each actor/run and turn.',
            'Five turns per case; not 1M-token pressure or days-long user validation.',
        ]}
    if Path(report_name).name != report_name or not report_name.endswith('.json'):
        raise ValueError('Report name must be a local JSON filename')
    (root / report_name).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', 'utf-8')
    with (root / Path(report_name).with_suffix('.csv')).open('w', encoding='utf-8', newline='') as handle:
        fields = ['case', 'turn', 'status', 'passed', 'failures', 'tool_steps', 'failed_tool_calls', 'recovery_observed', 'successful_reads', 'repeated_read_calls', 'model_requests', 'known_tokens', 'usage_unknown_requests', 'elapsed_ms']
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: row.get(k) for k in fields} for row in rows)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report-name', default='verification.json')
    parser.add_argument('--current-submissions', action='store_true')
    args = parser.parse_args()
    frozen = load(args.output / 'freeze.json')
    result = verify(args.output, args.report_name, args.current_submissions or frozen['protocol'].get('oracle_current_submissions', False))
    print(json.dumps({k: result[k] for k in ('planned_cases', 'planned_turns', 'started_turns', 'passed_turns', 'complete_cases_passed', 'per_case')}))
