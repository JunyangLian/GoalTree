"""Reject truncated streams, retry only model transport, never replay effects."""
import io
import json
from unittest.mock import patch

import pytest

from pico.control import Cancellation,Cancelled
from pico.providers.clients import OpenAICompatibleModelClient
from pico.providers.streaming import IncompleteSSEError,read_chat_stream
from pico.runtime import Pico,SessionStore
from pico.workspace import WorkspaceContext
from pico.web_status import model_failure,usage_summary


def stream(events,done=True):
    return b''.join(('data: '+json.dumps(e)+'\n\n').encode() for e in events)+(b'data: [DONE]\n\n' if done else b'')


def call(id,path):
    return {'index':0,'id':id,'type':'function','function':{'name':'write_file',
        'arguments':json.dumps({'path':path,'content':'actual written text'})}}


def response(events,done=True):
    class Response(io.BytesIO):
        headers={'Content-Type':'text/event-stream','x-request-id':'fixture-request-id'}
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
    return Response(stream(events,done))


def client():
    c=OpenAICompatibleModelClient('glm-5.3-flash','https://open.bigmodel.cn/api/paas/v4',
                                 'fixture-key',1,30,'chat_completions')
    c.stream=True;c.max_transport_attempts=2
    return c


@pytest.mark.parametrize('done,finish',[(False,None),(False,'stop'),(True,None)])
def test_missing_either_terminal_signal_rejects_complete_looking_tools_and_preserves_diagnostics(done,finish):
    audits=[];events=[{'choices':[{'delta':{'reasoning_content':'PRIVATE_REASONING',
        'content':'Unfinished public answer','tool_calls':[call('unaccepted','bad.txt')]},'finish_reason':finish}]}]
    with pytest.raises(IncompleteSSEError) as error:
        read_chat_stream(io.BytesIO(stream(events,done)),on_diagnostics=audits.append)
    audit=error.value.diagnostics
    assert audit==audits[0] and audit['done_seen']==done and audit['finish_reason']==finish
    assert audit['answer_chars']==24 and audit['tool_call_count']==1 and audit['data_events']==1
    assert audit['received_bytes']>0 and len(audit['raw_sha256'])==64
    assert 'PRIVATE_REASONING' not in json.dumps(audits) and 'bad.txt' not in json.dumps(audits)


def test_stream_read_timeout_and_cancel_are_distinct():
    class Broken:
        def readline(self,*args):raise TimeoutError('fixture read timeout')
    with pytest.raises(IncompleteSSEError) as error:read_chat_stream(Broken())
    assert error.value.diagnostics['termination']=='read_timeout'
    cancellation=Cancellation();cancellation.cancel()
    with pytest.raises(Cancelled):read_chat_stream(Broken(),cancellation)


def test_one_recovery_never_executes_partial_tool_or_replays_previous_write(tmp_path):
    c=client()
    frames=[response([{'model':c.model,'choices':[{'delta':{'reasoning_content':'PRIVATE_ONE',
        'tool_calls':[call('accepted-write','good.txt')]},'finish_reason':'tool_calls'}],
        'usage':{'prompt_tokens':10,'completion_tokens':5}}]),
        response([{'model':c.model,'choices':[{'delta':{'reasoning_content':'PRIVATE_PARTIAL',
            'tool_calls':[call('discarded-write','bad.txt')]}}]}],done=False),
        response([{'model':c.model,'choices':[{'delta':{'reasoning_content':'PRIVATE_FINAL','content':'Saved once.'},
            'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':6}}])]
    payloads=[]
    def respond(request,**kwargs):
        payloads.append(request.data);return frames.pop(0)
    a=Pico(c,WorkspaceContext.build(tmp_path,repo_root_override=tmp_path),SessionStore(tmp_path/'.pico/sessions'),
           harness_profile='workbench',max_steps=5,max_depth=0)
    approvals=[];a.approve=lambda name,args:approvals.append(name) or True
    events=[];original=a.emit_trace
    def emit(state,event,payload=None):
        events.append({'event':event,**(payload or {})});return original(state,event,payload)
    a.emit_trace=emit
    with patch('urllib.request.urlopen',side_effect=respond),patch('pico.providers.clients.time.sleep'):
        assert a.ask('Write good.txt')=='Saved once.'
    assert (tmp_path/'good.txt').read_text()=='actual written text' and not (tmp_path/'bad.txt').exists()
    assert approvals==['write_file'] and payloads[1]==payloads[2]
    assert c.native_conversation.pending is None and c.active_response is None
    assert len([e for e in events if e['event']=='tool_executed'])==1
    failed=[e for e in events if e['event']=='model_transport' and not e['ok']]
    assert len(failed)==1 and failed[0]['stream_reset'] and failed[0]['will_retry']
    assert failed[0]['request_id']=='fixture-request-id'
    assert 'PRIVATE_' not in json.dumps(events)
    summary=usage_summary(events)
    assert summary['unknown_transport_attempts']==1 and not summary['complete']
    assert summary['known_input_tokens']==30


def test_repeated_eof_stops_at_budget_with_unknown_usage_and_no_conversation_mutation():
    c=client();c.begin_native_run({},'context','request',None)
    before=json.dumps(c.native_conversation.messages)
    def respond(*args,**kwargs):return response([{'choices':[{'delta':{'content':'partial'}}]}],False)
    with patch('urllib.request.urlopen',side_effect=respond) as send,patch('pico.providers.clients.time.sleep'):
        with pytest.raises(IncompleteSSEError):c.complete('ignored',500)
    assert send.call_count==2 and c.active_response is None
    assert json.dumps(c.native_conversation.messages)==before and c.native_conversation.pending is None
    meta=c.last_completion_metadata
    assert meta['transport_failures']==2 and meta['unknown_usage_attempts']==2
    assert meta.get('input_tokens') is None
    assert meta['transport_events'][-1]['will_retry'] is False
    assert model_failure('Incomplete SSE response','IncompleteSSEError')[0]=='model_stream_interrupted'


def test_cancellation_during_retry_wait_stops_before_second_request():
    c=client();c.cancellation=Cancellation()
    def cancel_wait(seconds):c.cancellation.cancel();c.cancellation.check()
    c.cancellation.wait=cancel_wait
    with patch('urllib.request.urlopen',return_value=response([],False)) as send:
        with pytest.raises(Cancelled):c.complete('request',500)
    assert send.call_count==1 and c.active_response is None
