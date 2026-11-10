"""Public progress is evidence metadata, never private reasoning or fake completion."""
import io
import json
from unittest.mock import patch

from pico.providers.streaming import read_chat_stream
from pico.web_status import public_activity
from pico.web import Workbench
from pico.providers.clients import FakeModelClient, OpenAICompatibleModelClient


def sse(events):
    return io.BytesIO(b''.join(('data: '+json.dumps(e)+'\n\n').encode() for e in events)+b'data: [DONE]\n\n')


def test_stream_exposes_only_phase_metadata_and_visible_answer():
    progress,visible=[],[]
    private='PRIVATE_REASONING_MUST_STAY_PRIVATE'
    result=read_chat_stream(sse([
        {'model':'glm-5.3-flash','choices':[{'delta':{'reasoning_content':private}}]},
        {'choices':[{'delta':{'content':'Public answer'}}]},
        {'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':5,'completion_tokens':6}},
    ]),on_delta=visible.append,on_progress=progress.append)
    assert progress==[{'phase':'analysis'},{'phase':'answer'}]
    assert visible==['Public answer']
    assert private not in json.dumps(progress+visible)
    assert result['choices'][0]['message']['reasoning_content']==private  # Replay preserved internally.
    assert result['usage']['prompt_tokens']==5


def test_progress_heartbeat_is_bounded_and_partial_arguments_are_not_public():
    progress=[]
    with patch('pico.providers.streaming.time.monotonic',side_effect=[0,0.1,2.2,2.3]):
        result=read_chat_stream(sse([
            {'choices':[{'delta':{'reasoning_content':'one'}}]},
            {'choices':[{'delta':{'reasoning_content':'two'}}]},
            {'choices':[{'delta':{'reasoning_content':'three'}}]},
            {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'real-id','function':{'name':'read_file','arguments':'{"path":"a.py"}'}}]},'finish_reason':'tool_calls'}]},
        ]),on_progress=progress.append)
    assert progress==[{'phase':'analysis'},{'phase':'analysis'},{'phase':'tool_arguments'}]
    assert all(set(e)=={'phase'} for e in progress) and 'a.py' not in json.dumps(progress)
    assert result['choices'][0]['message']['tool_calls'][0]['id']=='real-id'


def test_public_history_projection_drops_reasoning_answers_and_tool_contents():
    result=public_activity({'id':'a','status':'completed','elapsed_ms':3000,'events':[
        {'event':'model_progress','phase':'analysis','reasoning_content':'SECRET'},
        {'event':'model_delta','text':'UNFINALIZED'},
        {'event':'model_parsed','kind':'final','completion_metadata':{'reasoning_content':'SECRET'}},
        {'event':'tool_executed','name':'plan_tasks','tool_status':'ok','args':{'steps':'[{"title":"A"}]'},'result':'PRIVATE_TOOL_OUTPUT'},
        {'event':'tool_executed','name':'read_file','args':['bad'],'tool_status':'rejected'},
    ]})
    text=json.dumps(result)
    assert all(value not in text for value in ['SECRET','UNFINALIZED','PRIVATE_TOOL_OUTPUT'])
    assert result['events'][2]['task_count']==1


def test_completed_message_restores_only_its_own_run_activity(tmp_path):
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient(['<final>A response.</final>']))
    scope=app.goals.binding('','','assistant')
    task=app.task(app.submit({'mode':'assistant','prompt':'A question'})['id'])
    with task.condition:
        assert task.condition.wait_for(lambda:task.finished is not None,30)
    message=app.session(scope['session_id'])['messages'][-1]
    assert message['activity']['id']==task.id and message['activity']['status']=='completed'
    assert message['content']=='A response.'
    # Old message migration requires exact prompt+answer, never a different retry.
    path=app.root/'.pico/sessions'/f"{scope['session_id']}.json"
    data=json.loads(path.read_text(encoding='utf-8'));data['history'][-1].pop('run_id');path.write_text(json.dumps(data),encoding='utf-8')
    assert app.session(scope['session_id'])['messages'][-1]['activity']['id']==task.id
    data['history'][-1]['content']='A different response.';path.write_text(json.dumps(data),encoding='utf-8')
    assert 'activity' not in app.session(scope['session_id'])['messages'][-1]


def test_native_glm_stream_keeps_actual_tool_ids_and_thinking_replay(tmp_path):
    from pico.runtime import Pico,SessionStore
    from pico.workspace import WorkspaceContext
    model=OpenAICompatibleModelClient('glm-5.3-flash','https://open.bigmodel.cn/api/paas/v4','fixture-key',1,30,'chat_completions')
    model.stream=True
    frames=[
        [{'model':model.model,'choices':[{'delta':{'reasoning_content':'PRIVATE_THINKING'}}]},
         {'choices':[{'delta':{'tool_calls':[{'index':0,'id':'actual-call','function':{'name':'read_file','arguments':'{"path":"a.txt"}'}}]},'finish_reason':'tool_calls'}]},
         {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':4}}],
        [{'model':model.model,'choices':[{'delta':{'reasoning_content':'PRIVATE_THINKING_2'}}]},
         {'choices':[{'delta':{'content':'Read actual file.'}}]},
         {'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':5}}],
    ]
    class Response(io.BytesIO):
        headers={'Content-Type':'text/event-stream'}
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
    payloads=[]
    def respond(request,**kwargs):
        payloads.append(json.loads(request.data))
        return Response(sse(frames.pop(0)).getvalue())
    (tmp_path/'a.txt').write_text('actual file')
    agent=Pico(model,WorkspaceContext.build(tmp_path,repo_root_override=tmp_path),SessionStore(tmp_path/'.pico/sessions'),harness_profile='workbench',max_steps=4)
    events=[];original=agent.emit_trace
    def emit(state,event,payload=None):
        events.append({'event':event,**(payload or {})});return original(state,event,payload)
    agent.emit_trace=emit
    with patch('urllib.request.urlopen',side_effect=respond):
        assert agent.ask('Read a.txt')=='Read actual file.'
    assert payloads[0]['stream'] and payloads[0]['tool_stream']
    assert any(m.get('tool_call_id')=='actual-call' for m in payloads[1]['messages'])
    assert any(m.get('reasoning_content')=='PRIVATE_THINKING' for m in payloads[1]['messages'])
    assert 'PRIVATE_THINKING' not in json.dumps(events)
    assert any(e['event']=='model_progress' and e['phase']=='analysis' for e in events)
    assert any(e['event']=='tool_started' for e in events)
