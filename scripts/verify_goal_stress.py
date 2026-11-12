"""Independent deterministic oracle for the GoalTree stress pilot.

Only reads frozen gold and completed observations. Does not import the Harness,
call a model, infer missing answers, or change acceptance based on results.
"""
import argparse
import csv
import hashlib
import json
import re
from pathlib import Path


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def answer_object(text):
    text = text.strip()
    if text.startswith('```json\n') and text.endswith('```'):
        text = text[8:-3].strip()
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def evaluate_fields(answer, expected):
    value = answer_object(answer)
    checks = {}
    for key, options in expected.items():
        observed = (value or {}).get(key)
        if isinstance(options, dict):
            checks[key] = isinstance(observed, str) and all(re.search(p, observed) for p in options['all_patterns'])
        else:
            checks[key] = any(type(observed) is type(option) and observed == option for option in options)
    return {'parsed': value is not None, 'checks': checks,
            'correct': sum(checks.values()), 'total': len(checks),
            'passed': bool(checks) and all(checks.values())}


def verify(directory):
    directory = Path(directory)
    frozen = load(directory / 'freeze.json')
    gold = frozen['gold']
    planned = frozen['tasks']
    rows, cases = [], {}
    for case in planned:
        case_dir = directory / case['id']
        runtime = load(case_dir / 'fixture.json') if (case_dir / 'fixture.json').exists() else {}
        case_rows = []
        for turn in case['turns']:
            result_path = case_dir / turn['id'] / 'result.json'
            result = load(result_path) if result_path.exists() else {}
            expected = gold[case['id']].get(turn['id'], {})
            scored = evaluate_fields(result.get('answer', ''), expected.get('fields', {}))
            visible_payloads = []
            for request_path in sorted(result_path.parent.glob('requests/*.json')):
                response_path = result_path.parent / 'responses' / request_path.name
                response = load(response_path) if response_path.exists() else {}
                if type(response.get('metadata', {}).get('provider_usage', {}).get('prompt_tokens')) is int:
                    visible_payloads.append(json.dumps(load(request_path).get('payload_without_reasoning', {}), ensure_ascii=False))
            state = result.get('state_after', {})
            desired = runtime.get('goals', {}).get(turn.get('target', 'A'), {}).get('id')
            binding = (result.get('binding') or {}).get('goal_id')
            declared = (answer_object(result.get('answer', '')) or {}).get('目标标识')
            foreign_before = result.get('foreign_before', {})
            foreign_after = {key: node for key, node in state.get('goals', {}).items()
                             if key in foreign_before}
            foreign_unchanged = foreign_before == foreign_after
            row = {'case': case['id'], 'turn': turn['id'], 'status': result.get('status', 'unstarted'),
                   'checkpoint': bool(expected.get('fields')), 'field_correct': scored['correct'],
                   'field_total': scored['total'], 'answer_passed': scored['passed'],
                   'target': turn.get('target', 'A'), 'binding_matches_target': bool(desired) and binding == desired,
                   'declared_goal_matches_target': bool(desired) and declared == desired if case['kind'] == 'switch' else None,
                   'foreign_goals_unchanged': foreign_unchanged if result else None,
                   'same_conversation': result.get('session_id') == runtime.get('conversation_session_id') if result else None,
                   'tool_steps': result.get('tool_steps'), 'elapsed_ms': result.get('elapsed_ms'),
                   'stop_reason': result.get('stop_reason'), 'scoring': scored}
            row['required_evidence_visible'] = {
                key: any(value in payload for payload in visible_payloads)
                for key, value in expected.get('required_evidence_strings', {}).items()}
            if result.get('error'):
                row['failure_category'] = 'runtime_or_api'
            elif not result:
                row['failure_category'] = 'unstarted'
            elif expected.get('fields') and not scored['passed']:
                row['failure_category'] = 'missing_or_wrong_fact'
            elif case['kind'] == 'switch' and binding != desired:
                row['failure_category'] = 'goal_routing_not_completed'
            else:
                row['failure_category'] = None
            if case['kind'] == 'switch' and desired != runtime.get('goals', {}).get('A', {}).get('id'):
                original_goal = runtime['goals']['A']['id']
                before_tasks = result.get('tasks_before', {})
                row['wrong_goal_mutated'] = any(
                    node.get('goal_id') == original_goal and before_tasks.get(key) != node
                    for key, node in state.get('tasks', {}).items()) or (
                        result.get('bound_goal_before') != state.get('goals', {}).get(original_goal)) if result else None
            else:
                row['wrong_goal_mutated'] = False if result else None
            if expected.get('deliverable'):
                rule = expected['deliverable']
                text = result.get('answer', '')
                row['deliverable_checks'] = {pattern: bool(re.search(pattern, text)) for pattern in rule['required_patterns']}
                row['deliverable_has_obsolete_fact'] = any(re.search(pattern, text) for pattern in rule['obsolete_patterns'])
                node_id = next((n['id'] for n in runtime.get('tasks', {}).get('A', []) if n['title'] == rule['task_title']), None)
                node = state.get('tasks', {}).get(node_id, {})
                row['deliverable_recorded_for_review'] = node.get('status') == 'review' and bool(node.get('deliverables'))
                row['deliverable_passed'] = (all(row['deliverable_checks'].values()) and not row['deliverable_has_obsolete_fact']
                                             and row['deliverable_recorded_for_review'])
                if not row['deliverable_passed']:
                    row['failure_category'] = 'deliverable_content_or_tracking'
            rows.append(row)
            case_rows.append(row)
        requests = []
        for p in sorted(case_dir.glob('*/requests/*.json')):
            requests.append(load(p))
        snapshots = [r for r in requests if r.get('stage') == 'prepared_before_privacy_gate']
        compactions = [r for r in snapshots if r.get('context_audit', {}).get('compacted_groups', 0) > 0]
        read_paths, read_ranges = set(), []
        errors, protocol_errors = [], 0
        for p in case_dir.glob('*/result.json'):
            for event in load(p).get('events', []):
                data = event
                if event.get('event') == 'tool_executed' and data.get('name') == 'read_file' and data.get('tool_status') == 'ok':
                    read_paths.add(data.get('args', {}).get('path'))
                    args = data.get('args', {})
                    read_ranges.append((args.get('path'), args.get('start', 1), args.get('end', 200)))
                if event.get('event') == 'tool_executed' and data.get('tool_status') != 'ok':
                    errors.append({'tool': data.get('name'), 'code': data.get('tool_error_code')})
                if event.get('event') == 'protocol_error':
                    protocol_errors += 1
        checkpoints = [r for r in case_rows if r['checkpoint']]
        cases[case['id']] = {
            'kind': case['kind'], 'planned_turns': len(case_rows),
            'started_turns': sum(r['status'] != 'unstarted' for r in case_rows),
            'checkpoint_passed': sum(r['answer_passed'] for r in checkpoints),
            'checkpoint_total': len(checkpoints),
            'checkpoint_attempted': sum(r['status'] != 'unstarted' for r in checkpoints),
            'field_correct': sum(r['field_correct'] for r in checkpoints),
            'field_total': sum(r['field_total'] for r in checkpoints),
            'compacted_requests': len(compactions),
            'max_pre_compaction_bytes': max((r.get('context_audit', {}).get('before_estimate', 0) for r in snapshots), default=0),
            'distinct_read_paths': sorted(p for p in read_paths if p),
            'successful_read_calls': len(read_ranges),
            'repeated_identical_read_ranges': len(read_ranges) - len(set(read_ranges)),
            'tool_failures': errors, 'protocol_error_events': protocol_errors,
            'tool_steps': sum(r['tool_steps'] for r in case_rows if type(r['tool_steps']) is int),
            'deliverable_passed': next((r.get('deliverable_passed') for r in case_rows if 'deliverable_passed' in r), None),
            'pressure_qualified': len(compactions) >= frozen['protocol']['minimum_compacted_requests']
                and len(read_paths) >= frozen['protocol']['minimum_distinct_reads'],
            'binding_correct': sum(r['binding_matches_target'] for r in case_rows if r['checkpoint']),
            'declared_goal_correct': sum(r['declared_goal_matches_target'] is True for r in case_rows if r['checkpoint']),
            'same_conversation_all_started': all(r['same_conversation'] for r in case_rows if r['status'] != 'unstarted'),
            'wrong_goal_mutation_turns': sum(r['wrong_goal_mutated'] is True for r in case_rows),
        }
    ledger = directory / 'ledger.jsonl'
    events = [json.loads(x) for x in ledger.read_text(encoding='utf-8').splitlines()] if ledger.exists() else []
    settlements = [e for e in events if e['event'] == 'settled']
    settled_ids = {e['id'] for e in settlements}
    reservations = [e for e in events if e['event'] == 'reserved']
    origin = load(directory / 'budget-origin.json') if (directory / 'budget-origin.json').exists() else {}
    imported = set(origin.get('imported_reservation_ids', []))
    batch_settlements = [e for e in settlements if e['id'] not in imported]
    summary = {'schema': 'goal-stress-independent-verification-v1', 'cases': cases,
               'model_request_reservations': len(reservations), 'known_usage_requests': len(settlements),
               'unsettled_reservations': sum(e['id'] not in settled_ids for e in reservations),
               'known_input_tokens': sum(e['usage']['prompt_tokens'] for e in settlements),
               'known_output_tokens': sum(e['usage']['completion_tokens'] for e in settlements),
               'batch_request_reservations': sum(e['id'] not in imported for e in reservations),
               'batch_known_input_tokens': sum(e['usage']['prompt_tokens'] for e in batch_settlements),
               'batch_known_output_tokens': sum(e['usage']['completion_tokens'] for e in batch_settlements),
               'actual_billed_cost': None, 'rows': rows,
               'gold_sha256': hashlib.sha256(json.dumps(gold, sort_keys=True).encode()).hexdigest(),
               'limitations': ['Synthetic development pilot, not a held-out benchmark or product ranking.',
                   'One sequence per scenario; repeated checkpoints are not independent tasks.',
                   'Pressure uses a frozen smaller byte budget, not a verified natural 1M-token window.',
                   'User instructions remain pinned by the existing Harness; tool-only facts are scored separately by field.',
                   'Prepared payload observation precedes privacy dispatch; successful provider usage is recorded separately.']}
    (directory / 'verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    with (directory / 'turns.csv').open('w', encoding='utf-8', newline='') as f:
        columns = [key for key in rows[0] if key != 'scoring'] if rows else []
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in columns} for row in rows)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    value = verify(args.output)
    print(json.dumps({key: value[key] for key in ('cases', 'model_request_reservations', 'known_input_tokens', 'known_output_tokens', 'unsettled_reservations')}, ensure_ascii=False))
