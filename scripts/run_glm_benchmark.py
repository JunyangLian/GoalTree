"""Frozen CTRepair30 known-regression comparison; real GLM API only."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.benchmark_score import score, VERSION
from pico.evaluation.cross_agent import clean_environment, kill_tree, snapshot
from pico.evaluation.isolated_execution import IsolatedExecutor, verify
from pico.evaluation.repair_suite import load_json, write_json, digest, utcnow
from scripts.bigmodel_benchmark_adapter import CONFIG
from scripts.glm_live_support import Budget, RATES
from scripts.run_cross_agent_pilot import source_info
from scripts.run_glm_live_pilot import worker, check
from scripts.glm_test_executor import PinnedTestExecutor


def prepare(output, source, python, test_python=None, prior_budget=None):
    output.mkdir(parents=True, exist_ok=False)
    original = ROOT/'benchmarks/final_agent_v1/tasks.json'
    tasks = load_json(original)['tasks']
    assert len(tasks) == len({t['id'] for t in tasks}) == 30
    selected = []
    for task in tasks:
        assert task['sha256'] == {n: hashlib.sha256(v.encode()).hexdigest() for n, v in task['files'].items()}
        # No old evidence proxy is silently promoted into a new reliable label.
        # Reference patches and source metadata are not installed as model input.
        selected.append({**task, 'phase_a': None, 'split': 'known_regression',
            'development_exposure_current': 'Previously evaluated; not a new holdout',
            'gold': {'required_files': [], 'lines': [], 'status': 'not scored; historical evidence labels require review'}})
    write_json(output/'tasks.json', selected, exclusive=True)
    plan = []
    ordered = selected.copy()
    random.Random(2026110602).shuffle(ordered)
    core = []
    counts = {}
    for task in ordered:
        category = task['category']
        if counts.get(category, 0) < 3:
            core.append(task)
            counts[category] = counts.get(category, 0)+1
    core_ids = {t['id'] for t in core}
    ordered = core+[t for t in ordered if t['id'] not in core_ids]
    for i, task in enumerate(ordered):
        agents = ['codetrail', 'apecode'] if i % 2 == 0 else ['apecode', 'codetrail']
        for agent in agents:
            plan.append({'agent': agent, 'task_id': task['id'], 'category': task['category'],
                'repeat': 1, 'run_key': f'{len(plan)+1:03d}-{agent}-{task["id"]}'})
    protocol = {'at': utcnow(), 'mode': 'CTRepair30-v1-known-regression-real-glm', 'model': CONFIG,
        'score_version': VERSION, 'score_formula': '100 * mean_category(mean_task(verified_success))',
        'categories': ['single_file', 'cross_file', 'unknown_location', 'regression_compatibility'],
        'repetitions': 1, 'planned_runs': 60, 'concurrency': 1, 'max_tools': 20,
        'max_model_requests': 30, 'worker_deadline_seconds': 645, 'network_route': 'direct',
        'backend': 'appcontainer', 'budget_window': str((output/'budget').resolve()),
        'max_spend_cny': '5', 'max_total_tokens': 2000000,
        'core_task_ids': [t['id'] for t in core],
        'nested_plan': 'First 12 tasks, 3 per category, are a separately reported frozen core. All 30 remain the full plan; report both, regardless of score.',
        'task_source_sha256': digest(original), 'original_tasks': str(original.relative_to(ROOT)),
        'safety_score': 'unassessed; no new adversarial suite',
        'context_score': 'unassessed; no validated equivalent-evidence gold for this batch',
        'stability_score': 'unassessed; one repetition only',
        'policy': 'No changed gold, no replaced failure, no host shell fallback, no model substitution',
        'stop_policy': 'Stop on money/token guard, auth/balance/rate failure or unresolved execution cleanup. Unstarted slots stay unknown.'}
    test_python = Path(test_python or sys.executable).resolve()
    protocol['test_runtime'] = {'python':str(test_python),'python_sha256':digest(test_python),
        'version':subprocess.run([str(test_python),'--version'],capture_output=True,check=True,text=True).stdout.strip(),
        'boundary':'Trusted fixed-command launcher, actual tests inside original AppContainer backend; no ACL changes'}
    external = {'source': str(source.resolve()), 'source_hashes': source_info(source),
        'python': str(python.resolve()), 'python_sha256': digest(python),
        'version': subprocess.run([str(python), '--version'], capture_output=True, check=True, text=True).stdout.strip()}
    if not external['version'].startswith('Python 3.13.'): raise ValueError('Unified Python3.13 required')
    write_json(output/'plan.json', plan, exclusive=True)
    write_json(output/'protocol.json', protocol, exclusive=True)
    write_json(output/'external.json', external, exclusive=True)
    write_json(output/'budget/budget.json', {'max_spend_cny': '5', 'max_total_tokens': 2000000,
        'rates': RATES, 'concurrency': 1, 'unknown_usage': 'Retain both money and token reservation'}, exclusive=True)
    if prior_budget:
        previous = Path(prior_budget).resolve()
        previous_events = previous/'ledger.jsonl'
        (output/'budget/ledger.jsonl').write_bytes(previous_events.read_bytes())
        write_json(output/'prior-window-import.json',{'source':str(previous),'sha256':digest(previous_events),
            'note':'All prior preflight usage/reservations count against this same 2M/5CNY allocation'},exclusive=True)
    # Both adapters' actual protocol preflights run in this same window before dispatch.
    sources = list((ROOT/'pico').rglob('*.py')) + [ROOT/'scripts'/n for n in
        ('run_glm_benchmark.py', 'run_glm_live_pilot.py', 'glm_live_support.py',
         'bigmodel_benchmark_adapter.py', 'run_cross_agent_pilot.py', 'glm_test_executor.py', 'glm_verification_bridge.py')]
    hashes = {p.relative_to(ROOT).as_posix(): digest(p) for p in sources}
    with zipfile.ZipFile(output/'source.zip', 'x', zipfile.ZIP_DEFLATED) as z:
        for name in hashes: z.write(ROOT/name, name)
        for name in external['source_hashes']: z.write(source/name, 'upstream/apecode/'+name)
    write_json(output/'freeze.json', {'at': utcnow(), 'sources': hashes, 'assets':
        {n: digest(output/n) for n in ('tasks.json', 'plan.json', 'protocol.json', 'external.json', 'budget/budget.json', 'source.zip')}}, exclusive=True)
    check(output)
    return report(output)


def reverify(output, slot):
    directory = output/'runs'/slot['run_key']
    row = load_json(directory/'result.json')
    task = next(t for t in load_json(output/'tasks.json') if t['id'] == slot['task_id'])
    target = directory/'second-verification'
    if (directory/'second-verification-result.json').exists():
        return
    assert snapshot(directory/'final-workspace') == row['final_sha256']
    implementation = {n: (directory/'final-workspace'/n).read_bytes() for n in task['editable_files']
                      if row['final_sha256'].get(n) not in (None, 'REPARSE_POINT')}
    verify(task, implementation, target, PinnedTestExecutor('appcontainer', directory/'second-execution',
        load_json(output/'protocol.json')['test_runtime']))


def report(output):
    rows = []
    tasks = {t['id']: t for t in load_json(output/'tasks.json')}
    for slot in load_json(output/'plan.json'):
        directory = output/'runs'/slot['run_key']
        if (directory/'result.json').exists():
            result = load_json(directory/'result.json')
            second = directory/'second-verification-result.json'
            row = {**slot, **result, 'audit_valid': second.exists() and load_json(second)['accepted'],
                'origin_kind': tasks[slot['task_id']]['origin']['kind']}
            session = result.get('session') or {}
            row['model_requests'] = session.get('model_requests')
            row['baseline_test_observed'] = session.get('baseline_test_observed')
            row['final_test_observed'] = session.get('final_test_observed')
            row.pop('session', None)
            rows.append(row)
        elif (directory/'controller-terminal.json').exists():
            rows.append({**slot, **load_json(directory/'controller-terminal.json'), 'audit_valid': False,
                'functional_passed': False, 'workflow_passed': False, 'scope_valid': False,
                'input_tokens': None, 'output_tokens': None, 'tool_steps': None})
    summary = score(load_json(output/'plan.json'), rows)
    core_ids = set(load_json(output/'protocol.json')['core_task_ids'])
    summary['core_12'] = score([p for p in load_json(output/'plan.json') if p['task_id'] in core_ids],
                               [r for r in rows if r['task_id'] in core_ids])
    gate = Budget(output/'budget')
    money, unknown, events = gate.state()
    tokens, pending_tokens = gate.token_state()
    summary['budget'] = {'known_tariff_upper_cny': str(money), 'unknown_reserved_cny': str(unknown),
        'known_total_tokens': tokens, 'unknown_reserved_tokens': pending_tokens,
        'max_cny': '5', 'max_total_tokens': 2000000, 'actual_account_debit_cny': None}
    summary['at'] = utcnow()
    summary['run_records'] = len(rows)
    write_json(output/'summary.json', summary)
    write_json(output/'runs.json', rows)
    with (output/'runs.csv').open('w', encoding='utf-8', newline='') as f:
        keys = ['run_key', 'agent', 'task_id', 'category', 'repeat', 'functional_passed', 'workflow_passed',
            'audit_valid', 'origin_kind', 'tool_steps', 'input_tokens', 'output_tokens', 'elapsed_ms', 'failure_category']
        writer = csv.DictWriter(f, keys, extrasaction='ignore', lineterminator='\n')
        writer.writeheader(); writer.writerows(rows)
    print(json.dumps({'recorded':len(rows), 'planned':60, 'tokens':tokens,
        'tariff_upper_cny':str(money), 'scores':{a:v['verified_score'] for a,v in summary['agents'].items()}}), flush=True)
    return summary


def preflight(output):
    """One real native call/result roundtrip per adapter, billed in the frozen window."""
    from scripts.glm_live_support import LiveApeCodeModel, LiveCodeTrailClient
    directory = output/'preflight'
    if directory.exists():
        if not (directory/'passed.json').exists(): raise RuntimeError('Failed preflight retained; no replacement')
        return
    directory.mkdir()
    gate = Budget(output/'budget')
    events = []
    client = LiveCodeTrailClient(directory/'codetrail', gate)
    client.on_wire_event = lambda e: events.append(e)
    # The same public native adapter as the live repair worker is used.
    tools = {'read_file': {'description': 'Read a text file.', 'schema': {'path': 'str'}}}
    client.begin_native_run(tools, 'Protocol check only.', 'Call read_file with path probe.txt then report READY.', None)
    # A native echo through the independent ApeCode transport confirms thinking replay.
    model = LiveApeCodeModel(directory/'apecode', None, gate)
    schema = [{'type':'function','function':{'name':'echo','description':'Echo a nonce.',
        'parameters':{'type':'object','properties':{'nonce':{'type':'string'}},'required':['nonce']}}}]
    nonce = 'ctrepair30-protocol-ready'
    messages = [{'role':'user','content':'Call echo exactly once with nonce '+nonce+'. After the result repeat that nonce.'}]
    try:
        # A read-only standard client completion verifies the CodeTrail format without fixture injection.
        response = client.complete('Call read_file with path probe.txt.', 4096)
        write_json(directory/'codetrail-wire.json', events, exclusive=True)
        if not getattr(response, 'action_kind', None) == 'tool': raise RuntimeError('CodeTrail native protocol preflight failed')
        first = model.complete(messages=messages, tools=schema)
        calls = first.get('tool_calls') or []
        if len(calls)!=1 or calls[0]['function']['name']!='echo' or json.loads(calls[0]['function']['arguments'])!={'nonce':nonce}:
            raise RuntimeError('Native echo call did not match')
        messages += [first,{'role':'tool','tool_call_id':calls[0]['id'],'content':nonce}]
        second = model.complete(messages=messages,tools=schema)
        if nonce not in (second.get('content') or '') or second.get('tool_calls'): raise RuntimeError('Tool return preflight failed')
    except Exception as e:
        write_json(directory/'failed.json', {'type':type(e).__name__, 'error':str(e)}, exclusive=True)
        raise
    write_json(directory/'passed.json', {'at':utcnow(), 'real_requests':3, 'exact_model':CONFIG['model'],
        'native_tool_call':True,'tool_result_roundtrip':True}, exclusive=True)


def run(output):
    check(output)
    preflight(output)
    external, protocol = load_json(output/'external.json'), load_json(output/'protocol.json')
    for slot in load_json(output/'plan.json'):
        check(output)
        if any(e['event']=='denied' for e in Budget(output/'budget').state()[2]):
            write_json(output/'stopped.json', {'at':utcnow(), 'reason':'money_or_token_budget', 'remaining':'unstarted_unknown'}, exclusive=True)
            break
        directory = output/'runs'/slot['run_key']
        if directory.exists():
            if not (directory/'result.json').exists(): raise RuntimeError('Interrupted slot retained; no replacement')
            reverify(output, slot)
            continue
        directory.mkdir(parents=True)
        write_json(directory/'dispatched.json', {'at':utcnow(),**slot}, exclusive=True)
        env=clean_environment(); env['BIGMODEL_API_KEY']=os.environ['BIGMODEL_API_KEY']
        command=[external['python'],'-X','utf8','-u',str(Path(__file__).resolve()),'--output',str(output),'--worker',slot['run_key']]
        print('START '+slot['run_key'],flush=True)
        with (directory/'worker.log').open('xb') as log:
            p=subprocess.Popen(command,cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
            try: p.wait(timeout=protocol['worker_deadline_seconds'])
            except subprocess.TimeoutExpired: kill_tree(p)
        if not (directory/'result.json').exists():
            write_json(directory/'controller-terminal.json', {'at':utcnow(), 'failure_category':'worker_incomplete',
                'exit_code':p.returncode}, exclusive=True)
            report(output)
            write_json(output/'stopped.json', {'at':utcnow(), 'reason':'worker_incomplete', 'remaining':'unstarted_unknown'},exclusive=True)
            break
        reverify(output, slot)
        report(output)
        error=load_json(directory/'result.json').get('error') or ''
        transport=directory/'session-b/transport.jsonl'
        entries=[json.loads(s) for s in transport.read_text(encoding='utf-8').splitlines()] if transport.exists() else []
        fatal=any(e.get('status',e.get('status_code')) in {401,402,403,429} for e in entries)
        if fatal or any(s in error for s in ('HTTP401','HTTP402','HTTP403','HTTP429','BudgetExceeded','ExecutionUnavailable','cleanup')):
            write_json(output/'stopped.json', {'at':utcnow(),'reason':'provider_or_budget_stop','trigger':slot['run_key'],
                'remaining':'unstarted_unknown'},exclusive=True)
            break
    return report(output)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--prepare',action='store_true')
    p.add_argument('--run',action='store_true')
    p.add_argument('--worker')
    p.add_argument('--python',type=Path)
    p.add_argument('--test-python',type=Path)
    p.add_argument('--prior-budget',type=Path)
    p.add_argument('--apecode-source',type=Path,default=ROOT/'artifacts/cross-agent-sources/apecode')
    a=p.parse_args(); out=a.output.resolve()
    if not out.is_relative_to(ROOT/'artifacts'): p.error('Evidence must remain under artifacts')
    if a.prepare:
        if not a.python: p.error('Pinned worker Python required')
        prepare(out,a.apecode_source.resolve(),a.python.resolve(),a.test_python,a.prior_budget)
    elif a.worker:
        runtime=load_json(out/'protocol.json')['test_runtime']
        worker(out,a.worker,lambda backend,evidence: PinnedTestExecutor(backend,evidence,runtime))
    elif a.run: run(out)
    else: report(out)
