"""Explicit official-provider fork of the frozen pilot, capped at CNY2.50."""
import argparse
import copy
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from scripts import run_cross_agent_pilot as base
from scripts import apecode_pilot_adapter as adapter
from scripts.deepseek_pilot_support import BASE_URL,MODEL,WIRE_OPTIONS,Budget,BudgetLimit,DeepSeekChatModel,balance
from pico.evaluation.repair_suite import RecordingClient,append_json,digest,load_json,utcnow,write_json
from pico.evaluation.cross_agent import clean_environment,kill_tree,process_metrics,snapshot
from pico.providers.clients import OpenAICompatibleModelClient
from pico.runtime import Pico,SessionStore
from pico.workspace import WorkspaceContext

PREFLIGHT=ROOT/'artifacts/deepseek-pilot-preflight-2026-11-05'
PROTOCOL=copy.deepcopy(base.PROTOCOL)
PROTOCOL.update(version=3,provider='deepseek_official',model=MODEL,base_url=BASE_URL,
    enable_thinking=None,thinking_budget=None,thinking={'type':'enabled'},reasoning_effort='low',
    temperature=None,temperature_effectiveness='not sent; ignored by provider in Thinking mode',
    max_transport_attempts=1,max_model_requests=30,max_spend_cny='2.50',reserve_cny='.21',
    source_version_limit='Official alias serves V4.1-Flash-1017 per current docs; mutable server revision. Returned model/fingerprint recorded.',
    budget='Account balance checked before every paid request; conservative peak tariff reservation; stop with original failures/unstarted slots retained',
    retry='One transport attempt per logical request on both agents; no replacement started slots.',
    official_freeze='official-freeze.json supplements parent freeze; all formal runs require both',
    comparison_to_previous='Provider/Thinking/retry/budget differ from blocked SiliconFlow batch; no pooled or causal comparison')
PROTOCOL['apecode']['adaptations'][0]='independently recorded official DeepSeek HTTP ChatModel transport'
base.PROTOCOL=PROTOCOL;base.MODEL=MODEL;adapter.PROTOCOL=PROTOCOL


def make_client(directory):
    client=OpenAICompatibleModelClient(MODEL,BASE_URL,os.environ['DEEPSEEK_API_KEY'],None,180,'chat_completions')
    client.top_p=.95;client.native_wire_options=copy.deepcopy(WIRE_OPTIONS);client.max_transport_attempts=1
    client.network_route='direct';budget=Budget(directory.parent.parent,directory);pending={}
    def wire(event):
        if os.environ['DEEPSEEK_API_KEY'] in event.get('body',''):raise RuntimeError('Credential in response; refuse export')
        if event['event']=='request':pending['id']=budget.before(json.loads(event['body']))
        append_json(directory/'wire.jsonl',{'at':utcnow(),**event})
        if event['event']=='response':
            data=json.loads(event['body']);budget.after(pending['id'],data)
            if data.get('model')!=MODEL:raise RuntimeError('Returned model mismatch; no fallback')
    client.on_wire_event=wire
    client.on_transport_event=lambda e:append_json(directory/'transport.jsonl',{'at':utcnow(),**e})
    return client


def codetrail_run(output,task,workspace,directory):
    client=make_client(directory);recorder=RecordingClient(client,directory/'model_io.jsonl')
    context=WorkspaceContext(str(workspace),str(workspace),'-','-','fresh frozen fixture',[],{'README.md':task['files']['README.md']})
    agent=Pico(recorder,context,SessionStore(workspace/'.pico/sessions'),approval_policy='ask',max_steps=20,max_new_tokens=4096,
        max_depth=0,verification_command=base.VERIFY,shell_backend='appcontainer',harness_profile='workbench',embedding_mode='off',
        network_route='direct',secret_env_names={'DEEPSEEK_API_KEY'})
    client.stream=True;client.cancellation=agent.cancellation
    def approve(name,args):
        allowed=base.check_tool_permission(workspace,task['editable_files'],name,args)
        append_json(directory/'approvals.jsonl',{'at':utcnow(),'name':name,'args':args,'allowed':allowed});return allowed
    agent.approve=approve;original=agent.execute_tool;observations=[]
    def observe(name,args):
        before=snapshot(workspace);result=original(name,args)
        value={'at':utcnow(),'name':name,'args':args,'output':result.content,'metadata':result.metadata,
               'exit_code':result.metadata.get('exit_code'),'before':before,'snapshot':snapshot(workspace),
               'rejected':result.metadata.get('tool_status')=='rejected'}
        observations.append(value);append_json(directory/'tools.jsonl',value);return result
    agent.execute_tool=observe;error=None
    try:agent.ask(load_json(output/'prompts.json')[task['id']])
    except Exception as exc:error=f'{type(exc).__name__}: {exc}'
    events=[json.loads(l) for l in (directory/'model_io.jsonl').read_text(encoding='utf-8').splitlines()]
    requests=[{'input_tokens':e.get('usage',{}).get('input_tokens'),'output_tokens':e.get('usage',{}).get('output_tokens'),
               'complete':e['event']=='response'} for e in events if e['event'] in {'response','error'}]
    return {**process_metrics(observations,requests,task,snapshot(workspace)),
        'completed':bool(agent.current_task_state and agent.current_task_state.status=='completed'),'agent_error':error}


original_apecode_run=adapter.run_apecode
def apecode_run(output,task,workspace,directory):
    return original_apecode_run(output,task,workspace,directory,DeepSeekChatModel)
adapter.run_apecode=apecode_run;base.codetrail_run=codetrail_run
# Base worker imports this module by its bare script name. Bind both names to
# one adapted instance; otherwise Python loads an unadapted second module.
sys.modules['apecode_pilot_adapter']=adapter
original_check=base.check


def check(output):
    original_check(output)
    extra=load_json(output/'official-freeze.json')
    if digest(output/'freeze.json')!=extra['parent_freeze_sha256']:raise RuntimeError('Parent freeze changed')
    for n,sha in extra['sources'].items():
        if digest(ROOT/n)!=sha:raise RuntimeError('Official source changed: '+n)
    for n,sha in extra['assets'].items():
        if digest(output/n)!=sha:raise RuntimeError('Official asset changed: '+n)
base.check=check


def probe():
    PREFLIGHT.mkdir(parents=True,exist_ok=True)
    initial=PREFLIGHT/'budget-initial.json'
    if not initial.exists():
        current=balance()
        write_json(initial,{'at':utcnow(),'initial_balance_cny':str(current),'max_spend_cny':str(min(Decimal('2.50'),max(Decimal(0),current-Decimal('.21'))))},exclusive=True)
    results=[]
    for name in ('native','independent'):
        directory=PREFLIGHT/'runs'/(name+'-'+str(time.time_ns()));directory.mkdir(parents=True,exist_ok=False)
        try:
            if name=='native':
                client=make_client(directory)
                client.begin_native_run({'echo_probe':{'schema':{'value':'str'},'description':'Return value.'}},
                    'Availability/protocol probe only, no repository task.',
                    'Call echo_probe with value READY, then after result reply READY.',None)
                first=client.complete('native',4096)
                assert first.action_kind=='tool' and first.action=={'name':'echo_probe','args':{'value':'READY'}}
                client.observe_native_tool('READY');last=client.complete('native',4096)
                assert last.action_kind=='final' and 'READY' in str(last)
            else:
                client=DeepSeekChatModel(directory)
                tools=[{'type':'function','function':{'name':'echo_probe','description':'Return value.','parameters':
                       {'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}}]
                messages=[{'role':'user','content':'Call echo_probe with value READY; after its result reply READY.'}]
                first=client.complete(messages=messages,tools=tools);calls=first.get('tool_calls') or []
                assert len(calls)==1 and calls[0]['function']['name']=='echo_probe'
                messages+=[first,{'role':'tool','tool_call_id':calls[0]['id'],'content':'READY'}]
                last=client.complete(messages=messages,tools=tools)
                assert not last.get('tool_calls') and 'READY' in (last.get('content') or '')
            result={'transport':name,'compatible':True,'scope':'Non-task Thinking + native Tool Calling round trip'}
        except Exception as exc:result={'transport':name,'compatible':False,'error':f'{type(exc).__name__}: {exc}'}
        write_json(directory/'result.json',result,exclusive=True);results.append(result)
        print(json.dumps(result),flush=True)
    write_json(PREFLIGHT/'latest-probe.json',{'at':utcnow(),'compatible':all(r['compatible'] for r in results),'results':results})


def entry_smoke():
    """Exercise the actual base-worker import entry with a separate live toy task."""
    external=load_json(ROOT/'artifacts/cross-agent-pilot-2026-11-05/external-agent.json')
    if sys.version_info<(3,13):
        subprocess.run([external['python'],'-u','-X','utf8',str(Path(__file__).resolve()),
                        '--output',str(PREFLIGHT),'--entry-smoke'],check=True,env=os.environ.copy());return
    import importlib
    entry=importlib.import_module('apecode_pilot_adapter')
    assert entry is adapter and entry.run_apecode is apecode_run
    files={'code.py':'def add(a, b):\n    return a - b\n',
           'test_acceptance.py':'import unittest\nfrom code import add\nclass Tests(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n',
           'README.md':'Non-benchmark official-provider adapter smoke fixture. Fix add in code.py; do not modify tests.\n'}
    key='official-entry-smoke-'+str(time.time_ns());directory=PREFLIGHT/'runs'/key;directory.mkdir(parents=True,exist_ok=False)
    workspace=Path(os.environ['LOCALAPPDATA'])/'CodeTrail'/key;base.install(workspace,files)
    import hashlib
    task={'id':key,'files':files,'sha256':{n:hashlib.sha256(v.encode()).hexdigest() for n,v in files.items()},
          'expected_tests':1,'editable_files':['code.py'],'prompt':'修复 code.py 中加法函数。先运行原测试，读取源码再修改，修改后重跑测试。只允许改 code.py。'}
    # Separate preflight custody; never installed into a formal task workspace.
    write_json(PREFLIGHT/'external-agent.json',external)
    write_json(PREFLIGHT/'freeze.json',{'at':utcnow(),'scope':'Live non-benchmark entry smoke only'})
    write_json(PREFLIGHT/'prompts.json',{key:base.prompt(task)})
    result=entry.run_apecode(PREFLIGHT,task,workspace,directory)
    external_result=base.verify_candidate(task,{'code.py':(workspace/'code.py').read_bytes()},directory/'external')
    successful=bool(result['completed'] and not result.get('agent_error') and result['baseline_test_observed'] and result['final_test_observed'] and external_result['accepted'])
    write_json(directory/'result.json',{'scope':'Actual base-worker module entry, live model, separate toy; not a formal repair task',
        'passed':successful,'adapter':result,'external':external_result},exclusive=True)
    write_json(PREFLIGHT/'entry-smoke-latest.json',{'at':utcnow(),'passed':successful,'directory':str(directory)})
    print(json.dumps({'entry_smoke_passed':successful,'error':result.get('agent_error')}),flush=True)
    if not successful:raise RuntimeError('Actual adapter-entry smoke failed; no formal batch launch')


def freeze(output):
    if not load_json(PREFLIGHT/'latest-probe.json')['compatible']:raise RuntimeError('Non-task protocol probes have not both passed')
    if not load_json(PREFLIGHT/'entry-smoke-latest.json')['passed']:raise RuntimeError('Actual adapter-entry smoke has not passed')
    external=load_json(ROOT/'artifacts/cross-agent-pilot-2026-11-05/external-agent.json')
    base.freeze(output,Path(external['source']),Path(external['python']))
    for n in ('budget-initial.json','budget-ledger.jsonl'):
        shutil.copy2(PREFLIGHT/n,output/n)
    shutil.copytree(PREFLIGHT,output/'official-provider-preflight')
    sources={p.relative_to(ROOT).as_posix():digest(p) for p in [Path(__file__).resolve(),ROOT/'scripts/deepseek_pilot_support.py',ROOT/'tests/test_deepseek_pilot.py']}
    with zipfile.ZipFile(output/'official_sources.zip','x',zipfile.ZIP_DEFLATED) as z:
        for n in sources:z.write(ROOT/n,n)
    write_json(output/'official-freeze.json',{'at':utcnow(),'parent_freeze_sha256':digest(output/'freeze.json'),
        'sources':sources,'assets':{n:digest(output/n) for n in ('official_sources.zip','budget-initial.json')},
        'protocol':PROTOCOL},exclusive=True)
    print(json.dumps({'official_freeze_sha256':digest(output/'official-freeze.json'),'budget_cny':load_json(output/'budget-initial.json')['max_spend_cny']}),flush=True)


def report(output):
    base.report(output)
    current=balance();initial=Decimal(load_json(output/'budget-initial.json')['initial_balance_cny'])
    ledger=[json.loads(l) for l in (output/'budget-ledger.jsonl').read_text(encoding='utf-8').splitlines()]
    settled=[e for e in ledger if e['event']=='settled']
    write_json(output/'cost-report.json',{'at':utcnow(),'initial_balance_cny':str(initial),'current_balance_cny':str(current),
        'observed_account_delta_cny':str(initial-current),'tariff_estimate_known_cny':str(sum((Decimal(e['tariff_estimate_cny']) for e in settled),Decimal(0))),
        'usage_observed_requests':len(settled),'unknown_request_ids':[e['id'] for e in ledger if e['event']=='reserved' and not any(s['id']==e['id'] for s in settled)],
        'includes_non_task_probes':True,'attribution_limit':'Account delta may include other account activity; token tariff estimate separately retained'})


def run(output):
    check(output)
    if not (output/'asset-preflight-complete.json').exists():raise RuntimeError('Asset preflight incomplete')
    for slot in load_json(output/'plan.json'):
        directory=output/'runs'/slot['run_key']
        if directory.exists():continue
        if balance()<Decimal('.30'):
            write_json(output/'stopped-budget.json',{'at':utcnow(),'reason':'balance reserve','not_started':slot});break
        logdir=output/'logs';logdir.mkdir(exist_ok=True)
        env=clean_environment();env['DEEPSEEK_API_KEY']=os.environ['DEEPSEEK_API_KEY']
        executable=load_json(output/'external-agent.json')['python'] if slot['agent']=='apecode' else sys.executable
        command=[executable,'-u','-X','utf8',str(Path(__file__).resolve()),'--output',str(output),'--worker',slot['run_key']]
        print('START '+slot['run_key'],flush=True)
        with (logdir/(slot['run_key']+'.log')).open('xb') as stream:
            p=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,env=env,cwd=ROOT)
            try:p.wait(timeout=PROTOCOL['run_deadline_seconds'])
            except subprocess.TimeoutExpired:kill_tree(p)
        report(output)
        if not (directory/'result.json').exists():raise RuntimeError('Interrupted worker retained, no replacement: '+slot['run_key'])
        row=load_json(directory/'result.json')
        print(json.dumps({k:row.get(k) for k in ('run_key','functional_passed','workflow_passed','failure_category','elapsed_ms')})+' balance='+str(balance()),flush=True)
        if any(marker in (row.get('agent_error') or '') for marker in ('Budget','HTTP402','HTTP401','HTTP403','KeyError:','ImportError:','ModuleNotFoundError:')):
            write_json(output/'stopped-budget.json',{'at':utcnow(),'trigger_run':slot['run_key'],'reason':row['agent_error']});break


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--probe',action='store_true');parser.add_argument('--entry-smoke',action='store_true');parser.add_argument('--freeze',action='store_true');parser.add_argument('--run',action='store_true');parser.add_argument('--worker')
    args=parser.parse_args();output=args.output.resolve()
    if ROOT not in output.parents:parser.error('Output must stay within project')
    if args.probe:probe()
    elif args.entry_smoke:entry_smoke()
    elif args.freeze:freeze(output)
    elif args.worker:
        check(output)
        ref=output/'run-references'/f'{args.worker}.json'
        write_json(ref,{'at':utcnow(),'official_freeze_sha256':digest(output/'official-freeze.json')},exclusive=True)
        base.worker(output,args.worker)
    elif args.run:run(output)
    else:check(output);report(output)
