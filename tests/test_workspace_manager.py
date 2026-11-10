"""Real HTTP folder routing, isolation and ordinary planning payload contracts."""
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch
import urllib.request
import urllib.error

import pytest

from pico.web import Workbench,make_server,RequestError
from pico.workspace_manager import WorkspaceManager,workspace_id
from pico.providers.clients import FakeModelClient,OpenAICompatibleModelClient


def app(root):
    return Workbench(root,harness_profile='workbench',model_factory=lambda:FakeModelClient(['<final>Please specify the meeting topic and audience.</final>']),load_env=False)


@contextmanager
def server(initial,manager):
    http=make_server(initial,0,workspace_manager=manager)
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def request(path,root=None,data=None,token=None):
        headers={}
        if root:headers['X-GoalTree-Workspace']=root
        if token:headers['X-Pico-Token']=token
        return json.load(opener.open(urllib.request.Request(f'http://127.0.0.1:{http.server_port}'+path,
            data=json.dumps(data).encode() if data is not None else None,headers=headers)))
    try:yield request
    finally:http.shutdown();http.server_close();thread.join(3)


def test_http_workspace_selection_is_pinned_and_data_stays_in_its_folder(tmp_path):
    a,b=tmp_path/'biology',tmp_path/'meeting';a.mkdir();b.mkdir()
    initial=app(a);manager=WorkspaceManager(initial,factory=app,folder_picker=lambda _:str(b))
    a_id=workspace_id(a)
    with server(initial,manager) as request:
        ac=request('/api/config');assert ac['workspace_id']==a_id
        chosen=request('/api/workspaces/pick',a_id,{},ac['csrf'])
        b_id=chosen['workspace_id'];bc=request('/api/config',b_id)
        assert Path(bc['workspace'])==b
        assert ac['csrf']!=bc['csrf']
        request('/api/tree',a_id,{'action':'create_goal','title':'Fish genomics'},ac['csrf'])
        assert not request('/api/tree',b_id)['goals']
        request('/api/tree',b_id,{'action':'create_goal','title':'Meeting report'},bc['csrf'])
        assert [g['title'] for g in request('/api/tree',a_id)['goals'].values()]==['Fish genomics']
        assert [g['title'] for g in request('/api/tree',b_id)['goals'].values()]==['Meeting report']
        for id,config in [(a_id,ac),(b_id,bc)]:
            request('/api/tree',id,{'action':'new_conversation'},config['csrf'])
        ca=request('/api/tree/context?mode=assistant',a_id)
        cb=request('/api/tree/context?mode=assistant',b_id)
        request('/api/cooperation',a_id,{'goal_id':'','session_id':ca['session_id'],'cooperation':{'planning':'step','implementation':'autonomous'}},ac['csrf'])
        assert request('/api/tree/context?mode=assistant',b_id)['cooperation']=={'planning':'balanced','implementation':'balanced'}
        attachment=request('/api/attachments',a_id,{'goal_id':'','session_id':ca['session_id'],'name':'fish.md','data':base64.b64encode(b'BIOLOGY_ONLY').decode()},ac['csrf'])
        with pytest.raises(urllib.error.HTTPError):
            request('/api/tasks',b_id,{'mode':'assistant','session_id':cb['session_id'],'prompt':'Read','attachments':[attachment['id']]},bc['csrf'])
        with pytest.raises(urllib.error.HTTPError) as error:
            request('/api/tree',b_id,{'action':'new_conversation'},ac['csrf'])
        assert error.value.code==403
        # Reading B did not change the header-less default nor another tab's A.
        assert request('/api/config')['workspace_id']==a_id
    assert (a/'.pico/goaltree.sqlite3').is_file() and (b/'.pico/goaltree.sqlite3').is_file()


def test_registry_reopens_canonical_folders_without_loading_their_env(tmp_path,monkeypatch):
    a,b=tmp_path/'a',tmp_path/'b';a.mkdir();b.mkdir()
    monkeypatch.setenv('PICO_OPENAI_MODEL','host-selected-model')
    (b/'.env').write_text('PICO_OPENAI_MODEL=untrusted-workspace-model\n',encoding='utf-8')
    initial=app(a);manager=WorkspaceManager(initial)
    b_id=manager.open(str(b))
    assert manager.open(str(b/'..'/'b'))==b_id
    assert os.environ['PICO_OPENAI_MODEL']=='host-selected-model'
    restored=WorkspaceManager(initial)
    assert restored.get(b_id).root==b.resolve()
    assert len(restored.folders())==2
    with pytest.raises(ValueError):restored.get('ws_unknown')
    with pytest.raises(ValueError):manager.open('relative-folder')
    with pytest.raises(ValueError):manager.open(str(b/'.env'))


def test_global_execution_serialization_does_not_retarget_existing_runs(tmp_path):
    a,b=tmp_path/'a',tmp_path/'b';a.mkdir();b.mkdir()
    first=app(a);manager=WorkspaceManager(first,factory=app)
    second=manager.get(manager.open(str(b)))
    first.active_id='running-a'
    with pytest.raises(RequestError,match='已有工作区'):manager.submit(second,{'prompt':'Other task'})
    assert first.active_id=='running-a' and not second.active_id


def test_cancelled_folder_picker_changes_nothing(tmp_path):
    initial=app(tmp_path);manager=WorkspaceManager(initial,factory=app,folder_picker=lambda _:None)
    before=manager.folders()
    assert manager.pick(initial) is None and manager.folders()==before


def test_registered_folder_can_reopen_after_restart_at_registry_capacity(tmp_path):
    from types import SimpleNamespace
    initial=SimpleNamespace(root=tmp_path,active_id=None)
    factory=lambda root:SimpleNamespace(root=root,active_id=None)
    manager=WorkspaceManager(initial,factory=factory)
    for index in range(19):
        folder=tmp_path/str(index);folder.mkdir();manager.open(str(folder))
    restored=WorkspaceManager(initial,factory=factory)
    assert restored.open(str(tmp_path/'0'))==workspace_id(tmp_path/'0')
    extra=tmp_path/'extra';extra.mkdir()
    with pytest.raises(ValueError,match='最多保存20个'):restored.open(str(extra))


def test_new_goal_planning_uses_low_effort_without_injecting_workspace_docs(tmp_path):
    (tmp_path/'README.md').write_text('BIOLOGY_PROJECT_MARKER: fish genomics only.',encoding='utf-8')
    (tmp_path/'docs').mkdir();(tmp_path/'docs'/'fish.md').write_text('BIOLOGY_SOURCE_MARKER')
    def model():return OpenAICompatibleModelClient('glm-5.3-flash','https://open.bigmodel.cn/api/paas/v4','fixture',1,30,'chat_completions')
    initial=Workbench(tmp_path,harness_profile='workbench',model_factory=model,load_env=False)
    payloads=[]
    class Response:
        headers={'Content-Type':'application/json'}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self):return json.dumps({'model':'glm-5.3-flash','choices':[{'message':{'content':'What is the report topic and audience?','reasoning_content':''},'finish_reason':'stop'}],'usage':{'prompt_tokens':10,'completion_tokens':5}}).encode()
    def respond(request,**kwargs):payloads.append(json.loads(request.data));return Response()
    with patch('urllib.request.urlopen',side_effect=respond),patch('pico.workspace.subprocess.run') as git:
        task=initial.task(initial.submit({'mode':'assistant','prompt':'准备开会，我准备做一个报告文档，帮我规划一下'})['id'])
        with task.condition:assert task.condition.wait_for(lambda:task.finished is not None,30)
        git.assert_not_called()
    assert task.status=='completed',task.error
    text=json.dumps(payloads,ensure_ascii=False)
    assert 'BIOLOGY_PROJECT_MARKER' not in text and 'BIOLOGY_SOURCE_MARKER' not in text
    assert 'Use code_search or symbol_info to navigate.' not in text
    assert payloads[0]['reasoning_effort']=='low' and payloads[0]['thinking']['type']=='enabled'
    assert not task.agent.workspace.project_docs and task.agent.workspace.planning_only
    assert any(e.get('request_configuration',{}).get('reasoning_effort')=='low' for e in task.events)
    # Complex implementation still starts with the provider's max effort.
    assert initial._build_agent(None,'ask',3).model_client.native_wire_options['reasoning_effort']=='max'
