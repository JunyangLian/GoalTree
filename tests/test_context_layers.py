"""Scope and bounded runtime invariants, with no external model calls."""
import copy
import json
from types import SimpleNamespace

import pytest

from pico.goals import GoalStore
from pico.handoff import dispatch, validate, response, HandoffError
from pico.native_context import NativeContextBudget, ContextBudgetExceeded, protocol_groups
from pico.session_context import start, observe, protected_view
from pico.tool_executor import ToolExecutionResult


def setup(root):
    store = GoalStore(root)
    g = store.action({'action':'create_goal','title':'Research','constraints':'PUBLIC_ONLY','acceptance':'CITE_SOURCES'})
    parent = store.action({'action':'create_task','goal_id':g['id'],'title':'Stage','constraints':'ANCESTOR_NO_INVENTION'})
    task = store.action({'action':'create_task','goal_id':g['id'],'parent_id':parent['id'],'title':'Read evidence',
                         'acceptance':'EXACT_ACCEPTANCE','verification_mode':'manual'})
    return store,g,parent,task,store.binding(g['id'],task['id'],'execute')


def test_handoff_excludes_foreign_history_and_preserves_ancestor_constraints(tmp_path):
    store,g,parent,task,binding = setup(tmp_path)
    store.action({'action':'create_goal','title':'FOREIGN_SECRET'})
    envelope = dispatch(store,binding,'Inspect only', 'policy1')
    wire = json.dumps(envelope)
    assert 'FOREIGN_SECRET' not in wire
    assert all(s in wire for s in ('PUBLIC_ONLY','ANCESTOR_NO_INVENTION','EXACT_ACCEPTANCE'))
    assert envelope['target']['session_id'] == task['session_id'] != g['chat_session_id']
    validate(store,envelope,binding,'policy1')
    with pytest.raises(HandoffError):dispatch(store,binding,'Inspect only','policy1',max_bytes=10)
    with pytest.raises(HandoffError):dispatch(store,{**binding,'session_id':'forged'},'Inspect only','policy1')


@pytest.mark.parametrize('kind', ['workspace','session','policy','tamper','ancestor','dependency'])
def test_handoff_rejects_wrong_scope_integrity_and_stale_contracts(tmp_path,kind):
    store,g,parent,task,binding = setup(tmp_path)
    if kind == 'dependency':
        dep = store.action({'action':'create_task','goal_id':g['id'],'title':'Prerequisite'})
        store.action({'action':'accept_task','goal_id':g['id'],'task_id':dep['id'],'revision':dep['revision'],'reason':'Host inspected'})
        task = store.action({'action':'update_task','goal_id':g['id'],'task_id':task['id'],'revision':task['revision'],'depends_on':[dep['id']]})
        binding = store.binding(g['id'],task['id'],'execute')
    envelope = dispatch(store,binding,'Inspect','p1')
    target = copy.deepcopy(binding);policy = 'p1';other = store
    if kind=='workspace':
        other = GoalStore(tmp_path/'other')
    elif kind=='session':target['session_id']='foreign'
    elif kind=='policy':policy='p2'
    elif kind=='tamper':envelope['instruction']='changed'
    elif kind=='ancestor':
        store.action({'action':'update_task','goal_id':g['id'],'task_id':parent['id'],'revision':parent['revision'],'context_notes':'Changed ancestor background'})
    else:
        now=store.read()['tasks'][dep['id']]
        store.action({'action':'update_task','goal_id':g['id'],'task_id':dep['id'],'revision':now['revision'],'context_notes':'Changed dependency summary'})
    with pytest.raises(ValueError):validate(other,envelope,target,policy)


def test_bounded_return_never_truncates_verification_or_confuses_manual_review(tmp_path):
    store,g,parent,task,binding = setup(tmp_path)
    envelope = dispatch(store,binding,'Inspect','p')
    verification={'status':'not_configured','command':None}
    details={'id':'run','run_id':'r','binding':binding,'status':'completed','verification':verification,
             'answer':'actual '*5000,'error':'','tool_steps':2}
    node={**task,'status':'review','validity':'unknown','acceptance_origin':'unverified'}
    result=response(envelope,details,node,'ev_full')
    assert len(json.dumps(result,ensure_ascii=False))<=3400
    assert result['verification']==verification and result['status']=='completed'
    assert result['acceptance']['status']=='review' and result['answer_truncated']
    assert details['answer']=='actual '*5000
    details['verification']={'status':'failed','command':'x'*4000}
    with pytest.raises(HandoffError):response(envelope,details,node,'ev_full')


def agent(root):
    return SimpleNamespace(session={'id':'own'},goal_binding={'goal_id':'g','task_id':'t','goal_spec_version':1,'task_spec_version':1},
                           path=lambda path:root/path)


def tool(content, **metadata):
    return ToolExecutionResult(content,{'tool_status':'ok',**metadata})


def test_read_receipts_merge_pages_keep_original_refs_and_detect_file_changes(tmp_path):
    import hashlib
    a=agent(tmp_path);start(a,'run1');p=tmp_path/'source.txt';p.write_text('a\nb\nc\nd\n')
    sha=hashlib.sha256(p.read_bytes()).hexdigest()
    for low,high,ref in ((1,2,'ev_first'),(3,4,'ev_second')):
        content=f'# source.txt (4 total lines) sha256={sha}\n'+'\n'.join(f'{i:4}: line' for i in range(low,high+1))
        observe(a,'read_file',{'path':'source.txt'},tool(content,evidence_id=ref),{'status':'not_run'})
    row=protected_view(a)['reads']['source.txt']
    assert row['full_file_returned'] and row['returned_ranges']==[[1,4]] and row['source_current']
    assert [r['evidence_id'] for r in row['evidence_refs']]==['ev_first','ev_second']
    p.write_text('new')
    assert not protected_view(a)['reads']['source.txt']['source_current']
    observe(a,'read_file',{'path':'source.txt'},tool('# source.txt (1 total lines)\n   1: new'),{})
    assert not protected_view(a)['reads']['source.txt']['source_current']  # Missing hash is not freshness.


def test_exact_errors_survive_restart_until_same_operation_succeeds(tmp_path):
    a=agent(tmp_path);start(a,'run1')
    error='Traceback: start\n'+'middle'*900+'\nAssertionError: EXPECTED_TAIL'
    observe(a,'run_shell',{'command':'verify'},tool(error,exit_code=1,evidence_id='ev_failure'),{'status':'failed'})
    # Successful unrelated tool is not proof the failed test was fixed.
    observe(a,'read_file',{'path':'else.txt'},tool('content'),{'status':'failed'})
    b=agent(tmp_path);b.session=json.loads(json.dumps(a.session));start(b,'run2')
    view=protected_view(b);failure=next(iter(view['errors'].values()))
    assert failure['head']==error[:900] and failure['tail']==error[-900:]
    assert failure['omitted_chars']==len(error)-1800 and failure['evidence_id']=='ev_failure'
    assert view['verification']['status']=='not_run'
    observe(b,'run_shell',{'command':'verify'},tool('missing exit code'),{'status':'not_run'})
    assert protected_view(b)['errors']  # An unknown outcome cannot clear a failure.
    observe(b,'run_shell',{'command':'verify'},tool('exit_code: 0',exit_code=0),{'status':'passed'})
    assert protected_view(b)['errors']=={}
    b.session['id']='foreign'
    with pytest.raises(ValueError):protected_view(b)


def test_host_rebind_allows_first_goal_but_never_other_goal_or_task(tmp_path):
    from pico.session_context import rebind
    a=agent(tmp_path);a.goal_binding={};start(a,'r1')
    a.goal_binding={'goal_id':'g','task_id':'','goal_spec_version':1,'task_spec_version':None}
    rebind(a)
    assert protected_view(a)['scope']['goal_id']=='g'
    assert protected_view(a)['verification']['status']=='stale'
    a.goal_binding['goal_id']='other'
    with pytest.raises(ValueError):rebind(a)


def test_multimodal_mandatory_frame_is_never_treated_as_optional_memory():
    content=[{'type':'text','text':'CURRENT_CONSTRAINT'},{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}]
    payload={'messages':[{'role':'system','content':'rules'},{'role':'user','content':content}]}
    prepared,audit=NativeContextBudget().prepare(payload,2)
    assert prepared==payload and audit['protected_frames_preserved']


def test_missing_cache_usage_is_unknown_not_a_measured_miss():
    from pico.providers.clients import _extract_usage_cache_details
    assert _extract_usage_cache_details({})['observed_cache_hit'] is None
    explicit = _extract_usage_cache_details({'usage':{'prompt_tokens_details':{'cached_tokens':0}}})
    assert explicit['cache_usage_reported'] and explicit['observed_cache_hit'] is False
    assert _extract_usage_cache_details({'usage':{'prompt_tokens_details':{'cached_tokens':32}}})['observed_cache_hit'] is True


def test_soft_threshold_compacts_before_hard_limit_and_preserves_mandatory_state():
    from pico.providers.native_tools import NativeToolConversation
    from pico.tools import BASE_TOOL_SPECS
    c=NativeToolConversation({'read_file':BASE_TOOL_SPECS['read_file']},'workspace','CURRENT_REQUEST',None,require_reasoning=False)
    c.set_budget(10);c.context_rules='HOST_PERMISSION_BOUNDARY'
    c.context_state={'contracts':{'acceptance':'EXACT_ACCEPTANCE','ancestor':'ANCESTOR_NO_INVENTION'},
                     'session_retention':{'errors':{'e':{'tail':'EXACT_ERROR_TAIL'}},'reads':{'file':{'full_file_returned':True}}}}
    for i in range(5):
        c.accept({'choices':[{'message':{'tool_calls':[{'id':str(i),'function':{'name':'read_file','arguments':'{"path":"file"}'},'type':'function'}]}}]}, {})
        c.observe('source '*350)
    payload={'max_tokens':512,**c.payload()}
    from pico.native_context import encoded_size
    size=encoded_size(payload);c.context_budget=NativeContextBudget(int(size*1.1),int(size*2))
    prepared=c.prepare_payload(payload);audit=c.last_context_audit
    assert size<audit['input_budget'] and size>audit['compaction_trigger']
    assert audit['compacted_groups']>0 and audit['protected_frames_preserved']
    wire=json.dumps(prepared)
    assert all(text in wire for text in ('CURRENT_REQUEST','EXACT_ACCEPTANCE','ANCESTOR_NO_INVENTION','EXACT_ERROR_TAIL','HOST_PERMISSION_BOUNDARY'))
    protocol_groups(prepared['messages'][5:])
    # Stable components do not include changing per-turn budget/current state.
    fingerprint=audit['stable_prefix_sha256'];c.context_state['dynamic']='changed';c.set_budget(9)
    c.prepare_payload({'max_tokens':512,**c.payload()})
    assert c.last_context_audit['stable_prefix_sha256']==fingerprint


def test_mandatory_error_overflow_stops_instead_of_silent_drop():
    payload={'messages':[{'role':'system','content':'policy'},{'role':'user','content':'task'},
                         {'role':'user','content':'EXACT_ERROR '*1000}]}
    with pytest.raises(ContextBudgetExceeded):NativeContextBudget(2048,4096).prepare(payload,3)


def test_compaction_commits_after_complete_response_not_prepare_and_does_not_nest_summaries():
    from pico.providers.native_tools import NativeToolConversation
    from pico.native_context import encoded_size, group_fact
    from pico.tools import BASE_TOOL_SPECS
    c=NativeToolConversation({'read_file':BASE_TOOL_SPECS['read_file']},'workspace','request',None,require_reasoning=False)
    for i in range(6):
        c.accept({'choices':[{'message':{'tool_calls':[{'id':str(i),'type':'function','function':{'name':'read_file','arguments':'{"path":"file"}'}}]}}]}, {})
        c.observe('public '*600)
    raw=copy.deepcopy(c.messages);size=encoded_size(c.payload())
    c.context_budget=NativeContextBudget(int(size*1.05),int(size*2))
    prepared=c.prepare_payload(c.payload())
    assert c.messages==raw and c.last_context_audit['compacted_groups']
    c.accept({'choices':[{'message':{'content':'Complete response'}}]}, {})
    assert len(c.messages)<len(raw)
    assert sum((m.get('content') or '').startswith('Compacted task evidence') for m in c.messages)==1
    summary=next(m for m in prepared['messages'] if (m.get('content') or '').startswith('Compacted task evidence'))
    facts=group_fact([summary])
    assert facts and all('notice' not in e for e in facts)
    assert 'Compacted task evidence' not in json.dumps(facts)


def test_real_coordinator_worker_uses_bounded_handoff_and_independent_sessions(tmp_path):
    from pico.web import Workbench
    from pico.providers.clients import FakeModelClient
    class Model(FakeModelClient):
        def __init__(self):super().__init__([]);self.calls=0
        def complete(self,prompt,*args,**kwargs):
            self.calls+=1
            context=json.JSONDecoder().raw_decode(prompt.split('GoalTree current scoped task state:\n',1)[1])[0]
            if context['mode']=='execute':return '<final>Actual scripted worker output.</final>'
            if self.calls==1:
                return '<tool>'+json.dumps({'name':'run_task','args':{'task_id':context['existing_tasks'][0]['id']}})+'</tool>'
            return '<final>Worker finished; manual acceptance remains pending.</final>'
    app=Workbench(tmp_path,model_factory=Model,harness_profile='workbench',load_env=False)
    g=app.goals.action({'action':'create_goal','title':'Scoped objective'})
    t=app.goals.action({'action':'create_task','goal_id':g['id'],'title':'Inspect','verification_mode':'manual'})
    parent=app.task(app.submit({'goal_id':g['id'],'mode':'assistant','prompt':'Execute','max_steps':6,'policy':'auto'})['id'])
    with parent.condition:
        assert parent.condition.wait_for(lambda:parent.finished is not None and app.active_id is None,timeout=30)
    assert parent.status=='completed',parent.error
    child=next(item for item in app.tasks.values() if item.id!=parent.id)
    assert child.session_id==t['session_id']!=parent.session_id
    assert child.handoff['target']['task_id']==t['id']
    receipts=[e for e in parent.events if e['event']=='handoff_received']
    assert len(receipts)==1 and receipts[0]['acceptance']['status']=='review'
    result=next(e for e in parent.events if e['event']=='tool_executed' and e['name']=='run_task')
    full=parent.agent.evidence.get(result['evidence_id'],parent.session_id)['content']
    answer=json.loads(full)
    assert answer['handoff_id']==child.handoff['id'] and answer['detail_evidence_id']
    with pytest.raises(ValueError):child.agent.evidence.get(answer['detail_evidence_id'],child.session_id)
