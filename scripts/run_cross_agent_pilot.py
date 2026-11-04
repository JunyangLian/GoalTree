"""Same-model CodeTrail/ApeCode pilot: 4 known tasks, 2 repeats, no replacement runs."""
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
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from pico.evaluation.cross_agent import (TASK_IDS, APECODE_COMMIT, clean_environment,
    check_tool_permission, kill_tree, process_metrics, scope_valid, snapshot, summarize, verify_candidate)
from pico.evaluation.repair_suite import append_json, digest, install, load_json, utcnow, write_json
from pico.windows_sandbox import VERIFY

MODEL = 'deepseek-ai/DeepSeek-V4.1-Flash'
PROTOCOL = {'version': 2, 'task_ids': list(TASK_IDS), 'repetitions': 2, 'agents': ['codetrail', 'apecode'],
    'schedule_seed': 20261105, 'concurrency': 1, 'run_deadline_seconds': 300,
    'model': MODEL, 'base_url': 'https://api.siliconflow.cn/v1', 'api_format': 'chat_completions',
    'temperature': .2, 'top_p': .95, 'enable_thinking': True, 'thinking_budget': 4096,
    'max_new_tokens': 4096, 'request_timeout_seconds': 180, 'max_transport_attempts': 3,
    'max_tool_calls': 20, 'verification_command': VERIFY,
    'external_verifier': 'pristine frozen tests + final eligible source bytes; Windows AppContainer; fail closed',
    'exposure': '4 previously evaluated project tasks, 2 author-created and 2 upstream minimal reproductions; no held-out claim',
    'acceptance': 'visible original tests read-only; final modifications limited to frozen editable files',
    'retry': 'No started slot replacement. Bounded transport retries belong to the original run.',
    'codetrail': {'profile': 'workbench', 'embedding': 'off', 'memory': 'fresh per run; no imported confirmed knowledge',
                  'shell_backend': 'appcontainer', 'max_depth': 0, 'stream': True},
    'apecode': {'repository': 'https://github.com/ApeCodeAI/apecode', 'commit': APECODE_COMMIT,
        'entry': 'original NanoCodeAgent.run and default registry; protocol integration, not upstream CLI',
        'max_model_turns': 60, 'stream': False,
        'adaptations': ['independently recorded SiliconFlow HTTP ChatModel transport',
            'host policy gate on every tool; exact unittest exec replaced with AppContainer backend',
            'hard per-tool budget20 outside upstream loop; no plugins/subagents/skills imported',
            'original system template, parent AGENTS discovery scoped to fixture, frozen clock']},
    'fairness': 'Same model/requested sampler/tool-call budget/tasks/prompts/oracle/wall deadline; native schemas/system prompts/context policies differ. Adapted harness comparison, not stock CLI or single-feature ablation.',
    'limits': 'Model ID may be mutable; returned ID recorded. No hidden proof of provider weights/sampler. Dollar cost unknown; missing token usage stays null.',
    'unstarted': 'Non-task provider preflight failure prevents official launch; not a repair failure.'}


def prompt(task):
    return task['prompt'] + '\n仅访问当前工作区，不查外部答案、历史记录或其他仓库，不联网、不委派。验收文件只读，不新建额外项目文件。先执行原始测试，修复后重跑同一命令。唯一允许执行的命令是：'+VERIFY+'。如实报告修改与测试结果。'


def source_info(source):
    executable=shutil.which('git') or r'C:\Program Files\Git\cmd\git.exe'
    commit=subprocess.run([executable,'-C',str(source),'rev-parse','HEAD'],capture_output=True,check=True).stdout.decode().strip()
    if commit!=APECODE_COMMIT: raise RuntimeError('ApeCode version mismatch; no automatic update')
    names=['src/apecode/__init__.py','src/apecode/agent.py','src/apecode/tools.py','src/apecode/system_prompt.py','LICENSE','pyproject.toml']
    return {n:digest(source/n) for n in names}


def freeze(output,source,external_python):
    tasks=load_json(ROOT/'benchmarks/final_agent_v1/tasks.json')['tasks']
    selected=[dict(next(t for t in tasks if t['id']==key)) for key in TASK_IDS]
    for t in selected:
        assert t['sha256']=={n:hashlib.sha256(v.encode()).hexdigest() for n,v in t['files'].items()}
        t['pilot_exposure']='Previously evaluated project task; not a new holdout'
    upstream=source_info(source);output.mkdir(parents=True,exist_ok=False)
    plan=[{'agent':a,'task_id':t,'repeat':r} for t in TASK_IDS for r in (1,2) for a in PROTOCOL['agents']]
    random.Random(PROTOCOL['schedule_seed']).shuffle(plan)
    for i,row in enumerate(plan,1):row['run_key']=f"{i:02d}-{row['agent']}-{row['task_id']}-r{row['repeat']}"
    for name,data in [('tasks.json',selected),('prompts.json',{t['id']:prompt(t) for t in selected}),('protocol.json',PROTOCOL),('plan.json',plan)]:
        write_json(output/name,data,exclusive=True)
    write_json(output/'external-agent.json',{'source':str(source),'source_hashes':upstream,'python':str(external_python),
        'python_sha256':digest(external_python),'version':subprocess.run([str(external_python),'--version'],capture_output=True,check=True).stdout.decode().strip()},exclusive=True)
    files=list((ROOT/'pico').rglob('*.py'))+list((ROOT/'pico/web_assets').glob('*'))
    files+=[Path(__file__).resolve(),ROOT/'tests/test_cross_agent_pilot.py',ROOT/'scripts/apecode_pilot_adapter.py',ROOT/'scripts/check_cross_agent_adapter.py']
    hashes={p.relative_to(ROOT).as_posix():digest(p) for p in files if p.is_file()}
    with zipfile.ZipFile(output/'source_snapshot.zip','x',zipfile.ZIP_DEFLATED) as z:
        for n in hashes:z.write(ROOT/n,n)
        for n in upstream:z.write(source/n,'upstream/apecode/'+n)
    names=['tasks.json','prompts.json','protocol.json','plan.json','external-agent.json','source_snapshot.zip']
    write_json(output/'freeze.json',{'at':utcnow(),'sources':hashes,'assets':{n:digest(output/n) for n in names}},exclusive=True)
    for task in selected:
        result=verify_candidate(task,{},output/'asset-preflight'/task['id'])
        if result.get('tests_run')!=task['expected_tests'] or result['accepted']:raise RuntimeError('Invalid fixture: '+task['id'])
    write_json(output/'asset-preflight-complete.json',{'at':utcnow(),'all_four_buggy_fixtures_rejected':True},exclusive=True)
    print(json.dumps({'frozen_tasks':4,'planned_runs':16,'freeze_sha256':digest(output/'freeze.json')}),flush=True)


def check(output):
    frozen=load_json(output/'freeze.json')
    for n,sha in frozen['sources'].items():
        if digest(ROOT/n)!=sha:raise RuntimeError('Frozen source changed: '+n)
    for n,sha in frozen['assets'].items():
        if digest(output/n)!=sha:raise RuntimeError('Frozen asset changed: '+n)
    external=load_json(output/'external-agent.json')
    for n,sha in external['source_hashes'].items():
        if digest(Path(external['source'])/n)!=sha:raise RuntimeError('Upstream source changed: '+n)
    if digest(external['python'])!=external['python_sha256']:raise RuntimeError('External interpreter changed')


def preflight(output):
    target=output/'availability'/str(time.time_ns());target.mkdir(parents=True,exist_ok=False)
    payload={'model':MODEL,'messages':[{'role':'user','content':'Non-task availability check. Reply READY.'}],
             'temperature':.2,'top_p':.95,'max_tokens':64,'enable_thinking':True,'thinking_budget':1024}
    write_json(target/'request.json',payload,exclusive=True);key=os.environ.get('SILICONFLOW_API_KEY')
    if not key:result={'ok':False,'reason':'missing_environment_credential','at':utcnow()}
    else:
        req=urllib.request.Request(PROTOCOL['base_url']+'/chat/completions',data=json.dumps(payload).encode(),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'})
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=35) as r:body=r.read().decode();status=r.status
            data=json.loads(body);result={'ok':data.get('model')==MODEL,'status':status,'model':data.get('model'),'at':utcnow()}
        except urllib.error.HTTPError as exc:
            body=exc.read().decode();result={'ok':False,'status':exc.code,'reason':'provider_unavailable','at':utcnow()}
        except Exception as exc:
            body=f'{type(exc).__name__}: {exc}';result={'ok':False,'status':None,'reason':'network_or_protocol','at':utcnow()}
        if key in body:raise RuntimeError('Credential returned in preflight body; refuse export')
        (target/'response.txt').write_text(body,encoding='utf-8')
    write_json(target/'status.json',result,exclusive=True);return result


def codetrail_run(output,task,workspace,directory):
    from pico.providers.clients import OpenAICompatibleModelClient
    from pico.runtime import Pico,SessionStore
    from pico.workspace import WorkspaceContext
    from pico.evaluation.repair_suite import RecordingClient
    model=OpenAICompatibleModelClient(MODEL,PROTOCOL['base_url'],os.environ['SILICONFLOW_API_KEY'],.2,180,'chat_completions');model.top_p=.95
    recorder=RecordingClient(model,directory/'model_io.jsonl');model.on_transport_event=lambda e:append_json(directory/'transport.jsonl',e)
    def wire(e):
        if os.environ['SILICONFLOW_API_KEY'] in e.get('body',''):raise RuntimeError('Credential in response; refuse export')
        append_json(directory/'wire.jsonl',e)
        if e['event']=='response' and json.loads(e['body']).get('model')!=MODEL:raise RuntimeError('Returned model mismatch')
    model.on_wire_event=wire
    context=WorkspaceContext(str(workspace),str(workspace),'-','-','fresh frozen fixture',[],{'README.md':task['files']['README.md']})
    agent=Pico(recorder,context,SessionStore(workspace/'.pico/sessions'),approval_policy='ask',max_steps=20,max_new_tokens=4096,
        max_depth=0,verification_command=VERIFY,shell_backend='appcontainer',harness_profile='workbench',embedding_mode='off',
        network_route='direct',secret_env_names={'SILICONFLOW_API_KEY'})
    model.stream=True;model.cancellation=agent.cancellation;model.network_route='direct'
    agent.approve=lambda name,args:check_tool_permission(workspace,task['editable_files'],name,args)
    observations=[];original=agent.execute_tool
    def observe(name,args):
        before=snapshot(workspace);value=original(name,args)
        o={'name':name,'args':args,'output':value.content,'metadata':value.metadata,'exit_code':value.metadata.get('exit_code'),
           'before':before,'snapshot':snapshot(workspace),'rejected':value.metadata.get('tool_status')=='rejected'}
        observations.append(o);append_json(directory/'tools.jsonl',o);return value
    agent.execute_tool=observe;error=None
    try:agent.ask(load_json(output/'prompts.json')[task['id']])
    except Exception as exc:error=f'{type(exc).__name__}: {exc}'
    events=[json.loads(s) for s in (directory/'model_io.jsonl').read_text(encoding='utf-8').splitlines()]
    requests=[{'input_tokens':e.get('usage',{}).get('input_tokens'),'output_tokens':e.get('usage',{}).get('output_tokens'),
               'complete':e['event']=='response'} for e in events if e['event'] in {'response','error'}]
    metrics=process_metrics(observations,requests,task,snapshot(workspace))
    return {**metrics,'completed':bool(agent.current_task_state and agent.current_task_state.status=='completed'),'agent_error':error}


def worker(output,key):
    check(output);slot=next(p for p in load_json(output/'plan.json') if p['run_key']==key)
    task=next(t for t in load_json(output/'tasks.json') if t['id']==slot['task_id'])
    directory=output/'runs'/key;directory.mkdir(parents=True,exist_ok=False)
    workspace=Path(os.environ['LOCALAPPDATA'])/'CodeTrail/cross-agent-runs'/output.name/key
    write_json(directory/'started.json',{'at':utcnow(),'workspace':str(workspace),**slot},exclusive=True)
    install(workspace,task['files']);assert snapshot(workspace)==task['sha256']
    if slot['agent']=='codetrail':
        (workspace/'.pico').mkdir();write_json(workspace/'.pico/policy.json',{'read_only_paths':[n for n in task['files'] if n not in task['editable_files']]})
    started=time.monotonic()
    try:
        if slot['agent']=='apecode':
            from apecode_pilot_adapter import run_apecode
            metrics=run_apecode(output,task,workspace,directory)
        else:metrics=codetrail_run(output,task,workspace,directory)
    except Exception as exc:metrics={'completed':False,'agent_error':f'{type(exc).__name__}: {exc}'}
    elapsed=round((time.monotonic()-started)*1000);final=snapshot(workspace)
    implementation={n:(workspace/n).read_bytes() for n in task['editable_files'] if final.get(n) not in (None,'REPARSE_POINT')}
    verifier=verify_candidate(task,implementation,directory/'external-final');valid=scope_valid(task,final)
    functional=bool(valid and verifier['accepted'])
    workflow=bool(functional and metrics.get('completed') and not metrics.get('agent_error') and metrics.get('baseline_test_observed') and metrics.get('final_test_observed'))
    failure=('unauthorized_file_change' if not valid else 'agent_runtime_error' if metrics.get('agent_error') else
             'external_acceptance_failed' if not functional else 'workflow_evidence_missing' if not workflow else 'success')
    row={**slot,**metrics,'category':task['category'],'origin_kind':task['origin']['kind'],'functional_passed':functional,
         'workflow_passed':workflow,'scope_valid':valid,'failure_category':failure,'external_verifier':verifier,
         'initial_sha256':task['sha256'],'final_sha256':final,'elapsed_ms':elapsed,'finished_at':utcnow(),'freeze_sha256':digest(output/'freeze.json')}
    patches=[]
    for n in sorted(task['sha256'].keys()|final.keys()):
        if task['sha256'].get(n)!=final.get(n):
            new=(workspace/n).read_text(encoding='utf-8',errors='replace') if final.get(n) not in (None,'REPARSE_POINT') else ''
            patches.extend(difflib.unified_diff(task['files'].get(n,'').splitlines(True),new.splitlines(True),fromfile='a/'+n,tofile='b/'+n))
    (directory/'changes.diff').write_text(''.join(patches),encoding='utf-8',newline='\n')
    shutil.copytree(workspace,directory/'final-workspace',ignore=shutil.ignore_patterns('__pycache__'),symlinks=True)
    check(output);write_json(directory/'result.json',row,exclusive=True)


def report(output):
    plan=load_json(output/'plan.json');rows=[]
    for slot in plan:
        directory=output/'runs'/slot['run_key']
        if (directory/'result.json').exists():rows.append(load_json(directory/'result.json'))
        elif directory.exists():rows.append({**slot,'functional_passed':False,'workflow_passed':False,
            'failure_category':'interrupted_without_result','scope_valid':None,'input_tokens':None,'output_tokens':None,'elapsed_ms':None})
    summary=summarize(plan,rows);write_json(output/'summary.json',summary);write_json(output/'results.json',rows)
    write_json(output/'not-started.json',[p for p in plan if not (output/'runs'/p['run_key']).exists()])
    fields=['run_key','agent','task_id','repeat','functional_passed','workflow_passed','failure_category','elapsed_ms','tool_steps','input_tokens','output_tokens','scope_valid']
    with (output/'runs.csv').open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    print(json.dumps({'planned':len(plan),'attempted':len(rows),'complete':summary['complete']}),flush=True)


def run(output):
    check(output)
    if not (output/'asset-preflight-complete.json').exists():raise RuntimeError('Asset preflight incomplete; no launch')
    available=preflight(output)
    if not available['ok']:
        write_json(output/'blocked.json',{'at':utcnow(),'preflight':available,'formal_runs_started':False})
        report(output);print('BLOCKED: no formal repair run launched; availability evidence retained.',flush=True);return
    for slot in load_json(output/'plan.json'):
        directory=output/'runs'/slot['run_key']
        if directory.exists():continue
        logdir=output/'logs';logdir.mkdir(exist_ok=True);env=clean_environment();env['SILICONFLOW_API_KEY']=os.environ['SILICONFLOW_API_KEY']
        executable=load_json(output/'external-agent.json')['python'] if slot['agent']=='apecode' else sys.executable
        command=[executable,'-u','-X','utf8',str(Path(__file__).resolve()),'--output',str(output),'--worker',slot['run_key']]
        print('START '+slot['run_key'],flush=True)
        with (logdir/(slot['run_key']+'.log')).open('xb') as stream:
            p=subprocess.Popen(command,stdout=stream,stderr=subprocess.STDOUT,env=env,cwd=ROOT)
            try:p.wait(timeout=PROTOCOL['run_deadline_seconds'])
            except subprocess.TimeoutExpired:kill_tree(p)
        report(output)
        if not (directory/'result.json').exists():raise RuntimeError('Interrupted slot retained; no automatic replacement: '+slot['run_key'])
        row=load_json(directory/'result.json')
        if any(code in (row.get('agent_error') or '') for code in ('402','401','403')):
            write_json(output/'stopped.json',{'at':utcnow(),'trigger_run':slot['run_key'],'reason':'fatal_provider_status','remaining':'not launched'});break


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--freeze',action='store_true');parser.add_argument('--run',action='store_true');parser.add_argument('--worker')
    parser.add_argument('--apecode-source',type=Path,default=ROOT/'artifacts/cross-agent-sources/apecode');parser.add_argument('--external-python',type=Path)
    args=parser.parse_args();output=args.output.resolve()
    if ROOT not in output.parents:parser.error('Evidence output must be within this project')
    if args.freeze:
        if not args.external_python:parser.error('--external-python must name Python >=3.13')
        freeze(output,args.apecode_source.resolve(),args.external_python.resolve())
    elif args.worker:worker(output,args.worker)
    elif args.run:run(output)
    else:check(output);report(output)
