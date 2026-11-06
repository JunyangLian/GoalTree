"""Real standard-API availability/protocol checks; no scripted model responses."""
import argparse
import csv
import difflib
import json
import os
from pathlib import Path
import random
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.bigmodel_benchmark_adapter import BASE_URL, MODEL, OPTIONS, CONFIG
from scripts.glm_live_support import Budget, LiveCodeTrailClient, LiveApeCodeModel, initialize_budget
from scripts.run_cross_agent_pilot import source_info
from pico.evaluation.repair_suite import write_json, utcnow, load_json, digest
from pico.evaluation.agent_session import run_codetrail, run_apecode
from pico.evaluation.cross_agent import snapshot, scope_valid, clean_environment, kill_tree
from pico.evaluation.isolated_execution import IsolatedExecutor, verify
from pico.evaluation.process_scenario import start

WINDOW = ROOT/'artifacts/glm-budget-window-2026-11-06'
TASK_IDS = ('dev_interval_end', 'dev_sku_contract')


def protocol_probe(output):
    if not WINDOW.exists(): initialize_budget(WINDOW)
    output.mkdir(parents=True, exist_ok=False)
    budget = Budget(WINDOW)
    model = LiveApeCodeModel(output, None, budget)
    nonce = 'glm-native-echo-20261106'
    tools = [{'type': 'function', 'function': {'name': 'echo', 'description': 'Echo a supplied nonce.',
        'parameters': {'type': 'object', 'properties': {'nonce': {'type': 'string'}},
                       'required': ['nonce'], 'additionalProperties': False}}}]
    messages = [{'role': 'user', 'content': 'Protocol check, not a benchmark. Call echo exactly once with nonce '
        +nonce+'. After receiving the tool result, give that exact nonce as your final answer.'}]
    error = None
    first = second = None
    try:
        first = model.complete(messages=messages, tools=tools)
        calls = first.get('tool_calls') or []
        if len(calls) != 1 or calls[0]['function']['name'] != 'echo' or json.loads(
                calls[0]['function']['arguments']) != {'nonce': nonce}:
            raise RuntimeError('Expected one exact echo native call')
        messages += [first, {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': nonce}]
        second = model.complete(messages=messages, tools=tools)
        if second.get('tool_calls') or nonce not in (second.get('content') or ''):
            raise RuntimeError('Final answer did not reflect the actual tool result')
    except Exception as exc: error = f'{type(exc).__name__}: {exc}'
    result = {'at': utcnow(), 'ok': error is None, 'error': error, 'real_model_requests': len(model.requests),
        'model': MODEL, 'route': 'direct', 'thinking_replayed': bool(first and isinstance(first.get('reasoning_content'), str)),
        'tool_result_roundtrip': bool(second and nonce in (second.get('content') or '')), 'formal_runs': 0}
    write_json(output/'status.json', result, exclusive=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def freeze(output, source, python, probe):
    if not load_json(probe/'status.json')['ok']: raise RuntimeError('Real protocol preflight not passed')
    assets = ROOT/'artifacts/processbench-v0.1-assets-2026-11-06'
    lock = load_json(assets/'asset-lock.json')
    for name, sha in lock['assets'].items():
        if digest(assets/name) != sha: raise ValueError('ProcessBench assets changed')
    tasks = [t for t in load_json(assets/'tasks.json') if t['id'] in TASK_IDS]
    assert len(tasks) == 2 and all(t['split'] == 'dev' and not t['phase_a'] for t in tasks)
    # Reference data is only hashed for integrity, never parsed, copied or
    # installed. Only faulty fixtures, tests and proxy evidence are selected.
    output.mkdir(parents=True, exist_ok=False)
    plan = [{'agent': a, 'task_id': t, 'repeat': 1} for t in TASK_IDS for a in ('codetrail', 'apecode')]
    random.Random(20261106).shuffle(plan)
    for i, row in enumerate(plan, 1): row['run_key'] = f"{i:02d}-{row['agent']}-{row['task_id']}"
    protocol = {'at': utcnow(), 'mode': 'real_glm_standard_api_development_pilot', 'model': CONFIG,
        'task_ids': list(TASK_IDS), 'agents': ['codetrail', 'apecode'], 'repetitions': 1,
        'planned_runs': 4, 'concurrency': 1, 'max_tools': 20, 'max_model_requests': 30,
        'worker_deadline_seconds': 645, 'network_route': 'direct', 'backend': 'appcontainer',
        'budget_window': str(WINDOW), 'max_window_spend_cny': '1',
        'exposure': '2 synthetic dev tasks; author asset qualification and offline development, not held-out or public repository tasks',
        'labels': 'exact source proxies; independent human review pending; no semantic-memory score',
        'policy': 'No replacement of started failures; original tests/scopes unchanged; no model substitution',
        'probe_status_sha256': digest(probe/'status.json')}
    external = {'source': str(source.resolve()), 'source_hashes': source_info(source),
        'python': str(python.resolve()), 'python_sha256': digest(python),
        'version': subprocess.run([str(python), '--version'], capture_output=True, check=True, text=True).stdout.strip()}
    if not external['version'].startswith('Python 3.13.'): raise ValueError('Unified Python3.13 required')
    for name, data in [('tasks.json', tasks), ('protocol.json', protocol), ('plan.json', plan), ('external.json', external)]:
        write_json(output/name, data, exclusive=True)
    files = list((ROOT/'pico').rglob('*.py')) + [Path(__file__).resolve(), ROOT/'scripts/glm_live_support.py',
        ROOT/'scripts/bigmodel_benchmark_adapter.py', ROOT/'scripts/run_cross_agent_pilot.py']
    hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in files}
    with zipfile.ZipFile(output/'source.zip', 'x', zipfile.ZIP_DEFLATED) as z:
        for name in hashes: z.write(ROOT/name, name)
        for name in external['source_hashes']: z.write(source/name, 'upstream/apecode/'+name)
    write_json(output/'freeze.json', {'at': utcnow(), 'sources': hashes,
        'assets': {n: digest(output/n) for n in ('tasks.json', 'protocol.json', 'plan.json', 'external.json', 'source.zip')}}, exclusive=True)


def check(output):
    frozen = load_json(output/'freeze.json')
    for name, sha in frozen['sources'].items():
        if digest(ROOT/name) != sha: raise ValueError('Frozen source changed: '+name)
    for name, sha in frozen['assets'].items():
        if digest(output/name) != sha: raise ValueError('Frozen asset changed: '+name)
    external = load_json(output/'external.json')
    if source_info(Path(external['source'])) != external['source_hashes']:
        raise ValueError('ApeCode source changed')
    if digest(external['python']) != external['python_sha256']: raise ValueError('Worker Python changed')


def worker(output, key):
    check(output)
    slot = next(p for p in load_json(output/'plan.json') if p['run_key'] == key)
    task = next(t for t in load_json(output/'tasks.json') if t['id'] == slot['task_id'])
    directory = output/'runs'/key
    write_json(directory/'started.json', {'at': utcnow(), **slot}, exclusive=True)
    state = start(task, Path(os.environ['LOCALAPPDATA'])/'CodeTrail/glm-live'/output.name/key, slot['agent'])
    workspace = Path(os.environ['LOCALAPPDATA'])/'CodeTrail/glm-live'/output.name/key/'workspace'
    write_json(workspace/'.pico/policy.json', {'read_only_paths': [n for n in task['files'] if n not in task['editable_files']]})
    protocol, external = load_json(output/'protocol.json'), load_json(output/'external.json')
    executor, budget = IsolatedExecutor('appcontainer', directory/'execution'), Budget(Path(protocol['budget_window']))
    at = time.monotonic()
    error = None
    result = {}
    try:
        args = (task, workspace, directory/'session-b', executor, 'b', state['session_id'])
        if slot['agent'] == 'codetrail': result = run_codetrail(*args, lambda d: LiveCodeTrailClient(d, budget))
        else: result = run_apecode(*args, lambda d, hook: LiveApeCodeModel(d, hook, budget),
                                  Path(external['source']), protocol['at'])
        error = result.get('agent_error')
    except Exception as exc: error = f'{type(exc).__name__}: {exc}'
    final = snapshot(workspace)
    scope = scope_valid(task, final)
    implementation = {n: (workspace/n).read_bytes() for n in task['editable_files'] if final.get(n) not in (None, 'REPARSE_POINT')}
    oracle = verify(task, implementation, directory/'independent-final', executor)
    functional = bool(scope and oracle['accepted'])
    workflow = bool(functional and not error and result.get('completed') and
                    result.get('baseline_test_observed') and result.get('final_test_observed'))
    row = {**slot, 'mode': protocol['mode'], 'session': result, 'functional_passed': functional,
        'workflow_passed': workflow, 'scope_valid': scope, 'external_verifier': oracle,
        'initial_sha256': task['sha256'], 'final_sha256': final, 'elapsed_ms': round((time.monotonic()-at)*1000),
        'error': error, 'failure_category': 'model_or_controller_error' if error else 'scope_violation' if not scope else
            'external_test_failure' if not functional else 'workflow_missing' if not workflow else 'success',
        'freeze_sha256': digest(output/'freeze.json'), 'tool_steps': result.get('tool_steps'),
        'input_tokens': result.get('input_tokens'), 'output_tokens': result.get('output_tokens')}
    diff = []
    for name in task['editable_files']:
        text = (workspace/name).read_text(encoding='utf-8') if final.get(name) not in (None, 'REPARSE_POINT') else ''
        diff.extend(difflib.unified_diff(task['files'][name].splitlines(True), text.splitlines(True), fromfile='a/'+name, tofile='b/'+name))
    (directory/'changes.diff').write_text(''.join(diff), encoding='utf-8', newline='\n')
    shutil.copytree(workspace, directory/'final-workspace', symlinks=True)
    check(output)
    write_json(directory/'result.json', row, exclusive=True)


def report(output):
    plan = load_json(output/'plan.json')
    rows = []
    for slot in plan:
        d = output/'runs'/slot['run_key']
        if (d/'result.json').exists(): rows.append(load_json(d/'result.json'))
        elif d.exists(): rows.append({**slot, 'functional_passed': False, 'workflow_passed': False,
            'failure_category': 'interrupted_without_result'})
    charged, pending, events = Budget(WINDOW).state()
    summary = {'mode': 'real_glm_standard_api_development_pilot', 'planned': len(plan), 'started': len(rows),
        'functional': sum(r['functional_passed'] for r in rows), 'workflow': sum(r['workflow_passed'] for r in rows),
        'complete': len(rows) == len(plan) and all('session' in r for r in rows),
        'tariff_upper_estimate_window_cny': str(charged), 'unknown_usage_reservations_cny': str(pending),
        'budget_cny': '1', 'actual_account_debit_cny': None,
        'settled_requests_including_preflight': sum(e['event'] == 'settled' for e in events),
        'by_agent': {a: {'runs': len(g), 'functional': sum(r['functional_passed'] for r in g),
                        'workflow': sum(r['workflow_passed'] for r in g)}
                     for a in ('codetrail', 'apecode') if (g := [r for r in rows if r['agent'] == a])}}
    write_json(output/'summary.json', summary)
    write_json(output/'runs.json', rows)
    with (output/'runs.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, ['run_key', 'agent', 'task_id', 'functional_passed', 'workflow_passed',
            'tool_steps', 'input_tokens', 'output_tokens', 'elapsed_ms', 'failure_category'], extrasaction='ignore', lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps(summary), flush=True)
    return summary


def run(output):
    check(output)
    external, protocol = load_json(output/'external.json'), load_json(output/'protocol.json')
    for slot in load_json(output/'plan.json'):
        _, _, ledger = Budget(WINDOW).state()
        if any(e['event'] == 'denied' for e in ledger):
            write_json(output/'budget-stopped.json', {'at': utcnow(), 'remaining': 'unstarted',
                'reason': 'A prior request was denied by the shared budget; no new slot dispatched'})
            break
        directory = output/'runs'/slot['run_key']
        if directory.exists():
            if not (directory/'result.json').exists(): raise RuntimeError('Interrupted run retained; no replacement')
            continue
        directory.mkdir(parents=True)
        env = clean_environment(); env['BIGMODEL_API_KEY'] = os.environ['BIGMODEL_API_KEY']
        command = [external['python'], '-X', 'utf8', '-u', str(Path(__file__).resolve()), '--output', str(output), '--worker', slot['run_key']]
        print('START '+slot['run_key'], flush=True)
        with (directory/'worker.log').open('xb') as log:
            process = subprocess.Popen(command, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            try: process.wait(timeout=protocol['worker_deadline_seconds'])
            except subprocess.TimeoutExpired: kill_tree(process)
        report(output)
        if not (directory/'result.json').exists(): raise RuntimeError('Interrupted record retained')
        error = load_json(directory/'result.json').get('error') or ''
        transport_file = directory/'session-b/transport.jsonl'
        fatal_transport = False
        if transport_file.exists():
            entries = [json.loads(s) for s in transport_file.read_text(encoding='utf-8').splitlines()]
            fatal_transport = any(e.get('status', e.get('status_code')) in {401, 402, 403, 429} for e in entries)
        if fatal_transport or any(marker in error for marker in ('BudgetExceeded', 'HTTP401', 'HTTP402', 'HTTP403', 'HTTP429')):
            write_json(output/'stopped.json', {'run_key': slot['run_key'], 'reason': error, 'remaining': 'unstarted'}, exclusive=True)
            break
    report(output)


def availability(output):
    output.mkdir(parents=True, exist_ok=False)
    key = os.environ['BIGMODEL_API_KEY']
    payload = {'model': MODEL, 'messages': [{'role': 'user', 'content': 'Non-task connection check. Reply READY.'}],
        'temperature': 1, 'top_p': .95, 'max_tokens': 256, 'stream': False, **OPTIONS}
    write_json(output/'request.json', payload, exclusive=True)
    request = urllib.request.Request(BASE_URL+'/chat/completions', data=json.dumps(payload).encode(),
        headers={'Authorization': 'Bearer '+key, 'Content-Type': 'application/json',
                 'User-Agent': 'CodeTrail-GLM-availability/1.0'})
    started = time.monotonic()
    transport_error = None
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=60) as response:
            raw = response.read().decode()
            status = response.status
    except urllib.error.HTTPError as exc:
        raw, status = exc.read().decode(errors='replace'), exc.code
    except Exception as exc:
        raw, status = '{}', None
        transport_error = {'type': type(exc).__name__, 'reason': str(getattr(exc, 'reason', exc)).replace(key, '<redacted>')}
    raw = raw.replace(key, '<redacted>')
    (output/'response.json').write_text(raw, encoding='utf-8')
    try: data = json.loads(raw)
    except ValueError: data = {}
    choice = (data.get('choices') or [{}])[0]
    message = choice.get('message') or {}
    result = {'at': utcnow(), 'route': 'direct', 'http_status': status, 'model': data.get('model'),
        'usage': data.get('usage'), 'thinking_present': isinstance(message.get('reasoning_content'), str),
        'finish_reason': choice.get('finish_reason'), 'error': data.get('error'),
        'transport_error': transport_error, 'elapsed_ms': round((time.monotonic()-started)*1000),
        'model_matches': data.get('model') == MODEL, 'model_requests': 1, 'formal_runs': 0,
        'scope': 'real standard API availability only; no repair task started'}
    write_json(output/'availability.json', result, exclusive=True)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--run', action='store_true')
    p.add_argument('--availability', action='store_true')
    p.add_argument('--probe', action='store_true')
    p.add_argument('--freeze', action='store_true')
    p.add_argument('--worker')
    p.add_argument('--python', type=Path)
    p.add_argument('--apecode-source', type=Path, default=ROOT/'artifacts/cross-agent-sources/apecode')
    p.add_argument('--probe-output', type=Path, default=ROOT/'artifacts/glm-native-protocol-2026-11-06')
    a = p.parse_args()
    output = a.output.resolve()
    if not output.is_relative_to(ROOT/'artifacts'): p.error('Evidence must remain under artifacts')
    if a.availability: availability(output)
    elif a.probe: protocol_probe(output)
    elif a.freeze:
        if not a.python: p.error('--python requires Python3.13')
        freeze(output, a.apecode_source.resolve(), a.python.resolve(), a.probe_output.resolve())
    elif a.worker: worker(output, a.worker)
    elif a.run: run(output)
    else: report(output)
