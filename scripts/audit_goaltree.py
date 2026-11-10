"""Read-only independent audit of GoalTree state/events and optional demo tests.

Does not load project .env, call models, modify task acceptance or replay tools.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys


def audit(root, verify_demo=False):
    root = root.resolve()
    db = sqlite3.connect((root/'.pico/goaltree.sqlite3').as_uri()+'?mode=ro', uri=True)
    try:
        final = json.loads(db.execute('SELECT body FROM workspace WHERE id=1').fetchone()[0])
        events = db.execute('SELECT seq,kind,payload,state_sha256 FROM goal_events ORDER BY seq').fetchall()
    finally:
        db.close()
    replay = {**final, 'goals': {}, 'tasks': {}, 'selection': {'goal_id': '', 'task_id': ''}, 'revision': 0}
    replay.pop('assistant_session_id', None)
    errors = []
    for seq, kind, payload, expected in events:
        change = json.loads(payload)['changes']
        for key in ('goals', 'tasks'):
            replay[key].update(change[key])
        replay['selection'], replay['revision'] = change['selection'], change['revision']
        if 'assistant_session_id' in change and change['assistant_session_id'] is not None:
            replay['assistant_session_id'] = change['assistant_session_id']
        digest = hashlib.sha256(json.dumps(replay, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if digest != expected:
            errors.append(f'event_{seq}_state_hash_mismatch')
    if replay != final:
        errors.append('replayed_state_differs_from_persisted_state')
    current_sessions = set()
    for item in [*final['goals'].values(), *final['tasks'].values()]:
        for key in ('session_id', 'chat_session_id', 'planner_session_id'):
            if item[key] in current_sessions:
                errors.append('duplicate_current_session_binding')
            current_sessions.add(item[key])
    for task in final['tasks'].values():
        refs = task['depends_on'] + ([task['parent_id']] if task['parent_id'] else [])
        if any(ref not in final['tasks'] or final['tasks'][ref]['goal_id'] != task['goal_id'] for ref in refs):
            errors.append('cross_goal_or_missing_reference')
        for run in task['runs']:
            if run['goal_id'] != task['goal_id'] or run['task_id'] != task['id']:
                errors.append('run_scope_mismatch')
            if run.get('session_id') and run['session_id'] not in {task['session_id'], *[old['session_id'] for old in task['context_history']]}:
                errors.append('run_session_not_in_task_context_history')
    snapshots = []
    for path in sorted((root/'.pico/web/tasks').glob('*.json')):
        run = json.loads(path.read_text(encoding='utf-8'))
        binding = run.get('binding')
        if binding and run.get('session_id') != binding['session_id']:
            errors.append('web_run_session_binding_mismatch')
        snapshots.append({'id': run['id'], 'binding': binding, 'status': run['status'],
                          'verification': run.get('verification'), 'tool_steps': run.get('tool_steps'),
                          'model_calls': run.get('attempts'),
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    result = {'workspace': str(root), 'goal_count': len(final['goals']), 'task_count': len(final['tasks']),
              'event_count': len(events), 'event_hash_replay_matches': replay == final and not any('hash' in e for e in errors),
              'errors': errors, 'web_runs': snapshots, 'scope': 'offline integration evidence, not benchmark/model performance'}
    if verify_demo:
        environment = {k: v for k, v in os.environ.items() if k.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'COMSPEC'}}
        environment['PYTHONIOENCODING'] = 'utf-8'
        test = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-v'], cwd=root,
                              env=environment, capture_output=True, encoding='utf-8', timeout=30)
        result['independent_demo_test'] = {'python': sys.executable, 'command': 'python -B -m unittest discover -v',
                                            'exit_code': test.returncode, 'stdout': test.stdout, 'stderr': test.stderr}
        if test.returncode:
            errors.append('independent_demo_test_failed')
    result['passed'] = not errors
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--verify-demo', action='store_true')
    args = parser.parse_args()
    result = audit(args.workspace, args.verify_demo)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        parser.error('output already exists; keep old evidence and choose a new path')
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('passed', 'goal_count', 'task_count', 'event_count', 'errors')}, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
