"""Three frozen twelve-turn real GLM workflows; append-only capped budget."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.eval_goal_stress import Observer, dump, sources
from scripts.eval_context_sharing_live import make_app, goal, consent
from scripts.glm_live_support import RATES
from scripts.run_goaltree_glm import load_key
from pico.context_tokens import GLMTokenCounter

PROTOCOL={'schema':'long-workflows-real-glm-v1','model':'glm-5.3-flash','base_url':'https://open.bigmodel.cn/api/paas/v4',
    'temperature':1,'top_p':.95,'native_wire_options':{'thinking':{'type':'enabled','clear_thinking':False},'reasoning_effort':'low'},
    'timeout_seconds':120,'maximum_requests':100,'max_output_tokens':1536,'max_total_tokens':600000,'max_spend_cny':'1',
    'max_tool_steps_per_turn':7,'turn_deadline_seconds':300,'concurrency':1,'transport_attempts':1,
    'input_token_budget':1032192,'token_context_window':1048576,'wire_byte_limit':33554432,
    'shell_backend':'appcontainer','verification_command':'python -B -m unittest discover -v',
    'authorization':'User authorized batch evaluation and stated 30M available; repair retest capped at 600K tokens / CNY1; previous 500K batch preserved.',
    'pricing_checked_date':'2026-11-12','provider_weight_revision':'unavailable; alias only'}

def plan(c):
    only=' 只返回JSON对象，不加解释或Markdown，不猜测；不修改其他Goal，不请求或自行执行人工验收。'
    return [
        {'id':'t01','target':'A','prompt':f'准备{c["title"]}。本次预算数量为{c["quota"]}。请只在当前Goal下新增一个标题为“核查来源”的待办Task，不执行实施，不修改其他已有任务。完成后简短说明。','create_title':'核查来源'},
        {'id':'t02','target':'A','prompt':'用read_file读取packet.txt，回答registry、subject和file_version。'+only,'gold':{'registry':c['code'],'subject':c['subject'],'file_version':'v1'},'read_required':True},
        {'id':'t03','target':'A','prompt':f'更正：预算数量改成{c["revised_quota"]}，覆盖之前数量。仅回答JSON键quota，不改任务树。'+only,'gold':{'quota':c['revised_quota']}},
        {'id':'t04','target':'B','prompt':'本轮已经由用户选中当前对话，仅根据当前Goal约束回答goal_code。不要从其他目标借资料，不调用工具。'+only,'gold':{'goal_code':c['secondary']}},
        {'id':'t05','target':'A','prompt':'把现有名为“提取公开信息”的Task验收条件更新为CITE_PHYSICAL_LINES，只改这个Task的acceptance，使用当前revision。更新后回答JSON键acceptance。'+only,'gold':{'acceptance':'CITE_PHYSICAL_LINES'},'acceptance_updated':True},
        {'id':'t06','target':'A','prompt':'请先用read_file尝试missing-required.txt（即使认为不存在也要实际尝试），失败后继续读取packet.txt恢复工作；只回答JSON error_code（来自真实错误）和registry，不创建文件。'+only,'gold':{'error_code':'invalid_arguments','registry':c['code']},'read_required':True,'missing_required':True},
        {'id':'t07','target':'B','prompt':'双方已开启记忆互联。检索当前可用的“公开协作代号”，仅回答JSON shared_code。不调用工具或修改目标。'+only,'gold':{'shared_code':c['shared']},'shared':True},
        {'id':'t08','target':'A','prompt':'packet.txt已经在本轮外部更新。请刷新读取，回答JSON file_version和count（整数），不得复用旧文件值。'+only,'gold':{'file_version':'v2','count':42},'read_required':True},
        {'id':'t09','target':'A','prompt':'仅用run_task执行一次现有“提取公开信息”叶子任务。子任务须实际读取packet.txt，报告registry、file_version和count，保持人工验收待复核。不运行父任务，不调用Shell，不修改文件。不要请求验收。','child_required':True},
        {'id':'t10','target':'B','prompt':'记忆互联已关闭。只基于本对话之前已经回答的公开协作代号，返回JSON shared_code；不进行新共享检索，不调用工具，不修改目标。'+only,'gold':{'shared_code':c['shared']},'shared':False},
        {'id':'t11','target':'A','prompt':'回顾本对话：更正后的预算数量是多少？已刷新读取的packet.txt版本和count是多少？返回JSON quota、file_version、count，不调用工具。'+only,'gold':{'quota':c['revised_quota'],'file_version':'v2','count':42}},
        {'id':'t12','target':'A','prompt':'最后根据当前Goal约束与真实Task状态检查：返回JSON goal_code、task_status（提取公开信息的状态原值）、constraint（PUBLIC_ONLY）、quota（本对话更正后的预算数）。不要修改状态，不把待复核当完成。'+only,'gold':{'goal_code':c['code'],'task_status':'review','constraint':'PUBLIC_ONLY','quota':c['revised_quota']}}
    ]

def packet(c,version):
    return f'Registry: {c["code"]}\nSubject: {c["subject"]}\nFile_version: {version}\nCount: {12 if version=="v1" else 42}\n'+ '\n'.join(f'{i}: public synthetic background for {c["title"]}; no private data.' for i in range(35))+'\n'

def approval_allowed(a,goal_id):
    # An explicit evaluator decision for this synthetic workflow only. Never
    # alter runtime policy, approve shell/write, or approve user acceptance.
    proposal=a.get('args',{})
    if a.get('name')=='goal_action':
        if 'proposal' in proposal:
            try:proposal=json.loads(proposal['proposal'])
            except (ValueError,TypeError):return False
        return proposal.get('action') in {'create_task','update_task'} and proposal.get('goal_id',goal_id)==goal_id
    return a.get('name')=='run_task' and a.get('kind')=='implementation_task'

def turn(app,node,spec,observer):
    before=app.goals.read();foreign={k:v for k,v in before['goals'].items() if k!=node['id']}
    foreign_tasks={k:v for k,v in before['tasks'].items() if v['goal_id']!=node['id']}
    t=app.task(app.submit({'goal_id':node['id'],'mode':'assistant','session_id':node['chat_session_id'],'prompt':spec['prompt'],
        'policy':'ask','max_steps':7,'cooperation':{'planning':'balanced','implementation':'balanced'}})['id'])
    deadline=time.monotonic()+300;seen=set()
    while True:
        with t.condition:
            done=t.finished is not None and app.active_id is None;a=dict(t.approval) if t.approval else None
        if done:break
        if a and a['id'] not in seen:
            seen.add(a['id']);allowed=approval_allowed(a,node['id'])
            dump(observer.turn_dir/('approval-'+a['id']+'.json'),{'approval':a,'allowed':allowed})
            app.approve(t.id,a['id'],allowed)
        if time.monotonic()>deadline:
            app.cancel(t.id);observer.halted='Frozen turn deadline; no replacement'
            with t.condition:t.condition.wait_for(lambda:t.finished is not None and app.active_id is None,25)
            break
        with t.condition:t.condition.wait(timeout=.2)
    r=t.view();state=app.goals.read();r.update(state_after=state,foreign_before=foreign,
        foreign_after={k:state['goals'].get(k) for k in foreign},foreign_tasks_before=foreign_tasks,
        foreign_tasks_after={k:v for k,v in state['tasks'].items() if v['goal_id']!=node['id']},
        child_runs=[x.view() for x in app.tasks.values() if getattr(x,'parent_task',None) is t])
    dump(observer.turn_dir/'result.json',r);return r

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--live',action='store_true');args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False);out=args.output.resolve()
    tasks=json.loads((ROOT/'benchmarks/long_workflows_v1/tasks.json').read_text('utf-8'));frozen=sources()
    asset_hash=hashlib.sha256((ROOT/'benchmarks/long_workflows_v1/tasks.json').read_bytes()).hexdigest()
    slots=[{'case':c['id'],'turn':s['id'],'status':'unstarted','spec':s} for c in tasks for s in plan(c)]
    dump(out/'freeze.json',{'protocol':PROTOCOL,'tasks':tasks,'sources':frozen,'task_asset_sha256':asset_hash,'frozen_before_first_request':True,'plans':slots})
    for relative,h in frozen.items():
        data=(ROOT/relative).read_bytes()
        if hashlib.sha256(data).hexdigest()!=h:raise RuntimeError('Freeze source drift')
        d=out/'source-snapshot'/relative;d.parent.mkdir(parents=True,exist_ok=True);d.write_bytes(data)
    dump(out/'budget.json',{'max_spend_cny':PROTOCOL['max_spend_cny'],'max_total_tokens':PROTOCOL['max_total_tokens'],'rates':{**RATES,'checked_date':'2026-11-12'}})
    dump(out/'slots.json',slots)
    if not args.live:return
    key=load_key(ROOT/'.env.glm.local')
    if not key:raise ValueError('Ordinary GLM key unavailable; no fallback')
    os.environ['BIGMODEL_API_KEY']=key;observer=Observer(out,PROTOCOL,frozen);counter=GLMTokenCounter()
    apps={};nodes={}
    for c in tasks:
        app=make_app(out/c['id']/'workspace',key,observer,counter)
        a=goal(app,c['title'],constraints=f'goal_code={c["code"]}; PUBLIC_ONLY; no invented facts; manual acceptance only.',context_notes=f'公开协作代号 shared_code={c["shared"]}。只适用于本批公开合成协作。')
        b=goal(app,'第二目标 '+c['title'],constraints=f'goal_code={c["secondary"]}; different Goal; PUBLIC_ONLY; manual acceptance only.')
        parent=app.goals.action({'action':'create_task','goal_id':a['id'],'title':'资料阶段','constraints':'PUBLIC_ONLY','verification_mode':'manual'})
        leaf=app.goals.action({'action':'create_task','goal_id':a['id'],'parent_id':parent['id'],'title':'提取公开信息',
            'description':'Read packet.txt. Report registry, file_version and count from actual source. Do not change files or run shell. Manual review remains required.',
            'acceptance':'CITE_SOURCE','verification_mode':'manual'})
        (app.root/'packet.txt').write_text(packet(c,'v1'),'utf-8')
        apps[c['id']]=app;nodes[c['id']]={'A':a,'B':b,'leaf':leaf};dump(out/c['id']/'fixture.json',nodes[c['id']])
    for slot in slots:
        if observer.halted:break
        if hashlib.sha256((ROOT/'benchmarks/long_workflows_v1/tasks.json').read_bytes()).hexdigest()!=asset_hash:
            observer.halted='Task asset changed';break
        app=apps[slot['case']];n=nodes[slot['case']];spec=slot['spec'];observer.turn_dir=out/slot['case']/slot['turn']
        dump(observer.turn_dir/'input.json',spec);slot['status']='started';dump(out/'slots.json',slots)
        try:
            if slot['turn']=='t07':consent(app,n['A'],True);consent(app,n['B'],True)
            if slot['turn']=='t08':(app.root/'packet.txt').write_text(packet(next(c for c in tasks if c['id']==slot['case']),'v2'),'utf-8')
            if slot['turn']=='t10':consent(app,n['B'],False)
            result=turn(app,n[spec['target']],spec,observer);slot['status']=result['status']
        except Exception as exc:
            slot.update(status='runner_failed',error={'type':type(exc).__name__,'detail':str(exc)[:300]});dump(observer.turn_dir/'runner-error.json',slot['error']);observer.halted='Runner failed; preserve all records'
        dump(out/'slots.json',slots)
        print(json.dumps({'case':slot['case'],'turn':slot['turn'],'status':slot['status'],'requests':observer.calls,'tokens':observer.budget.token_state()}),flush=True)
    dump(out/'summary.json',{'halted':observer.halted,'sources_unchanged':sources()==frozen,'tokens':observer.budget.token_state(),'model_requests':observer.calls})
    subprocess.run([sys.executable,str(ROOT/'scripts/verify_long_workflows.py'),'--output',str(out)],check=True)
    dump(out/'evidence-sha256.json',{p.relative_to(out).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in out.rglob('*') if p.is_file() and p.name!='evidence-sha256.json'})

if __name__=='__main__':main()
