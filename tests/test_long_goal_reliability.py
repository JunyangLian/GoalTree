"""Long goal state transitions, context continuity and scoped retrieval."""
import json
from unittest.mock import patch

import pytest

from pico.goals import GoalStore,GoalError
from pico.continuity import select_events
from pico.retrieval import RepositoryIndex
from pico.evolution import EvolutionStore
from pico.native_context import NativeContextBudget,protocol_groups
from pico.providers.native_tools import NativeToolConversation


def setup(root):
    store=GoalStore(root)
    goal=store.action({'action':'create_goal','title':'报告目标','constraints':'不得编造实验数据；只写公开资料。'})
    first=store.action({'action':'create_task','goal_id':goal['id'],'title':'准备资料','acceptance':'有出处','verification_mode':'manual'})
    second=store.action({'action':'create_task','goal_id':goal['id'],'title':'撰写报告','depends_on':[first['id']],'verification_mode':'manual'})
    return store,goal,first,second


def current(store,item):return store.read()['tasks'][item['id']]
def edit(store,item,**fields):
    now=current(store,item)
    return store.action({'action':'update_task','goal_id':now['goal_id'],'task_id':now['id'],'revision':now['revision'],**fields})
def accept(store,item):
    now=current(store,item)
    return store.action({'action':'accept_task','goal_id':now['goal_id'],'task_id':now['id'],'revision':now['revision'],'reason':'操作者独立核对'})


def test_cosmetic_task_changes_preserve_context_acceptance_and_downstream(tmp_path):
    store,goal,first,second=setup(tmp_path)
    first=accept(store,first);second=accept(store,second)
    edited=edit(store,first,title='资料准备',context_notes='备注：资料归档到同一目录')
    assert edited['revision']>first['revision']
    assert edited['spec_version']==first['spec_version'] and edited['session_id']==first['session_id']
    assert edited['validity']=='fresh' and current(store,second)==second


def test_planner_revisions_invalidate_existing_downstream_drafts_and_preserve_other_goal(tmp_path):
    store,goal,first,second=setup(tmp_path)
    store.record_progress(goal['id'],second['id'],second['revision'],'review','已提交草稿',{'evidence_id':'ev_fixture'})
    other=store.action({'action':'create_goal','title':'其他目标'})
    foreign=store.action({'action':'create_task','goal_id':other['id'],'title':'不可串扰'})
    revision=store.read()['goals'][goal['id']]['revision']
    store.plan(goal['id'],[{'id':first['id'],'title':first['title'],'acceptance':'必须包含原文页码'}],revision)
    changed=current(store,second)
    assert changed['validity']=='stale' and changed['status']=='planned'
    assert changed['deliverables'][0]['evidence_id']=='ev_fixture'
    assert changed['invalidated_reason']=='plan_contract_changed'
    assert changed['session_id']!=second['session_id'] and current(store,foreign)==foreign


def test_planner_cannot_overwrite_a_planned_node_with_prior_deliverables(tmp_path):
    store,goal,first,second=setup(tmp_path)
    store.record_progress(goal['id'],second['id'],second['revision'],'review','草稿',{'evidence_id':'ev_fixture'})
    edit(store,second,acceptance='新版验收')
    before=store.read()
    with pytest.raises(GoalError):store.plan(goal['id'],[{'id':second['id'],'title':'覆盖历史'}],before['goals'][goal['id']]['revision'])
    assert store.read()==before


@pytest.mark.parametrize('action,extra',[('archive_task',{}),('set_status',{'status':'cancelled'}),('set_status',{'status':'blocked'})])
def test_retired_dependencies_revoke_downstream_acceptance_and_stale_bindings(tmp_path,action,extra):
    store,goal,first,second=setup(tmp_path)
    first=accept(store,first);accept(store,second)
    binding=store.binding(goal['id'],second['id'],'execute')
    store.action({'action':action,'goal_id':goal['id'],'task_id':first['id'],'revision':first['revision'],**extra})
    assert current(store,second)['validity']=='stale'
    with pytest.raises(GoalError):store.binding(goal['id'],second['id'],'execute')
    with pytest.raises(GoalError):store.start(binding,'old-binding')


def test_replanning_cycle_failure_is_atomic(tmp_path):
    store,goal,first,second=setup(tmp_path);before=store.read()
    with pytest.raises(GoalError):
        store.plan(goal['id'],[{'id':first['id'],'title':'环路','depends_on':[second['id']]}],before['goals'][goal['id']]['revision'])
    assert store.read()==before


def test_dependency_becoming_active_during_run_cannot_be_reported_as_accepted(tmp_path):
    store,goal,first,second=setup(tmp_path);accept(store,first)
    binding=store.binding(goal['id'],second['id'],'execute');store.start(binding,'child-run')
    upstream=store.binding(goal['id'],first['id'],'execute');store.start(upstream,'new-upstream-run')
    assert not store.finish(binding,{'id':'child-run','status':'completed','answer':'passed','verification':{'status':'passed'}})
    assert current(store,second)['status']=='blocked'
    assert current(store,second)['invalidated_reason']=='dependency_changed_during_run'


def test_current_contract_cannot_be_silently_cut_when_mandatory_context_is_too_large(tmp_path):
    from pico.native_context import ContextBudgetExceeded
    store,goal,first,second=setup(tmp_path);accept(store,first)
    edit(store,second,constraints='不可删除的用户限制。'*150)
    context=store.context(store.binding(goal['id'],second['id'],'execute'))
    conversation=NativeToolConversation({},'host rules','继续',None)
    conversation.context_state={'goal_tree':context};conversation.context_budget=NativeContextBudget(2048,4096)
    with pytest.raises(ContextBudgetExceeded):
        conversation.prepare_payload({'max_tokens':512,**conversation.payload()})


def test_compacted_history_exposes_exact_pointer_omissions_under_small_remaining_budget():
    from test_long_task_context import turn
    from pico.tools import BASE_TOOL_SPECS
    conversation=NativeToolConversation(BASE_TOOL_SPECS,'rules','do not modify tests',None)
    conversation.context_state={'verification':{'status':'failed'}}
    for index in range(12):turn(conversation,index,'line evidence '*600)
    from pico.native_context import encoded_size
    payload={'max_tokens':1024,**conversation.payload()}
    base={**payload,'messages':payload['messages'][:3]+payload['messages'][-2:]}
    conversation.context_budget=NativeContextBudget(encoded_size(base)+500,65536)
    wire=conversation.prepare_payload({'max_tokens':1024,**conversation.payload()})
    audit=conversation.last_context_audit
    assert audit['compacted_groups']>0 and audit['after_estimate']<=conversation.context_budget.input_budget
    selection=audit['evidence_selection']
    assert selection.get('previews_omitted_for_budget',0)>0


def test_frontier_and_contract_survive_compaction_recovery_and_goal_switch(tmp_path):
    store,goal,first,second=setup(tmp_path);accept(store,first)
    binding=store.binding(goal['id'],second['id'],'execute');store.start(binding,'interrupted')
    assert GoalStore(tmp_path).recover()==[second['id']]
    ctx=store.context(store.binding(goal['id'],second['id'],'execute'))
    other=store.action({'action':'create_goal','title':'保密目标','constraints':'OTHER_GOAL_MUST_NOT_APPEAR'})
    capsule=ctx['continuity']
    assert capsule['contracts'][0]['constraints']==goal['constraints']
    assert next(t for t in capsule['frontier'] if t['id']==second['id'])['status']=='blocked'
    conversation=NativeToolConversation({},'host rules','继续报告，不编造数据',None)
    conversation.context_budget=NativeContextBudget(11000,20000)
    conversation.context_state={'goal_tree':ctx}
    for i in range(20):conversation.messages.append({'role':'user','content':f'无关临时通知 {i} '+'noise '*400})
    wire=conversation.prepare_payload({'max_tokens':512,**conversation.payload()})
    raw=json.dumps(wire,ensure_ascii=False)
    assert goal['constraints'] in raw and 'OTHER_GOAL_MUST_NOT_APPEAR' not in raw
    assert conversation.last_context_audit['compacted_groups']>0
    assert 'frontier_omitted' in raw and other['id'] not in raw


def test_compaction_preserves_early_relevant_evidence_instead_of_recent_noise():
    events=[{'tool':'read_file','path':'audit_contract.py','result_preview':'Unique receipt contract','sha256':'a','call_id':'critical'}]
    events.extend({'tool':'read_file','path':f'noise{i}.py','result_preview':'irrelevant','sha256':str(i),'call_id':str(i)} for i in range(16))
    selected,audit=select_events(events,'inspect audit_contract receipt',8)
    assert 'critical' in audit['selected_call_ids'] and len(selected)<=8
    assert audit['omitted_events']==9


def test_last_mile_context_reduction_keeps_early_critical_read_pointer():
    from test_long_task_context import turn
    from pico.native_context import encoded_size
    from pico.tools import BASE_TOOL_SPECS
    conversation=NativeToolConversation(BASE_TOOL_SPECS,'rules','inspect file0.py receipt',None)
    conversation.context_state={'verification':{'status':'failed'}}
    for i in range(12):turn(conversation,i,'line evidence '*600+f'\nEvidence ID: ev_{i:032x}')
    payload={'max_tokens':1024,**conversation.payload()}
    base={**payload,'messages':payload['messages'][:3]+payload['messages'][-2:]}
    conversation.context_budget=NativeContextBudget(encoded_size(base)+900,65536)
    wire=conversation.prepare_payload(payload)
    summary=next(m for m in wire['messages'] if m.get('content','').startswith('Compacted task evidence'))
    events=json.loads(summary['content'].split('\n',1)[1])['events']
    assert any(e.get('call_id')=='call-0' and e.get('evidence_id')=='ev_'+'0'*32 for e in events)
    assert conversation.last_context_audit['evidence_selection']['events_omitted_for_budget']>0


def test_invalid_unbound_planning_is_rejected_before_collaboration_approval(tmp_path):
    from pico.providers.clients import FakeModelClient
    from pico.runtime import Pico,SessionStore
    from pico.workspace import WorkspaceContext
    agent=Pico(FakeModelClient([]),WorkspaceContext.build(tmp_path),SessionStore(tmp_path/'.pico/sessions'),
               harness_profile='workbench',approval_policy='ask')
    agent.goal_binding={'mode':'assistant','goal_id':''}
    agent.goal_planner=lambda steps:pytest.fail('invalid planning must not mutate state')
    agent.tools.update(agent.capabilities.goal_registry())
    agent.collaboration_gate=lambda *args:pytest.fail('must not ask approval for invalid planning')
    result=agent.execute_tool('plan_tasks',{'steps':'[{"title":"Unbound task"}]'})
    assert result.metadata['tool_status']!='ok' and 'Create the Goal first' in result.content


def test_incremental_index_reuses_parsing_but_detects_same_size_content_and_revoked_paths(tmp_path):
    from pico import retrieval
    source=tmp_path/'a.py';source.write_text('def old(): return 1')
    allowed=[True];index=RepositoryIndex(tmp_path,path_filter=lambda p:allowed[0])
    with patch.object(retrieval,'chunk_file',wraps=retrieval.chunk_file) as chunk:
        index.search('old');index.search('old')
        assert chunk.call_count==1 and index.last_audit['reused_files']==1
        source.write_text('def new(): return 2');index.search('new')
        assert chunk.call_count==2 and all('old' not in c['text'] for c in index.chunks)
        allowed[0]=False;assert index.search('new')==[]
        assert index._file_cache=={}


def test_learning_ranking_filters_scope_and_freshness_before_selection_without_remote_upload(tmp_path):
    s=EvolutionStore(tmp_path)
    def memory(goal,key,body):
        receipt=s.observe(key,category='feedback',goal_id=goal,session_id=goal,observation='Host preference receipt')
        item=s.propose('memory',key,body,[receipt['id']],goal_id=goal,session_id=goal)
        s.automatic(goal,goal);return s.get(item['id'])
    public=memory('current','presentation',{'text':'汇报先列证据和来源','conditions':'准备汇报时'})
    private=memory('foreign','private',{'text':'汇报先列 FOREIGN_PRIVATE_CONTENT','conditions':'汇报'})
    class NeverUpload:
        identity='fixture-not-authorized-for-memory';last_usage=None
        def encode(self,texts):pytest.fail('private memories must not reach embedding service')
    index=RepositoryIndex(tmp_path,NeverUpload())
    rows=s.search('汇报证据','current','current',retriever=index)
    assert [r['id'] for r in rows]==[public['id']]
    assert not s.last_search_audit['dense_allowed']
    assert private['id'] not in s.last_search_audit['selected_ids']
