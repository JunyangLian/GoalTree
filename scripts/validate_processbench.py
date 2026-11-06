"""Offline OS-isolated red/green qualification; never an Agent capability score."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.cross_agent import verify_candidate
from pico.evaluation.repair_suite import digest, install, load_json, write_json, utcnow


def validate(assets, output):
    assets, output = Path(assets), Path(output)
    lock = load_json(assets/'asset-lock.json')
    for name, value in lock['assets'].items():
        if digest(assets/name) != value:
            raise ValueError('Asset lock mismatch: '+name)
    tasks = load_json(assets/'tasks.json')
    references = load_json(assets/'controller-only-reference.json')
    if len({t['root_fault_id'] for t in tasks}) != len(tasks):
        raise ValueError('Root fault overlap across splits')
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in tasks:
        directory = output/task['id']
        directory.mkdir()
        if task['phase_a']:
            # Materialize actual separate phase snapshots; no generated answers/memory.
            install(directory/'phase-a-workspace', task['phase_a']['files'])
            stale = task['phase_a'].get('stale_paths', [])
            for path in stale:
                if task['phase_a']['files'][path] == task['files'][path]:
                    raise ValueError('Stale-memory scenario did not change source')
        red = verify_candidate(task, {}, directory/'faulty')
        green = verify_candidate(task, {n: t.encode() for n, t in references[task['id']].items()},
                                 directory/'reference')
        passed = bool(not red['accepted'] and red.get('tests_run') == task['expected_tests']
                      and green['accepted'] and green.get('tests_run') == task['expected_tests'])
        row = {'task_id': task['id'], 'split': task['split'], 'category': task['category'],
            'faulty_rejected': not red['accepted'], 'reference_accepted': green['accepted'],
            'tests_each': task['expected_tests'], 'qualified': passed,
            'phase_a_materialized': bool(task['phase_a']),
            'reference_sha256': {n: __import__('hashlib').sha256(t.encode()).hexdigest()
                                 for n, t in references[task['id']].items()},
            'agent_runs': 0, 'backend': 'windows_appcontainer'}
        rows.append(row)
        write_json(directory/'qualification.json', row, exclusive=True)
        print(json.dumps({'task': task['id'], 'qualified': passed}), flush=True)
    summary = {'at': utcnow(), 'asset_lock_sha256': digest(assets/'asset-lock.json'),
        'scenarios': len(rows), 'qualified': sum(r['qualified'] for r in rows),
        'tests_per_version': sum(r['tests_each'] for r in rows), 'external_verifications': len(rows)*2,
        'agent_runs': 0, 'scope': 'offline synthetic fixture red/green qualification, not memory/Agent performance',
        'rows': rows}
    write_json(output/'summary.json', summary, exclusive=True)
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assets', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    result = validate(a.assets, a.output)
    raise SystemExit(0 if result['qualified'] == result['scenarios'] else 1)
