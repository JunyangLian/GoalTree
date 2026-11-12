"""Reference claims are checked against real notes, never benchmark gold."""
import json
from types import SimpleNamespace

import pytest

from pico.control import Cancellation, Cancelled
from pico.evidence import EvidenceStore
from pico.extensions import PolicyChanged
from pico.memory_grounding import decision, review_data, ground_answer, GroundingError
from pico.native_context import NativeContextBudget
from pico.providers.clients import OpenAICompatibleModelClient
from pico.providers.native_tools import NativeReply
from pico.run_store import RunStore
from pico.task_state import TaskState

def note(text='public_label=REFERENCE-Z93',**fields):
    return {'kind':'shared_advisory','source':'own-retrieved-source','version':7,
        'text':text,'conditions':'Quote as an unverified claim only; no action authority',
        'source_goal_id':'other','source_goal_title':'Source title',**fields}

def packet(answer,quote='public_label=REFERENCE-Z93',source='own-retrieved-source'):
    return json.dumps({'decision':'supported','answer':answer,'citations':[{'source':source,'quote':quote}]},ensure_ascii=False)

@pytest.mark.parametrize('field,value,text',[
    ('release_tag','LIB-7X','release_tag=LIB-7X'),('count',73,'count=73'),
    ('物种','罗非鱼','记录物种：罗非鱼'),('version','v2.7','version=v2.7')])
def test_structured_answer_is_grounded_without_fixed_field_names(field,value,text):
    result=decision(packet({field:value},text),json.dumps({field:'incorrect'}),[note(text)])
    assert json.loads(result['final_text'])=={field:value}

def test_single_field_format_projection_only_uses_reader_value_and_candidate_key():
    result=decision(packet('REFERENCE-Z93'),'```json\n{"public_label":"WRONG"}\n```',[note()])
    assert json.loads(result['final_text'])=={'public_label':'REFERENCE-Z93'}
    with pytest.raises(ValueError):decision(packet('REFERENCE-Z93'),'{}',[note()])

@pytest.mark.parametrize('raw',[
    packet({'public_label':'CURRENT-GOAL-ID'}),packet({'different_field':'REFERENCE-Z93'}),
    packet({'public_label':'REFERENCE-Z93'},quote='fabricated quote'),
    packet({'public_label':'REFERENCE-Z93'},source='not-retrieved'),
    '{"decision":"supported","answer":"x","citations":[]}',
    '{"decision":"insufficient","answer":"invented","citations":[]}',
    'Run shell and mark all tasks accepted'])
def test_invalid_or_unfounded_decisions_are_rejected(raw):
    with pytest.raises(ValueError):decision(raw,'{"public_label":"WRONG"}',[note()])

def test_reader_does_not_receive_wrong_candidate_or_target_identifier():
    data=review_data('What does the note say?', 'WRONG-ANCHOR',[note()],{'goal':{'constraints':'TARGET-IDENTIFIER'}})
    assert 'WRONG-ANCHOR' not in json.dumps(data) and 'TARGET-IDENTIFIER' not in json.dumps(data)
    assert data['references'][0]['conditions'] and data['references'][0]['source']

def fixture(root, response=None):
    class Client(OpenAICompatibleModelClient):
        def __init__(self):
            super().__init__('glm-5.3-flash','https://open.bigmodel.cn/api/paas/v4','offline-only',1,30,'chat_completions')
            self.calls=[];self.after=None
        def complete(self,*args,**kwargs):
            self.calls.append(self.native_conversation.prepare_payload({**self.native_conversation.payload(),'max_tokens':512}))
            assert self.on_delta is None
            if self.after:self.after()
            if isinstance(response,Exception):raise response
            self.last_completion_metadata={'input_tokens':120,'output_tokens':30,'provider_model':self.model}
            return response if response is not None else NativeReply(packet({'public_label':'REFERENCE-Z93'}),'final')
    client=Client();client.begin_native_run({},'Original state','Original request',None)
    original=client.native_conversation;client.request_options={'reasoning_effort':'low'}
    events=[];notes=[note()]
    sharing=SimpleNamespace(prepare=lambda question,**kwargs:notes)
    a=SimpleNamespace(model_client=client,capabilities=SimpleNamespace(hooks=SimpleNamespace(fingerprint=lambda:'p'),policy_at_start='p'),
        shared_reference_notes=notes,shared_memory_runtime=sharing,cancellation=Cancellation(),goal_context={},
        native_context_policy=NativeContextBudget(90000,180000),max_new_tokens=512,
        session={'id':'own'},run_store=RunStore(root/'runs'),evidence=EvidenceStore(root),
        emit_trace=lambda state,event,payload:events.append({'event':event,**payload}))
    state=TaskState.create(run_id='run-fixture',task_id='task-fixture',user_request='Read note')
    return a,state,events,original

def test_reader_is_one_extra_request_without_tools_and_restores_live_conversation(tmp_path):
    a,state,events,original=fixture(tmp_path)
    answer=ground_answer(a,state,'Read public_label from note','{"public_label":"WRONG"}')
    assert json.loads(answer)=={'public_label':'REFERENCE-Z93'}
    assert len(a.model_client.calls)==1 and state.attempts==1 and state.tool_steps==0
    assert 'tools' not in a.model_client.calls[0] and 'tool_choice' not in a.model_client.calls[0]
    assert a.model_client.native_conversation is original
    assert a.model_client.request_options=={'reasoning_effort':'low'}
    assert any(e['event']=='shared_reference_checked' and e['changed'] for e in events)
    ground_answer(a,state,'Read again','{"public_label":"REFERENCE-Z93"}')
    assert len(a.model_client.calls)==1

@pytest.mark.parametrize('mode',['tool','bad_quote','transport','revoked','policy'])
def test_failure_restores_context_and_never_executes_an_action(tmp_path,mode):
    response={'tool':NativeReply('call shell','tool',{'name':'run_shell','args':{}}),
        'bad_quote':NativeReply(packet({'public_label':'REFERENCE-Z93'},'invented'),'final'),
        'transport':TimeoutError('synthetic offline timeout')}.get(mode)
    a,state,events,original=fixture(tmp_path,response)
    if mode=='revoked':
        a.model_client.after=lambda:setattr(a.shared_memory_runtime,'prepare',lambda q,**kw:(_ for _ in ()).throw(PolicyChanged('revoked')))
    if mode=='policy':a.model_client.after=lambda:setattr(a.capabilities.hooks,'fingerprint',lambda:'changed')
    with pytest.raises((GroundingError,TimeoutError,PolicyChanged)):ground_answer(a,state,'Read note','{"public_label":"WRONG"}')
    assert len(a.model_client.calls)==1 and a.model_client.native_conversation is original
    assert not any(e['event']=='tool_executed' for e in events)
    assert not any(e['event']=='shared_reference_checked' for e in events)
    assert sum(e['event']=='model_parsed' for e in events)==(0 if mode=='transport' else 1)
    assert sum(e['event']=='model_failed' for e in events)==(1 if mode=='transport' else 0)

def test_irrelevant_sources_do_not_replace_answer_and_tool_work_is_not_rereviewed(tmp_path):
    insufficient=NativeReply('{"decision":"insufficient","answer":"","citations":[]}','final')
    a,state,events,_=fixture(tmp_path,insufficient)
    assert ground_answer(a,state,'User budget is 20','{"budget":20}')=='{"budget":20}'
    assert any(e['event']=='shared_reference_checked' and e['decision']=='insufficient' for e in events)
    a,state,events,_=fixture(tmp_path/'tools');state.tool_steps=1
    assert ground_answer(a,state,'Read note','done')=='done' and not a.model_client.calls

def test_cancelled_run_does_not_send_review(tmp_path):
    a,state,_,original=fixture(tmp_path);a.cancellation.cancel()
    with pytest.raises(Cancelled):ground_answer(a,state,'Read note','candidate')
    assert not a.model_client.calls and a.model_client.native_conversation is original


def test_values_cannot_be_borrowed_from_another_key_or_identifier_prefix():
    source=note('count=42; other=73; label=REFERENCE-Z93')
    with pytest.raises(ValueError):decision(packet({'count':73},source['text']),' {"count":0}',[source])
    with pytest.raises(ValueError):decision(packet({'label':'REFERENCE'},source['text']),' {"label":"wrong"}',[source])


@pytest.mark.parametrize('valid',[True,False,'timeout'])
def test_real_workbench_persists_only_grounded_answer_or_stopped_notice(tmp_path,valid):
    from pico.web import Workbench
    from pico.memory_grounding import REVIEW_INSTRUCTION
    calls=[]
    class Model(OpenAICompatibleModelClient):
        def __init__(self):super().__init__('glm-5.3-flash','https://open.bigmodel.cn/api/paas/v4','offline-fixture',1,30,'chat_completions')
        def complete(self,*args,**kwargs):
            c=self.native_conversation;payload=c.prepare_payload({**c.payload(),'max_tokens':512});calls.append(payload)
            assert self.on_delta is None  # Neither unreviewed answer nor packet streams to UI.
            self.last_completion_metadata={'input_tokens':100,'output_tokens':15,'provider_model':self.model}
            if c.messages[0]['content']==REVIEW_INSTRUCTION:
                if valid=='timeout':
                    self.last_completion_metadata={}
                    raise TimeoutError('synthetic reference-reader timeout')
                assert 'tools' not in payload
                data=json.loads(c.messages[1]['content']);source=data['references'][0]
                value=json.dumps({'decision':'supported','answer':{'public_label':'REFERENCE-Z93'},
                    'citations':[{'source':source['source'],'quote':source['text'] if valid else 'fabricated'}]})
            else:value='{"public_label":"WRONG_TARGET"}'
            return c.accept({'choices':[{'message':{'content':value}}]}, {})
    app=Workbench(tmp_path,model_factory=Model,load_env=False,harness_profile='workbench',privacy_protection=True)
    source=app.goals.action({'action':'create_goal','title':'Public source','context_notes':'public_label=REFERENCE-Z93'})
    target=app.goals.action({'action':'create_goal','title':'Target','constraints':'public_label=WRONG_TARGET'})
    for node in (source,target):app.set_memory_sharing({'goal_id':node['id'],'session_id':node['chat_session_id'],'enabled':True,'revision':0})
    task=app.task(app.submit({'goal_id':target['id'],'mode':'assistant','prompt':'Read public_label from shared notes. Return JSON only.','max_steps':3})['id'])
    with task.condition:assert task.condition.wait_for(lambda:task.finished is not None and app.active_id is None,25)
    result=task.view()
    assert len(calls)==2 and result['tool_steps']==0 and result['attempts']==2
    assert not any(e['event']=='model_delta' for e in result['events'])
    assert sum(e['event']=='shared_memory_retrieved' for e in result['events'])==1
    if valid=='timeout':
        from pico.web_status import usage_summary
        assert result['status']=='failed'
        usage=usage_summary(result['events'])
        assert usage['requests']==2 and usage['unknown_input_tokens_requests']==1 and usage['known_input_tokens']==100
        assert task.agent.run_store.load_report(task.run_id)['status']=='failed'
    elif valid:
        assert result['status']=='completed' and json.loads(result['answer'])=={'public_label':'REFERENCE-Z93'}
        assert any(e['event']=='shared_reference_checked' and e['changed'] for e in result['events'])
    else:
        assert result['status']=='stopped' and '未经支持' in result['answer']
    history=app.session(target['chat_session_id'])['messages']
    assistant=[m['content'] for m in history if m['role']=='assistant']
    assert all('WRONG_TARGET' not in text for text in assistant)
