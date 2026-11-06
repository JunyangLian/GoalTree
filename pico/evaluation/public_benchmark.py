"""Pinned SWE-bench inputs and official-verifier handoff, never a replacement grader."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random
import shutil
import subprocess

from .repair_suite import digest, load_json, utcnow, write_json

DATASET = 'princeton-nlp/SWE-bench_Lite'
REVISION = '6ec7bb89b9342f664a54a6e0a6ea6501d3437cc2'
PARQUET_SHA = '7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4'
HARNESS_COMMIT = '02e7a74ffd0b707aab73d203fe87bdc7c76afc8e'
INPUT_FIELDS = ('instance_id', 'repo', 'base_commit', 'problem_statement')


def agent_input(row):
    """Whitelist, never export gold patch, test_patch, hints or test names."""
    return {k: row[k] for k in INPUT_FIELDS}


def select(rows, count=5, seed=20261106):
    if count < 1 or count > len(rows):
        raise ValueError('Invalid sample size')
    ids = [r['instance_id'] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate upstream task IDs')
    groups = defaultdict(list)
    for row in sorted(rows, key=lambda r: r['instance_id']):
        groups[row['repo']].append(row)
    rng = random.Random(seed)
    repos = sorted(groups)
    rng.shuffle(repos)
    for values in groups.values():
        rng.shuffle(values)
    chosen = []
    while len(chosen) < count:
        for repo in repos:
            if groups[repo]:
                chosen.append(groups[repo].pop())
                if len(chosen) == count:
                    break
    return chosen


def prepare(parquet, output):
    import pyarrow.parquet as pq  # Offline preparation dependency, not runtime dependency.
    parquet, output = Path(parquet), Path(output)
    if digest(parquet) != PARQUET_SHA:
        raise ValueError('Pinned upstream parquet hash mismatch')
    rows = pq.read_table(parquet).to_pylist()
    chosen = select(rows)
    output.mkdir(parents=True, exist_ok=False)
    write_json(output/'agent-inputs.json', [agent_input(r) for r in chosen], exclusive=True)
    # This controller-only file must NEVER be mounted in an agent workspace.
    write_json(output/'verifier-only.json', chosen, exclusive=True)
    manifest = {'schema_version': 1, 'at': utcnow(), 'dataset': DATASET,
        'revision': REVISION, 'split': 'test', 'source_sha256': PARQUET_SHA,
        'harness_commit': HARNESS_COMMIT, 'selection_seed': 20261106,
        'selection': 'round-robin across seeded shuffled repositories and instances; no outcomes used',
        'population': len(rows), 'selected': [agent_input(r) for r in chosen],
        'status': 'candidate_selection_locked; environment and gold qualification pending',
        'formal_runs': 0, 'not_a_full_swebench_score': True,
        'assets': {n: digest(output/n) for n in ('agent-inputs.json', 'verifier-only.json')}}
    write_json(output/'selection-lock.json', manifest, exclusive=True)
    return manifest


def check(output):
    output = Path(output)
    manifest = load_json(output/'selection-lock.json')
    for name, sha in manifest['assets'].items():
        if digest(output/name) != sha:
            raise ValueError('Selected benchmark asset changed: '+name)
    return manifest


def docker_preflight():
    docker = shutil.which('docker')
    if not docker:
        return {'ready': False, 'reason': 'docker_cli_missing', 'host_fallback': False}
    try:
        result = subprocess.run([docker, 'info', '--format', '{{json .}}'],
                                capture_output=True, text=True, timeout=20)
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        return {'ready': result.returncode == 0 and data.get('OSType') == 'linux',
                'reason': 'linux_daemon_required', 'os_type': data.get('OSType'),
                'daemon_id': data.get('ID'), 'server_version': data.get('ServerVersion'),
                'host_fallback': False}
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return {'ready': False, 'reason': type(exc).__name__, 'host_fallback': False}


def export_predictions(output, rows, destination):
    manifest = check(output)
    expected = {r['instance_id'] for r in manifest['selected']}
    ids = [r['instance_id'] for r in rows]
    if len(set(ids)) != len(ids) or set(ids) != expected:
        raise ValueError('Require exactly one prediction per selected task; empty failures remain')
    with Path(destination).open('x', encoding='utf-8', newline='\n') as stream:
        for row in rows:
            if not isinstance(row.get('model_patch'), str) or not isinstance(row.get('model_name_or_path'), str):
                raise ValueError('Invalid official prediction schema')
            stream.write(json.dumps({k: row[k] for k in ('instance_id', 'model_patch', 'model_name_or_path')})+'\n')


def verifier_handoff(output, predictions, harness, python, destination):
    """One unique official run; blocked attempts stay on disk; no host test fallback."""
    output, harness, destination = Path(output), Path(harness), Path(destination)
    manifest = check(output)
    git = shutil.which('git') or r'C:\Program Files\Git\cmd\git.exe'
    head = subprocess.run([git, '-C', str(harness), 'rev-parse', 'HEAD'],
                          check=True, capture_output=True, text=True).stdout.strip()
    if head != HARNESS_COMMIT:
        raise ValueError('Official harness commit mismatch')
    dirty = subprocess.run([git, '-C', str(harness), 'status', '--porcelain', '--untracked-files=all'],
                           check=True, capture_output=True, text=True).stdout.strip()
    if dirty:
        raise ValueError('Official harness source is modified')
    destination.mkdir(parents=True, exist_ok=False)
    # Validate the schema and copy the exact submitted bytes before invoking Docker.
    rows = [json.loads(s) for s in Path(predictions).read_text(encoding='utf-8').splitlines() if s.strip()]
    export_predictions(output, rows, destination/'predictions.jsonl')
    # The verifier receives pinned local rows, not a mutable HF dataset alias.
    shutil.copyfile(output/'verifier-only.json', destination/'dataset.json')
    run_id = 'codetrail-'+hashlib.sha256(str(destination.resolve()).encode()).hexdigest()[:20]
    status = docker_preflight()
    command = [str(python), '-m', 'swebench.harness.run_evaluation',
        '--dataset_name', str((destination/'dataset.json').resolve()), '--split', 'test',
        '--predictions_path', str((destination/'predictions.jsonl').resolve()),
        '--max_workers', '1', '--run_id', run_id]
    write_json(destination/'handoff.json', {'at': utcnow(), 'preflight': status,
        'harness_commit': head, 'selection_lock_sha256': digest(output/'selection-lock.json'),
        'command': command, 'run_id': run_id, 'status': 'ready' if status['ready'] else 'blocked',
        'official_grading_completed': False}, exclusive=True)
    if not status['ready']:
        return False
    # Isolated interpreter must have the pinned harness installed. Verify its imported path.
    probe = subprocess.run([str(python), '-c', 'import swebench; print(swebench.__file__)'],
                           capture_output=True, text=True, check=True, timeout=20)
    if not Path(probe.stdout.strip()).resolve().is_relative_to(harness.resolve()):
        raise ValueError('Interpreter imports a different SWE-bench installation')
    with (destination/'official.log').open('xb') as log:
        result = subprocess.run(command, cwd=destination, stdout=log, stderr=subprocess.STDOUT,
                                timeout=7200)
    write_json(destination/'process-exit.json', {'exit_code': result.returncode,
        'note': 'Process exit is not a repair score; inspect official reports and per-instance tests.'}, exclusive=True)
    return result.returncode == 0
