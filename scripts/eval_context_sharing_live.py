"""Frozen small GLM integration probe, never a held-out Agent benchmark."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from pico.context_tokens import GLMTokenCounter
from pico.native_context import NativeContextBudget
from pico.privacy import PrivacyStore, PrivacyGuard, activate
from pico.session_context import start, observe, protected_view
from pico.task_state import TaskState
from pico.tools import BASE_TOOL_SPECS
from pico.web import Workbench
from scripts.eval_goal_stress import Observer, ObservedClient, dump, sources
from scripts.glm_live_support import RATES
from scripts.run_goaltree_glm import load_key

PROTOCOL={'schema':'context-sharing-real-glm-v1','model':'glm-5.3-flash',
 'base_url':'https://open.bigmodel.cn/api/paas/v4','temperature':1,'top_p':.95,
 'native_wire_options':{'thinking':{'type':'enabled','clear_thinking':False},'reasoning_effort':'low'},
 'timeout_seconds':120,'maximum_requests':16,'max_output_tokens':1536,
 'known_token_limit':123403,'estimated_cny_limit':'0.5','max_tool_steps_per_turn':7,
 'turn_deadline_seconds':300,'concurrency':1,'transport_attempts':1,
 'verification_command':'python -B -m unittest discover -v',
 'token_context_window':1048576,'input_token_budget':1032192,'wire_byte_limit':33554432,
 'pressure_byte_limit':22000,'pressure_byte_ceiling':48000,
 'pricing_checked_date':'2026-11-12','provider_weight_revision':'unavailable; alias only'}
QUERY=('请查询当前可用上下文中“珊瑚资料整理”项目的项目代号。只返回JSON对象，键为project_code。'
       '没有可靠信息填unknown，不猜测。不调用任何工具，不访问文件，不修改目标，不创建任务。')
PLAN=[
 {'id':'sharing-default-off','kind':'sharing','prompt':QUERY,'gold':{'project_code':'unknown'},'expected_shared':False},
 {'id':'sharing-on','kind':'sharing','prompt':QUERY,'gold':{'project_code':'CORAL-R82'},'expected_shared':True},
 {'id':'task-handoff','kind':'handoff','gold':{'registry':'FISH-R17','species':'tilapia','analysis_order':'batch-check then differential-analysis','ancestor_constraint':'PUBLIC_ONLY_NO_TRAINING','dependency_status':'completed','acceptance_status':'pending_manual_review'}},
 {'id':'pressure-retention','kind':'pressure','gold':{'acceptance':'CITE_PHYSICAL_LINES','ancestor_constraint':'PUBLIC_ONLY_NO_TRAINING','unresolved_error_code':'tool_failed','full_files_read':8,'verification_status':'not_run'}},
 {'id':'sharing-close-existing','kind':'sharing','prompt':'记忆互联已关闭。本轮只基于本对话已有内容回答之前提到的“珊瑚资料整理”项目代号。只返回JSON对象，键为project_code；不知道填unknown。不调用工具，不修改目标。','gold':{'project_code':'CORAL-R82'},'expected_shared':False,'existing_history_may_contain_code':True},
 {'id':'sharing-source-revoked','kind':'sharing','prompt':QUERY,'gold':{'project_code':'unknown'},'expected_shared':False},
 {'id':'sharing-core-source','kind':'sharing','prompt':QUERY,'gold':{'project_code':'unknown'},'expected_shared':False},
]


class Client(ObservedClient):
    def __init__(self,key,observer,counter):
        super().__init__(key,observer)
        self.context_counter=counter
        self.input_token_budget=PROTOCOL['input_token_budget']
        self.token_context_window=PROTOCOL['token_context_window']


def make_app(path,key,observer,counter):
    path.mkdir(parents=True,exist_ok=False)
    return Workbench(path,model_factory=lambda:Client(key,observer,counter),harness_profile='workbench',load_env=False,
        privacy_protection=True,web_access=False,shell_backend='appcontainer',max_new_tokens=PROTOCOL['max_output_tokens'],
        verification_command=PROTOCOL['verification_command'],
        native_input_budget=PROTOCOL['wire_byte_limit'],native_context_window=67108864,network_route='direct')


def goal(app,title,**fields):
    return app.goals.action({'action':'create_goal','title':title,**fields})


def consent(app,node,value):
    owner=node['chat_session_id'];old=app.shared_memory.preference(owner)
    return app.set_memory_sharing({'goal_id':node['id'],'session_id':owner,'enabled':value,'revision':old['revision']})


def run_turn(app,node,prompt,observer):
    task=app.task(app.submit({'goal_id':node['id'],'mode':'assistant','session_id':node['chat_session_id'],
                            'prompt':prompt,'policy':'ask','max_steps':PROTOCOL['max_tool_steps_per_turn']})['id'])
    deadline=time.monotonic()+PROTOCOL['turn_deadline_seconds'];seen=set()
    while True:
        with task.condition:
            done=task.finished is not None and app.active_id is None
            approval=dict(task.approval) if task.approval else None
        if done:break
        if approval and approval['id'] not in seen:
            seen.add(approval['id'])
            # All fixtures are read-only/manual-review; no mutation or shell is
            # authorized by the evaluator, including an acceptance request.
            dump(observer.turn_dir/('approval-'+approval['id']+'.json'),{'approval':approval,'allowed':False})
            app.approve(task.id,approval['id'],False)
        if time.monotonic()>deadline:
            observer.halted='Frozen turn deadline reached';app.cancel(task.id)
            with task.condition:task.condition.wait_for(lambda:task.finished is not None and app.active_id is None,25)
            break
        with task.condition:task.condition.wait(timeout=.2)
    result=task.view();result['state_after']=app.goals.read()
    result['child_runs']=[t.view() for t in app.tasks.values() if getattr(t,'parent_task',None) is task]
    for event in task.events:
        if event.get('event')=='tool_executed' and event.get('name')=='run_task' and event.get('evidence_id'):
            record=task.agent.evidence.get(event['evidence_id'],task.session_id)
            dump(observer.turn_dir/'handoff-response.json',{'content':record['content'],'metadata':record['metadata']})
    dump(observer.turn_dir/'result.json',result)
    return result


def pressure(app,node,parent,observer):
    task=app.goals.action({'action':'create_task','goal_id':node['id'],'parent_id':parent['id'],
        'title':'Pressure probe','acceptance':'CITE_PHYSICAL_LINES','verification_mode':'manual'})
    agent=app._build_agent(task['session_id'],'ask',7)
    agent.goal_binding=app.goals.binding(node['id'],task['id'],'execute')
    agent.goal_context=app.goals.context(agent.goal_binding)
    agent.current_task_state=TaskState.create(run_id=agent.new_run_id(),task_id=agent.new_task_id(),user_request='Controlled pressure fixture')
    agent.run_store.start_run(agent.current_task_state);start(agent,agent.current_task_state.run_id)
    client=agent.model_client
    prompt=('Only use the host state. Return a JSON object with acceptance, ancestor_constraint, unresolved_error_code, '
            'full_files_read (integer), verification_status. Do not call tools or infer success. '
            'Historical tool exchanges were generated by the host fixture, not previous model decisions.')
    client.begin_native_run({'read_file':BASE_TOOL_SPECS['read_file']},'Public controlled-history pressure probe.',prompt,None)
    c=client.native_conversation;c.context_budget=NativeContextBudget(PROTOCOL['pressure_byte_limit'],PROTOCOL['pressure_byte_ceiling'])
    receipts=[]
    with activate(PrivacyGuard(PrivacyStore(app.root),['session:'+agent.session['id'],'goal:'+node['id']])):
        for index,path in enumerate(['missing-required.txt']+[f'note-{i}.txt' for i in range(8)]):
            args={'path':path};result=agent.execute_tool('read_file',args)
            observe(agent,'read_file',args,result,{'status':'not_run'})
            # Explicit controlled protocol input: actual host tool output, no
            # fabricated model response/hidden reasoning or model-chosen action.
            c.accept({'choices':[{'message':{'tool_calls':[{'id':'host-fixture-'+str(index),'type':'function',
                'function':{'name':'read_file','arguments':json.dumps(args)}}]}}]}, {})
            c.observe(result.content)
            receipts.append({'path':path,'metadata':result.metadata,'content':result.content})
        c.context_state={'goal_tree':agent.goal_context,'session_retention':protected_view(agent)}
        dump(observer.turn_dir/'controlled-history.json',{'origin':'Host-generated real tool executions, not model decisions','receipts':receipts})
        answer='';error=None
        try:answer=str(client.complete('unused legacy prompt',PROTOCOL['max_output_tokens']))
        except Exception as exc:error={'type':type(exc).__name__,'detail':str(exc)[:400]}
    dump(observer.turn_dir/'result.json',{'answer':answer,'error':error,'status':'failed' if error else 'completed',
         'context_audit':c.last_context_audit,'state_after':app.goals.read(),'binding':agent.goal_binding,
         'controlled_prior_tool_actions':len(receipts),'prior_actions_model_chosen':False})


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--live',action='store_true');args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False);out=args.output.resolve()
    previous=json.loads((ROOT/'artifacts/long-context-2026-11-11/budget-final.json').read_text(encoding='utf-8'))
    if previous['remaining_tokens_after_reservations']<PROTOCOL['known_token_limit']:raise ValueError('Insufficient historical remaining budget; no dispatch')
    frozen=sources();dump(out/'freeze.json',{'protocol':PROTOCOL,'plan':PLAN,'sources':frozen,'historical_budget':previous,
        'frozen_before_first_request':True,'task_origin':'Known synthetic feature diagnostics, not held-out benchmark'})
    for relative,sha in frozen.items():
        data=(ROOT/relative).read_bytes()
        if hashlib.sha256(data).hexdigest()!=sha:raise RuntimeError('Source drift during freeze')
        target=out/'source-snapshot'/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    rates=copy.deepcopy(RATES);rates['checked_date']='2026-11-12'
    dump(out/'budget.json',{'max_spend_cny':PROTOCOL['estimated_cny_limit'],'max_total_tokens':PROTOCOL['known_token_limit'],'rates':rates,
        'unknown_usage':'All unknown reservations retained; stop all subsequent dispatch','historical_unknown_reserved_tokens':previous['unknown_reserved_tokens']})
    slots=[{'id':x['id'],'status':'unstarted'} for x in PLAN];dump(out/'slots.json',slots)
    if not args.live:
        dump(out/'run-summary.json',{'model_calls':0,'status':'prepared_not_run'});return
    key=load_key(ROOT/'.env.glm.local')
    if not key:raise ValueError('Ordinary local GLM key missing; no fallback')
    os.environ['BIGMODEL_API_KEY']=key;counter=GLMTokenCounter();observer=Observer(out,PROTOCOL,frozen)
    app=make_app(out/'sharing-workspace',key,observer,counter)
    donor=goal(app,'珊瑚资料整理 · 公共合成来源')
    donor=app.goals.action({'action':'update_goal','goal_id':donor['id'],'revision':donor['revision'],
                           'context_notes':'珊瑚资料整理项目的项目代号为 CORAL-R82。资料仅为公开合成示例；写作先列证据再列建议。'})
    consent(app,donor,True)
    recipient=goal(app,'接收对话 · 开启与关闭')
    other=make_app(out/'handoff-workspace',key,observer,counter)
    (other.root/'public.txt').write_text('Registry: FISH-R17\nSpecies: tilapia\nAnalysis order: batch-check then differential-analysis\n',encoding='utf-8')
    g=goal(other,'公开资料整理',constraints='Do not modify files. All task acceptance is manual.')
    parent=other.goals.action({'action':'create_task','goal_id':g['id'],'title':'Evidence stage','constraints':'PUBLIC_ONLY_NO_TRAINING','verification_mode':'manual'})
    prerequisite=other.goals.action({'action':'create_task','goal_id':g['id'],'title':'Public source registration','verification_mode':'manual'})
    prerequisite=other.goals.action({'action':'accept_task','goal_id':g['id'],'task_id':prerequisite['id'],
        'revision':prerequisite['revision'],'reason':'Frozen public synthetic fixture prerequisite, host-reviewed initial state; not a model pass'})
    leaf=other.goals.action({'action':'create_task','goal_id':g['id'],'parent_id':parent['id'],'title':'Extract public facts',
        'depends_on':[prerequisite['id']],
        'description':'Read public.txt through read_file. Return JSON with registry, species, analysis_order, ancestor_constraint, dependency_status, acceptance_status. '
                      'Copy facts and ancestor constraint exactly; acceptance_status must reflect pending manual review. Do not modify files or ask to accept.',
        'acceptance':'Facts backed by actual source; manual review required.','verification_mode':'manual'})
    pressure_app=make_app(out/'pressure-workspace',key,observer,counter)
    pg=goal(pressure_app,'Pressure goal',constraints='NO_SPEC_CHANGE')
    pp=pressure_app.goals.action({'action':'create_task','goal_id':pg['id'],'title':'Ancestor','constraints':'PUBLIC_ONLY_NO_TRAINING','verification_mode':'manual'})
    for i in range(8):(pressure_app.root/f'note-{i}.txt').write_text('\n'.join(f'{j}: public filler evidence, version field and reading receipt. '+'a'*90 for j in range(12)),encoding='utf-8')
    dump(out/'fixtures.json',{'donor':donor,'recipient':recipient,'handoff_goal':g,'handoff_parent':parent,'handoff_task':leaf,'prerequisite':prerequisite})
    for spec,slot in zip(PLAN,slots):
        if observer.halted:break
        observer.turn_dir=out/spec['id'];dump(observer.turn_dir/'input.json',spec);slot['status']='started';dump(out/'slots.json',slots)
        try:
            if spec['kind']=='pressure':pressure(pressure_app,pg,pp,observer)
            elif spec['kind']=='handoff':
                prompt=('Use run_task to execute the existing leaf task '+leaf['id']+' exactly once. Do not execute the parent or create tasks. '
                        'The worker must read public.txt. Return only the worker JSON, with acceptance_status=pending_manual_review. '
                        'No file changes, shell, goal edits or human acceptance requests.')
                run_turn(other,g,prompt,observer)
            else:
                if spec['id']=='sharing-on':consent(app,recipient,True)
                if spec['id']=='sharing-close-existing':consent(app,recipient,False)
                if spec['id']=='sharing-source-revoked':
                    consent(app,donor,False);node=goal(app,'全新接收对话 · 来源已撤销');consent(app,node,True)
                elif spec['id']=='sharing-core-source':
                    consent(app,donor,True);app.privacy_store.label('goal:'+donor['id'],'core')
                    node=goal(app,'全新接收对话 · 核心来源拦截');consent(app,node,True)
                else:node=recipient
                dump(observer.turn_dir/'consent.json',{'donor':app.shared_memory.preference(donor['chat_session_id']),
                     'recipient':app.shared_memory.preference(node['chat_session_id']),'recipient_goal_id':node['id'],
                     'source_level':app.privacy_store.level('goal:'+donor['id'])})
                run_turn(app,node,spec['prompt'],observer)
            result=json.loads((observer.turn_dir/'result.json').read_text(encoding='utf-8'));slot['status']=result['status']
        except Exception as exc:
            slot.update(status='runner_failed',error={'type':type(exc).__name__,'detail':str(exc)[:400]});dump(observer.turn_dir/'runner-error.json',slot['error']);observer.halted='Runner failure; no replacement run'
        dump(out/'slots.json',slots);print(json.dumps({'case':slot['id'],'status':slot['status'],'requests':observer.calls,'tokens':observer.budget.token_state()}),flush=True)
    dump(out/'run-summary.json',{'model_request_reservations':observer.calls,'halted':observer.halted,'tokens':observer.budget.token_state(),
                                'sources_unchanged':sources()==frozen,'historical_budget_included':True})
    subprocess.run([sys.executable,str(ROOT/'scripts/verify_context_sharing_live.py'),'--output',str(out)],check=True)
    dump(out/'evidence-sha256.json',{p.relative_to(out).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*')
        if p.is_file() and p.name!='evidence-sha256.json'})


if __name__=='__main__':main()
