"""Frozen dual-Agent multi-session controller integration; offline scripted fixtures only.

No benchmark gold is loaded. urllib is fail-closed patched for the entire worker;
the HTTP protocol and both original Agent loops run with scripted responses.
This CLI deliberately does not expose a paid-model launch option.
"""
import argparse
import csv
import difflib
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.agent_session import run_codetrail, run_apecode
from pico.evaluation.cross_agent import clean_environment, kill_tree, scope_valid, snapshot
from pico.evaluation.isolated_execution import IsolatedExecutor, verify, cleanup_abandoned
from pico.evaluation.process_scenario import start, advance
from pico.evaluation.process_trace import efficiency
from pico.evaluation.repair_suite import digest, load_json, utcnow, write_json
from pico.windows_sandbox import VERIFY
from scripts.bigmodel_benchmark_adapter import CONFIG, BigModelChatModel, make_codetrail_client
from scripts.run_cross_agent_pilot import source_info


def h(text):
    return hashlib.sha256(text.encode()).hexdigest()


def fixtures():
    """New controller toys, not additions to any benchmark or held-out set."""
    values = []
    for key, phase_a, files, edit in [
        ('controller_add', None, {'compute.py': 'def add(a, b):\n    return a - b\n',
         'test_acceptance.py': 'import unittest\nfrom compute import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(2, 3), 5)\n'},
         ('compute.py', 'return a - b', 'return a + b')),
        ('controller_changed_limit', {'files': {'limits.py': 'MAXIMUM = 2\n'},
         'prompt': 'Read limits.py and retain source-backed knowledge. Do not change files or run commands.'},
         {'limits.py': 'MAXIMUM = 4\n', 'compute.py': 'from limits import MAXIMUM\n\ndef limit(n):\n    return min(n, 2)\n',
          'test_acceptance.py': 'import unittest\nfrom compute import limit\nclass T(unittest.TestCase):\n    def test_limit(self): self.assertEqual(limit(5), 4)\n'},
         ('compute.py', 'min(n, 2)', 'min(n, MAXIMUM)'))]:
        files['README.md'] = 'Offline scripted controller fixture; not a model benchmark.\n'
        lines = [{'path': n, 'line': i, 'text': line, 'source_sha256': h(text)}
                 for n, text in files.items() if n.endswith('.py') and not n.startswith('test_')
                 for i, line in enumerate(text.splitlines(), 1)]
        values.append({'id': key, 'files': files, 'sha256': {n: h(t) for n, t in files.items()},
            'editable_files': ['compute.py'], 'expected_tests': 1, 'phase_a': phase_a,
            'prompt': 'Fix the implementation. First run '+VERIFY+'. Inspect current implementation and imports. '
                'Only compute.py may be changed. Never alter tests. Rerun the same test after editing.',
            'gold': {'required_files': sorted({s['path'] for s in lines}), 'lines': lines},
            'scripted_edit': list(edit), 'category': 'controller_integration_only'})
    return values


def actions(task, agent, phase):
    if phase == 'a':
        return [('read_file', {'path': n}) for n in task['phase_a']['files']]
    shell = 'run_shell' if agent == 'codetrail' else 'exec_command'
    patch_name = 'patch_file' if agent == 'codetrail' else 'replace_in_file'
    path, old, new = task['scripted_edit']
    def edit(before):
        return {'path': path, **({'old_text': before, 'new_text': new} if agent == 'codetrail'
                                else {'old': before, 'new': new})}
    return [(shell, {'command': 'python -c "print(0)"'}),
        ('write_file', {'path': 'test_acceptance.py', 'content': 'forged'}),
        (shell, {'command': VERIFY}),
        *[('read_file', {'path': n}) for n in task['gold']['required_files']],
        (patch_name, edit('deliberately absent text in integration probe')),
        ('read_file', {'path': path}), (patch_name, edit(old)), (shell, {'command': VERIFY})]


class ScriptedHTTP:
    def __init__(self, planned, phase):
        self.planned, self.phase, self.index = planned, phase, 0

    def __call__(self, request, **kwargs):
        if request.full_url != CONFIG['base_url']+'/chat/completions':
            raise AssertionError('Unexpected network operation blocked by offline controller')
        payload = json.loads(request.data)
        assert payload['model'] == CONFIG['model'] and not payload.get('stream')
        index = self.index
        self.index += 1
        if index < len(self.planned):
            name, args = self.planned[index]
            message = {'role': 'assistant', 'content': None, 'reasoning_content': 'offline scripted fixture',
                'tool_calls': [{'id': f'fixture-{index}', 'type': 'function',
                               'function': {'name': name, 'arguments': json.dumps(args)}}]}
        else:
            message = {'role': 'assistant', 'content': 'Scripted inspection complete.' if self.phase == 'a'
                else 'Scripted repair and actual tests complete.', 'reasoning_content': 'offline scripted fixture'}
        body = {'model': CONFIG['model'], 'choices': [{'finish_reason': 'tool_calls' if message.get('tool_calls') else 'stop',
                                                     'message': message}]}
        class Response:
            headers = {'Content-Type': 'application/json'}
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(body).encode()
        return Response()


def freeze(output, source, python, backend='appcontainer', image=None):
    external = source_info(source)
    version = subprocess.run([str(python), '--version'], capture_output=True, check=True, text=True).stdout.strip()
    if tuple(map(int, version.split()[1].split('.')[:2])) < (3, 13):
        raise ValueError('Both workers require Python >=3.13 for pinned ApeCode')
    output.mkdir(parents=True, exist_ok=False)
    tasks = fixtures()
    plan = [{'agent': a, 'task_id': t['id'], 'repeat': r} for t in tasks
            for a in ('codetrail', 'apecode') for r in (1, 2)]
    random.Random(20261106).shuffle(plan)
    for i, p in enumerate(plan, 1):
        p['run_key'] = f"{i:02d}-{p['agent']}-{p['task_id']}-r{p['repeat']}"
    protocol = {'at': utcnow(), 'mode': 'offline_scripted_integration_not_benchmark',
        'model_api_calls': 0, 'fixture_count': 2, 'scenario_slots': 8, 'session_slots': 12,
        'backend': backend, 'image': image, 'concurrency': 1, 'max_tools_per_session': 20,
        'max_model_requests_per_session': 30, 'worker_deadline_seconds': 645,
        'sampling_contract': CONFIG, 'started_slot_replacement': False,
        'memory': 'same scenario workspace; fresh clients and session IDs; no prose auto-confirmation',
        'limits': 'scripted decisions; no Agent ranking, cost or model repair success claim'}
    # Validate Docker immutability before recording a batch.
    if backend == 'docker':
        import re
        if not image or not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}', image):
            raise ValueError('Docker image digest required')
    for name, data in [('tasks.json', tasks), ('plan.json', plan), ('protocol.json', protocol),
        ('external.json', {'source': str(source.resolve()), 'source_hashes': external,
            'python': str(python.resolve()), 'python_sha256': digest(python), 'version': version})]:
        write_json(output/name, data, exclusive=True)
    files = list((ROOT/'pico').rglob('*.py')) + [Path(__file__).resolve(),
        ROOT/'scripts/bigmodel_benchmark_adapter.py', ROOT/'scripts/run_cross_agent_pilot.py']
    hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in files}
    with zipfile.ZipFile(output/'source.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for name in hashes: archive.write(ROOT/name, name)
        for name in external: archive.write(source/name, 'upstream/apecode/'+name)
    write_json(output/'freeze.json', {'at': utcnow(), 'sources': hashes,
        'assets': {n: digest(output/n) for n in ('tasks.json', 'plan.json', 'protocol.json', 'external.json', 'source.zip')}}, exclusive=True)


def check(output):
    manifest = load_json(output/'freeze.json')
    for name, sha in manifest['sources'].items():
        if digest(ROOT/name) != sha: raise ValueError('Frozen source changed: '+name)
    for name, sha in manifest['assets'].items():
        if digest(output/name) != sha: raise ValueError('Frozen asset changed: '+name)
    external = load_json(output/'external.json')
    if source_info(Path(external['source'])) != external['source_hashes']:
        raise ValueError('ApeCode bytes changed')
    if digest(external['python']) != external['python_sha256']:
        raise ValueError('Worker Python changed')


def worker(output, key):
    check(output)
    slot = next(p for p in load_json(output/'plan.json') if p['run_key'] == key)
    task = next(t for t in load_json(output/'tasks.json') if t['id'] == slot['task_id'])
    directory = output/'runs'/key
    write_json(directory/'started.json', {'at': utcnow(), **slot}, exclusive=True)
    # The Agent workspace is outside controller/source/reference directories.
    destination = Path(os.environ['LOCALAPPDATA'])/'CodeTrail/process-controller'/output.name/key
    state = start(task, destination, slot['agent'])
    workspace = destination/'workspace'
    protocol, external = load_json(output/'protocol.json'), load_json(output/'external.json')
    executor = IsolatedExecutor(protocol['backend'], directory/'execution', protocol['image'])
    started = time.monotonic()
    sessions = []
    error = None
    try:
        for phase in (('a', 'b') if task['phase_a'] else ('b',)):
            if phase == 'b' and state['phase'] == 'a':
                state = advance(task, destination, state['session_id'])
            phase_task = dict(task)
            if phase == 'a':
                files = task['phase_a']['files']
                phase_task['gold'] = {'required_files': sorted(files), 'lines': [
                    {'path': n, 'line': i, 'text': line, 'source_sha256': h(text)}
                    for n, text in files.items() for i, line in enumerate(text.splitlines(), 1)]}
            policy = {'read_only_paths': [n for n in (task['phase_a']['files'] if phase == 'a' else task['files'])
                                          if phase == 'a' or n not in task['editable_files']]}
            write_json(workspace/'.pico/policy.json', policy)
            scripted = ScriptedHTTP(actions(task, slot['agent'], phase), phase)
            with patch.dict(os.environ, {'BIGMODEL_API_KEY': 'offline-integration-not-a-provider-credential'}), \
                 patch('urllib.request.urlopen', side_effect=scripted):
                args = (phase_task, workspace, directory/('session-'+phase), executor, phase, state['session_id'])
                if slot['agent'] == 'codetrail':
                    result = run_codetrail(*args, lambda d: make_codetrail_client())
                else:
                    result = run_apecode(*args, lambda d, hook: BigModelChatModel(d, hook),
                                         Path(external['source']), protocol['at'])
            sessions.append(result)
            if not result['completed'] or result['agent_error']:
                raise RuntimeError(result['agent_error'] or 'Session did not complete')
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    final = snapshot(workspace)
    valid = scope_valid(task, final)
    implementation = {n: (workspace/n).read_bytes() for n in task['editable_files']
                      if final.get(n) not in (None, 'REPARSE_POINT')}
    oracle = verify(task, implementation, directory/'independent-final', executor)
    functional = bool(valid and oracle['accepted'])
    workflow = bool(functional and not error and sessions and sessions[-1].get('baseline_test_observed')
                    and sessions[-1].get('final_test_observed'))
    tools = sum(s['tool_steps'] for s in sessions)
    row = {**slot, 'mode': protocol['mode'], 'sessions': sessions, 'session_count': len(sessions),
        'functional_passed': functional, 'workflow_passed': workflow, 'scope_valid': valid,
        'external_verifier': oracle, 'tool_steps': tools, 'total_tokens': None,
        'failure_category': 'controller_or_agent_error' if error else 'scope_violation' if not valid else
            'external_test_failure' if not functional else 'workflow_missing' if not workflow else 'success',
        'error': error, 'elapsed_ms': round((time.monotonic()-started)*1000),
        'fatal_execution_error': executor.poisoned,
        'initial_sha256': task['sha256'], 'final_sha256': final, 'freeze_sha256': digest(output/'freeze.json'),
        'api_calls': 0, 'benchmark_runs': 0, 'model_requests_are_scripted': True}
    changes = []
    for name in task['editable_files']:
        text = (workspace/name).read_text(encoding='utf-8') if final.get(name) not in (None, 'REPARSE_POINT') else ''
        changes.extend(difflib.unified_diff(task['files'][name].splitlines(True), text.splitlines(True),
                                          fromfile='a/'+name, tofile='b/'+name))
    (directory/'changes.diff').write_text(''.join(changes), encoding='utf-8', newline='\n')
    shutil.copytree(workspace, directory/'final-workspace', symlinks=True)
    check(output)
    write_json(directory/'result.json', row, exclusive=True)


def report(output):
    plan, rows = load_json(output/'plan.json'), []
    for slot in plan:
        directory = output/'runs'/slot['run_key']
        if (directory/'result.json').exists(): rows.append(load_json(directory/'result.json'))
        elif directory.exists(): rows.append({**slot, 'functional_passed': False, 'workflow_passed': False,
            'failure_category': 'interrupted_without_result', 'total_tokens': None})
    summary = {'mode': 'offline_scripted_integration_not_benchmark', 'planned_scenarios': len(plan),
        'started_scenarios': len(rows), 'sessions': sum(r.get('session_count', 0) for r in rows),
        'mechanism_functional_passes': sum(r['functional_passed'] for r in rows),
        'mechanism_workflow_passes': sum(r['workflow_passed'] for r in rows), 'api_calls': 0,
        'new_benchmark_tasks': 0, 'new_model_benchmark_runs': 0, 'overall_score': None,
        'efficiency': efficiency(rows), 'complete': len(rows) == len(plan) and all('sessions' in r for r in rows),
        'note': 'Scripted requests have unknown/non-billable token usage, never entered into model rankings.'}
    write_json(output/'summary.json', summary)
    write_json(output/'runs.json', rows)
    with (output/'runs.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, ['run_key', 'agent', 'task_id', 'repeat', 'session_count',
            'functional_passed', 'workflow_passed', 'failure_category', 'tool_steps', 'total_tokens', 'elapsed_ms'], extrasaction='ignore')
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps(summary), flush=True)
    return summary


def run(output):
    check(output)
    external = load_json(output/'external.json')
    protocol = load_json(output/'protocol.json')
    if protocol['backend'] == 'docker':
        from pico.evaluation.public_benchmark import docker_preflight
        status = docker_preflight()
        if not status['ready']:
            write_json(output/'blocked.json', {'preflight': status, 'formal_runs': 0}, exclusive=True)
            return report(output)
    for slot in load_json(output/'plan.json'):
        directory = output/'runs'/slot['run_key']
        if directory.exists():
            if not (directory/'result.json').exists():
                raise RuntimeError('Unresolved interrupted slot retained; no replacement')
            continue
        directory.mkdir(parents=True)
        write_json(directory/'dispatched.json', {'at': utcnow(), **slot}, exclusive=True)
        command = [external['python'], '-X', 'utf8', '-u', str(Path(__file__).resolve()),
                   '--output', str(output), '--worker', slot['run_key']]
        print('START '+slot['run_key'], flush=True)
        with (directory/'worker.log').open('xb') as log:
            process = subprocess.Popen(command, cwd=ROOT, env=clean_environment(), stdout=log, stderr=subprocess.STDOUT)
            try: process.wait(timeout=protocol['worker_deadline_seconds'])
            except subprocess.TimeoutExpired:
                kill_tree(process)
            if protocol['backend'] == 'docker': cleanup_abandoned(directory/'execution')
        report(output)
        if not (directory/'result.json').exists():
            raise RuntimeError('Worker interrupted; original evidence retained')
        if load_json(directory/'result.json').get('fatal_execution_error'):
            write_json(output/'stopped.json', {'trigger': slot['run_key'],
                'reason': 'execution_cleanup_unresolved', 'remaining': 'unstarted'}, exclusive=True)
            break
    return report(output)


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--freeze', action='store_true')
    p.add_argument('--run', action='store_true')
    p.add_argument('--worker')
    p.add_argument('--apecode-source', type=Path, default=ROOT/'artifacts/cross-agent-sources/apecode')
    p.add_argument('--python', type=Path)
    p.add_argument('--backend', choices=['appcontainer', 'docker'], default='appcontainer')
    p.add_argument('--image')
    a = p.parse_args()
    output = a.output.resolve()
    if not output.is_relative_to(ROOT/'artifacts'):
        p.error('Controller evidence must be in the project artifacts directory')
    if a.freeze:
        if not a.python: p.error('--python requires a pinned Python >=3.13')
        freeze(output, a.apecode_source.resolve(), a.python.resolve(), a.backend, a.image)
    elif a.worker: worker(output, a.worker)
    elif a.run: run(output)
    else: report(output)
