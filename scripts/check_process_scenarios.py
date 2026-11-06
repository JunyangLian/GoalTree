"""Offline scenario transitions with real KnowledgeStore; no model, no implicit approval."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.process_scenario import start, advance
from pico.evaluation.repair_suite import load_json, digest, write_json
from pico.knowledge import KnowledgeStore


def check(assets, output):
    assets, output = Path(assets), Path(output)
    lock = load_json(assets/'asset-lock.json')
    if any(digest(assets/n) != h for n, h in lock['assets'].items()):
        raise ValueError('Asset lock changed')
    output.mkdir(parents=True, exist_ok=False)
    rows = []
    for task in load_json(assets/'tasks.json'):
        if not task['phase_a']:
            continue
        target = output/task['id']
        first = start(task, target, 'codetrail')
        workspace = target/'workspace'
        store = KnowledgeStore(workspace)
        path = next(iter(task['phase_a']['files']))
        # Prose remains a candidate: the checker never silently confirms it.
        candidate = store.propose('Offline candidate tied to phase A source', 'probe:'+path,
                                  [path], scope='repository', session_id=first['session_id'])
        mechanical = store.index_file(path) if path.endswith('.py') else None
        second = advance(task, target, first['session_id'])
        reloaded = KnowledgeStore(workspace)
        active = reloaded.active(second['session_id'])
        assert all(r['id'] != candidate['id'] for r in active)
        assert reloaded.get(candidate['id'])['status'] == 'candidate'
        if mechanical:
            assert reloaded.get(mechanical['id'])['status'] == 'stale'
            assert all(r['id'] != mechanical['id'] for r in active)
        row = {'task_id': task['id'], 'passed': True, 'new_session_same_workspace': True,
               'prose_not_auto_confirmed': True, 'mechanical_stale_rejected': bool(mechanical),
               'api_calls': 0, 'agent_task_runs': 0}
        write_json(target/'check.json', row, exclusive=True)
        rows.append(row)
    result = {'passed': len(rows), 'total': len(rows), 'rows': rows,
        'scope': 'scripted transition/knowledge integration; not Agent memory recall or repair performance',
        'api_calls': 0}
    write_json(output/'summary.json', result, exclusive=True)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--assets', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(check(a.assets, a.output)))
