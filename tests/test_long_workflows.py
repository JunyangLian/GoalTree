"""Small-scale long lifecycle regression; no claim of model task success."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from pico.evidence import EvidenceStore
from pico.goals import GoalStore, GoalError
from pico.handoff import dispatch, validate
from pico.native_context import NativeContextBudget, protocol_groups
from pico.native_history import historical_requests, historical_deliverables
from pico.session_context import start, observe, protected_view
from pico.tool_executor import ToolExecutionResult

DOMAINS=('fish-report','meeting-plan','code-release')

def receipt(tmp_path, name, rows):
    (tmp_path/(name+'.json')).write_text(json.dumps(rows,ensure_ascii=False,indent=2),'utf-8')

@pytest.mark.parametrize('domain',DOMAINS)
def test_long_history_keeps_chronology_and_reports_preview_omission(tmp_path,domain):
    ev=EvidenceStore(tmp_path);history=[];outputs=[]
    for i in range(24):
        history.append({'role':'user','content':f'{domain} correction {i}'})
        body=f'{domain} answer {i}: '+('public details '*100)+f'TAIL_{i}'
        history.append({'role':'assistant','content':body,'run_id':f'run-{i}'})
        ref=ev.put('own',f'run-{i}','conversation_deliverable',body,{'ordinal':len(history)-1})
        outputs.insert(0,{'evidence_id':ref,'run_id':f'run-{i}'})
    history.append({'role':'user','content':'current request'})
    agent=SimpleNamespace(session={'id':'own','history':history},evidence=ev,goal_context={'recent_deliverables':outputs[:3]})
    # All historical user requirements remain recoverable; recent assistant
    # snippets must carry their ordinal and disclose missing tail content.
    users=historical_requests(agent);answers=historical_deliverables(agent)
    assert len(users)==24 and len(answers)==3
    receipt(tmp_path,'history-actual',answers)
    assert all('preview_truncated=true' in r['content'] for r in answers)
    from pico.native_history import historical_turns
    turns=historical_turns(agent)
    assert turns.index(answers[-1])>turns.index(users[-1])
    assert turns.index(answers[-2])<turns.index(users[-1])
    assert all('TAIL_' not in r['content'] for r in answers)
    receipt(tmp_path,'chronological-actual',turns)

@pytest.mark.parametrize('domain',DOMAINS)
def test_long_revision_dependency_and_handoff_cycles(tmp_path,domain):
    s=GoalStore(tmp_path);g=s.action({'action':'create_goal','title':domain})
    other=s.action({'action':'create_goal','title':'untouched'})
    a=s.action({'action':'create_task','goal_id':g['id'],'title':'evidence','verification_mode':'manual'})
    b=s.action({'action':'create_task','goal_id':g['id'],'title':'draft','depends_on':[a['id']],'verification_mode':'manual'})
    rows=[]
    for i in range(20):
        a=s.read()['tasks'][a['id']]
        a=s.action({'action':'accept_task','goal_id':g['id'],'task_id':a['id'],'revision':a['revision'],'reason':'Synthetic operator review'})
        binding=s.binding(g['id'],b['id'],'execute');envelope=dispatch(s,binding,'read only','fixed')
        a=s.action({'action':'update_task','goal_id':g['id'],'task_id':a['id'],'revision':a['revision'],'acceptance':f'CITE_VERSION_{i}'})
        with pytest.raises(ValueError):validate(s,envelope,binding,'fixed')
        with pytest.raises(GoalError):s.binding(g['id'],b['id'],'execute')
        assert s.read()['goals'][other['id']]==other
        rows.append({'cycle':i,'stale_handoff_rejected':True,'dependency_blocked':True,'foreign_unchanged':True})
    receipt(tmp_path,'revision-cycles',rows)

@pytest.mark.parametrize('domain',DOMAINS)
def test_long_read_coverage_restart_and_source_changes(tmp_path,domain):
    path=tmp_path/'source.txt';rows=[]
    a=SimpleNamespace(session={'id':'own'},goal_binding={'goal_id':domain,'task_id':'read'},path=lambda p:tmp_path/p)
    start(a,'run-0')
    for i in range(60):
        text=f'{domain} revision {i//10}\n';path.write_text(text,'utf-8');sha=hashlib.sha256(path.read_bytes()).hexdigest()
        result=ToolExecutionResult(f'# source.txt (1 total lines) sha256={sha}\n   1: {text}',
            {'tool_status':'ok','evidence_id':'ev_'+str(i)})
        observe(a,'read_file',{'path':'source.txt'},result,{'status':'not_run'})
        if i%10==9:
            a.session=json.loads(json.dumps(a.session));start(a,f'run-{i}')
        view=protected_view(a);read=view['reads']['source.txt']
        assert read['source_current'] and read['full_file_returned'] and view['verification']['status']=='not_run'
        assert len(read['evidence_refs'])<=10
        rows.append({'step':i,'hash':read['source_sha256'],'refs':len(read['evidence_refs'])})
    path.write_text('changed after observation','utf-8')
    assert not protected_view(a)['reads']['source.txt']['source_current']
    receipt(tmp_path,'read-cycles',rows)

@pytest.mark.parametrize('domain',DOMAINS)
def test_long_native_pressure_preserves_atomic_groups(tmp_path,domain):
    from pico.providers.native_tools import NativeToolConversation
    c=NativeToolConversation({},'contract '+domain,'current acceptance EXACT','',require_reasoning=False)
    c.context_budget=NativeContextBudget(9000,18000);c.context_state={'ancestor':'PUBLIC_ONLY','error':'EXACT_ERROR'}
    rows=[]
    for i in range(60):
        c.accept({'choices':[{'message':{'tool_calls':[{'id':str(i),'type':'function','function':{'name':'read_file','arguments':'{"path":"public.txt"}'}}]}}]}, {})
        c.observe('public data '+str(i)+' x'*900)
        prepared=c.prepare_payload({**c.payload(),'max_tokens':100})
        protocol_groups(prepared['messages']);audit=c.last_context_audit
        assert audit['after_estimate']<=9000 and audit['protected_frames_preserved']
        c.accept({'choices':[{'message':{'content':'fixture response'}}]}, {})
        rows.append({'step':i,'compacted':audit['compacted_groups'],'bytes':audit['after_estimate']})
    assert sum(r['compacted'] for r in rows)>0
    receipt(tmp_path,'pressure-cycles',rows)

@pytest.mark.parametrize('domain',DOMAINS)
def test_long_memory_consent_mutation_and_privacy_cycles(tmp_path,domain):
    from pico.web import Workbench
    from pico.providers.clients import FakeModelClient
    from pico.shared_memory import SharedMemoryRuntime
    from pico.extensions import PolicyChanged
    app=Workbench(tmp_path,load_env=False,harness_profile='workbench',model_factory=lambda:FakeModelClient([]),privacy_protection=True)
    source=app.goals.action({'action':'create_goal','title':domain+' source','context_notes':domain+' public version 0'})
    target=app.goals.action({'action':'create_goal','title':domain+' target'})
    def consent(g,on):
        old=app.shared_memory.preference(g['chat_session_id'])
        app.set_memory_sharing({'goal_id':g['id'],'session_id':g['chat_session_id'],'revision':old['revision'],'enabled':on})
    consent(source,True);consent(target,True);rows=[]
    def runtime():
        a=app._build_agent(target['chat_session_id'],'ask',3);a.goal_binding=app.goals.binding(target['id'],'','assistant')
        return SharedMemoryRuntime(a,app.shared_memory,app.goals)
    for i in range(20):
        old=runtime();assert old.prepare(domain)
        current=app.goals.read()['goals'][source['id']]
        source=app.goals.action({'action':'update_goal','goal_id':source['id'],'revision':current['revision'],'context_notes':f'{domain} public version {i+1}'})
        with pytest.raises(PolicyChanged):old.prepare(domain)
        assert str(i+1) in runtime().prepare(domain)[0]['text']
        consent(source,False);assert runtime().prepare(domain)==[]
        consent(source,True)
        rows.append({'cycle':i,'revoked_runtime_rejected':True,'off_filtered':True})
    app.privacy_store.label('goal:'+source['id'],'core');assert runtime().prepare(domain)==[]
    with pytest.raises(ValueError):app.privacy_store.label('goal:'+source['id'],'public')
    receipt(tmp_path,'sharing-cycles',rows)


def test_current_request_follows_history_on_wire_without_duplicate_or_tool_reordering():
    from pico.providers.native_tools import NativeToolConversation
    c=NativeToolConversation({},'current context','ONLY_LATEST_CORRECTION','',require_reasoning=False)
    history=[{'role':'user','content':'Historical user request #0\nREAD_OLD_FILE'},
             {'role':'user','content':'Historical assistant deliverable #1\nOLD_ANSWER'}]
    c.messages.extend(history);c.set_budget(5);c.context_state={'acceptance':'EXACT_ACCEPTANCE'}
    original=json.loads(json.dumps(c.messages))
    c.accept({'choices':[{'message':{'reasoning_content':'private live reasoning','tool_calls':[{'id':'call','type':'function','function':{'name':'read_file','arguments':'{}'}}]}}]}, {})
    c.observe('actual tool receipt')
    prepared=c.prepare_payload({**c.payload(),'max_tokens':100})
    wire=prepared['messages'];text=json.dumps(wire)
    assert text.count('ONLY_LATEST_CORRECTION')==1
    assert text.index('OLD_ANSWER')<text.index('ONLY_LATEST_CORRECTION')<text.index('private live reasoning')
    protocol_groups(wire)
    assert c.messages[:len(original)]==original
    assert [wire[i] for i in c.last_context_audit['protected_frame_wire_indices']]==c.payload()['messages'][:4]


def test_ordered_compaction_commits_private_tail_without_dropping_current_request():
    from pico.providers.native_tools import NativeToolConversation
    c=NativeToolConversation({},'context','CURRENT_PINNED','',require_reasoning=False)
    c.context_budget=NativeContextBudget(4500,9000)
    for i in range(30):c.messages.append({'role':'user','content':f'Historical user request #{i}\n'+('old '*200)})
    prepared=c.prepare_payload({**c.payload(),'max_tokens':100})
    assert c.last_context_audit['compacted_groups']>0 and c.last_context_audit['current_request_after_history']
    assert json.dumps(prepared).count('CURRENT_PINNED')==1
    c.accept({'choices':[{'message':{'content':'actual final'}}]}, {})
    again=c.prepare_payload({**c.payload(),'max_tokens':100})
    assert json.dumps(again).count('CURRENT_PINNED')==1
    assert len(c.messages)<32
    protocol_groups(again['messages'])


def test_fresh_shared_background_and_host_state_follow_stale_chat():
    from pico.providers.native_tools import NativeToolConversation
    c=NativeToolConversation({},'workspace','QUERY_SHARED_ITEM','',require_reasoning=False)
    c.messages.extend([{'role':'user','content':'Historical assistant deliverable #1\nOLD_OWN_GOAL_IDENTIFIER'}])
    c.context_state={'constraint':'CURRENT_HOST_CONTRACT'}
    c.context_memory=[{'kind':'shared_advisory','text':'SHARED_ITEM_IDENTIFIER','source_goal_id':'source','conditions':'Public background only'}]
    wire=c.prepare_payload({**c.payload(),'max_tokens':100})['messages'];text=json.dumps(wire)
    assert text.index('OLD_OWN_GOAL_IDENTIFIER')<text.index('CURRENT_HOST_CONTRACT')<text.index('SHARED_ITEM_IDENTIFIER')<text.index('QUERY_SHARED_ITEM')
    assert text.count('SHARED_ITEM_IDENTIFIER')==1


def test_real_two_turn_runtime_does_not_pin_previous_request_as_current_state(tmp_path):
    from pico.web import Workbench
    from pico.providers.clients import OpenAICompatibleModelClient
    captured=[]
    class NativeFixture(OpenAICompatibleModelClient):
        def __init__(self):super().__init__('test','https://api.deepseek.com/v1','offline-fixture',1,30,'chat_completions')
        def complete(self,*args,**kwargs):
            payload=self.native_conversation.prepare_payload({**self.native_conversation.payload(),'max_tokens':100})
            captured.append(payload)
            return self.native_conversation.accept({'choices':[{'message':{'content':'Offline fixture answer','reasoning_content':'fixture only'}}]}, {})
    app=Workbench(tmp_path,model_factory=NativeFixture,load_env=False,harness_profile='workbench')
    g=app.goals.action({'action':'create_goal','title':'Public background'})
    for prompt in ('PREVIOUS_LOCAL_ONLY_REQUEST','CURRENT_AUTHORIZED_BACKGROUND_REQUEST'):
        task=app.task(app.submit({'goal_id':g['id'],'session_id':g['chat_session_id'],'mode':'assistant','prompt':prompt,'max_steps':3})['id'])
        with task.condition:assert task.condition.wait_for(lambda:task.finished is not None and app.active_id is None,20)
        assert task.view()['status']=='completed'
    current=next(m['content'] for m in captured[-1]['messages'] if 'Current user request:' in (m.get('content') or ''))
    assert 'CURRENT_AUTHORIZED_BACKGROUND_REQUEST' in current
    assert 'PREVIOUS_LOCAL_ONLY_REQUEST' not in current
    assert 'PREVIOUS_LOCAL_ONLY_REQUEST' in json.dumps(captured[-1])
    assert 'explicitly authorized retrieved background' in current
    assert 'does not authorize modifying that Goal' in current
