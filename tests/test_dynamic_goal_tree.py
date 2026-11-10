"""Conversation deliverables, atomic stage acceptance and scoped tree updates."""
import json

import pytest

from pico.goals import GoalStore,GoalError
from pico.providers.clients import FakeModelClient
from pico.task_progress import task_progress
from pico.web import Workbench


def tool(name,args):return '<tool>'+json.dumps({'name':name,'args':args},ensure_ascii=False)+'</tool>'


def setup(app):
    g=app.goals.action({'action':'create_goal','title':'报告'})
    p=app.goals.action({'action':'create_task','goal_id':g['id'],'title':'阶段一','verification_mode':'manual'})
    leaves=[app.goals.action({'action':'create_task','goal_id':g['id'],'parent_id':p['id'],
        'title':title,'acceptance':'有实际内容可核对','verification_mode':'manual'}) for title in ['需求','大纲']]
    return g,p,leaves


def submit(app,g,outputs,prompt='继续完善',approve=None):
    app.make_model=lambda:FakeModelClient(list(outputs))
    job=app.task(app.submit({'mode':'assistant','goal_id':g['id'],'prompt':prompt,'max_steps':12,
        'cooperation':{'planning':'autonomous','implementation':'autonomous'}})['id'])
    approvals=[]
    while True:
        with job.condition:
            assert job.condition.wait_for(lambda:job.approval is not None or job.finished is not None,20)
            a=job.approval
        if job.finished is not None:break
        assert approve is not None,'unexpected approval'
        approvals.append(a)
        if callable(approve):allowed=approve(a)
        else:allowed=approve
        app.approve(job.id,a['id'],allowed)
        with job.condition:job.condition.wait_for(lambda:job.approval is None or job.approval['id']!=a['id'],10)
    assert job.status=='completed',job.error
    return job,approvals


def progress(item,content):
    return tool('goal_action',{'proposal':json.dumps({'action':'record_progress','task_id':item['id'],
        'revision':item['revision'],'status':'review','note':'已产出，等待人工核对','output':content},ensure_ascii=False)})


@pytest.mark.parametrize('allowed',[True,False])
def test_chat_work_is_tracked_then_one_actual_approval_accepts_entire_stage(tmp_path,allowed):
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient([]))
    g,p,leaves=setup(app)
    job,_=submit(app,g,[progress(leaves[0],'研究对象与范围：蓝子鱼脂质'),progress(leaves[1],'报告大纲：背景、证据、建议'),'<final>需求与大纲已提交。</final>'])
    s=app.goals.read()
    assert '研究对象与范围：蓝子鱼脂质' in job.answer
    assert '报告大纲：背景、证据、建议' in job.answer
    assert all(s['tasks'][t['id']]['status']=='review' for t in leaves)
    assert task_progress(s)[p['id']]['display_status']=='review'
    ref=s['tasks'][leaves[0]['id']]['deliverables'][0]['evidence_id']
    assert '蓝子鱼' in job.agent.evidence.get(ref,job.session_id)['content']
    _,approvals=submit(app,g,[tool('confirm_task',{'task_id':p['id'],'include_children':1,
        'reason':'用户核对本阶段产出后确认'}),'<final>已处理验收请求。</final>'],approve=allowed)
    assert len(approvals)==1 and len(approvals[0]['args']['tasks'])==3
    assert {n['title'] for n in approvals[0]['args']['tasks']}=={'阶段一','需求','大纲'}
    assert any('蓝子鱼脂质' in n['output_preview'] for n in approvals[0]['args']['tasks'])
    s=app.goals.read();aggregate=task_progress(s)[p['id']]
    assert aggregate['display_status']==('completed' if allowed else 'review')
    assert all(s['tasks'][t['id']]['acceptance_origin']=='operator' for t in leaves) if allowed else all(s['tasks'][t['id']]['status']=='review' for t in leaves)
    assert len(s['tasks'])==3


def test_batch_acceptance_rejects_version_change_without_partial_acceptance(tmp_path):
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient([]))
    g,p,leaves=setup(app)
    submit(app,g,[*[progress(t,'实际产出') for t in leaves],'<final>已提交。</final>'])
    def change_during_approval(approval):
        t=app.goals.read()['tasks'][leaves[0]['id']]
        app.goals.action({'action':'update_task','goal_id':g['id'],'task_id':t['id'],
            'revision':t['revision'],'acceptance':'验收范围已变更'})
        return True
    job,_=submit(app,g,[tool('confirm_task',{'task_id':p['id'],'include_children':1,'reason':'核对阶段'}),
        '<final>版本变化，未完成验收。</final>'],approve=change_during_approval)
    assert not any(t['status']=='completed' for t in app.goals.read()['tasks'].values())
    assert any(e.get('tool_status')=='error' for e in job.events if e['event']=='tool_executed')


def test_planned_leaf_cannot_be_bulk_accepted_and_notes_do_not_invalidate_contracts(tmp_path):
    store=GoalStore(tmp_path)
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient([]))
    g,p,leaves=setup(app);before=store.read()
    with pytest.raises(GoalError,match='尚未提交'):store.acceptance_nodes(before,g['id'],p['id'],1)
    store.action({'action':'update_goal','goal_id':g['id'],'revision':before['goals'][g['id']]['revision'],'context_notes':'用户研究蓝子鱼'})
    after=store.read()
    assert after['tasks']==before['tasks']
    assert after['goals'][g['id']]['spec_version']==before['goals'][g['id']]['spec_version']


def test_followup_reads_prior_final_output_and_edits_existing_ids_without_cross_goal_changes(tmp_path):
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient([]))
    g,p,leaves=setup(app)
    other=app.goals.action({'action':'create_goal','title':'其他目标'})
    foreign=app.goals.action({'action':'create_task','goal_id':other['id'],'title':'不可串目标'})
    submit(app,g,['<final>## 已有大纲\n背景、结果和展望。</final>'])
    current=app.goals.read()['tasks'][leaves[1]['id']]
    job,_=submit(app,g,[tool('goal_action',{'proposal':json.dumps({'action':'update_task',
        'task_id':current['id'],'revision':current['revision'],'title':'十分钟汇报提纲'})}),'<final>已调整原节点。</final>'])
    assert app.goals.read()['tasks'][current['id']]['title']=='十分钟汇报提纲'
    assert app.goals.read()['tasks'][foreign['id']]==foreign
    refs=job.agent.goal_context['recent_deliverables']
    assert any('已有大纲' in r['preview'] for r in refs)
    ref=next(r['evidence_id'] for r in refs if '已有大纲' in r['preview'])
    now=app.goals.read()['tasks'][current['id']]
    job,_=submit(app,g,[tool('goal_action',{'proposal':json.dumps({'action':'record_progress',
        'task_id':now['id'],'revision':now['revision'],'status':'review','note':'引用上轮大纲产出','evidence_id':ref})}),
        '<final>已关联原有产出。</final>'])
    assert app.goals.read()['tasks'][now['id']]['deliverables'][-1]['evidence_id']==ref
    result=job.agent.execute_tool('goal_action',{'proposal':json.dumps({'action':'record_progress',
        'task_id':foreign['id'],'revision':foreign['revision'],'status':'active','note':'错误目标'})})
    assert result.metadata['tool_status']!='ok'


def test_native_followup_request_contains_tree_after_successful_mutations(tmp_path):
    from test_goal_planning_contract import NativePlanningClient
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=NativePlanningClient,
                  native_input_budget=131072,native_context_window=262144)
    job=app.task(app.submit({'mode':'assistant','prompt':'规划鱼类组学项目','max_steps':8,
        'cooperation':{'planning':'autonomous','implementation':'autonomous'}})['id'])
    with job.condition:assert job.condition.wait_for(lambda:job.finished is not None,20)
    assert job.status=='completed',job.error
    payload=job.agent.model_client.sent[-1]
    frames=[m['content'] for m in payload['messages'] if (m.get('content') or '').startswith('Host tracked task state')]
    state=json.loads(frames[0].split('\n',1)[1])
    assert {t['id'] for t in state['goal_tree']['existing_tasks']}==set(app.goals.read()['tasks'])


def test_background_notes_are_routine_but_scope_change_still_requires_direction_confirmation():
    from pico.cooperation import checkpoints
    settings={'planning':'balanced','implementation':'balanced'}
    routine={'proposal':json.dumps({'action':'update_goal','context_notes':'物种为蓝子鱼'})}
    assert checkpoints(settings,'assistant','goal_action',routine,False,{})==[]
    critical={'proposal':json.dumps({'action':'update_goal','constraints':'改做其他物种'})}
    assert checkpoints(settings,'assistant','goal_action',critical,False,{})[0][0]=='planning_step'


def test_changed_draft_is_not_submitted_for_the_new_contract(tmp_path):
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient([]))
    g,p,leaves=setup(app)
    submit(app,g,[*[progress(t,'旧规格产出') for t in leaves],'<final>已提交。</final>'])
    t=app.goals.read()['tasks'][leaves[0]['id']]
    result=app.goals.action({'action':'update_task','goal_id':g['id'],'task_id':t['id'],
        'revision':t['revision'],'description':'改成五页PPT，不再写长文'})
    assert result['status']=='planned' and result['validity']=='stale'
    assert result['deliverables']==t['deliverables']
    with pytest.raises(GoalError,match='尚未提交'):
        app.goals.acceptance_nodes(app.goals.read(),g['id'],p['id'],1)


def test_own_parent_change_conflict_requires_repair_before_final_success(tmp_path):
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient([]))
    g,p,leaves=setup(app)
    def update(item,revision,**fields):
        return tool('goal_action',{'proposal':json.dumps({'action':'update_task','task_id':item['id'],
            'revision':revision,**fields})})
    job,_=submit(app,g,[update(p,p['revision'],description='新版阶段'),
        update(leaves[0],leaves[0]['revision'],title='新版子任务'),
        '<final>已全部更新。</final>',
        update(leaves[0],leaves[0]['revision']+1,title='新版子任务'),
        '<final>父子任务已更新。</final>'])
    assert app.goals.read()['tasks'][leaves[0]['id']]['title']=='新版子任务'
    assert any(e['event']=='completion_rejected' for e in job.events)
    assert not job.agent.goal_sync_problem()
