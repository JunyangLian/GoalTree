"""Explicit cross-conversation consent, bounded retrieval and privacy controls."""
import json
import threading
import urllib.request
import urllib.error

import pytest

from pico.web import Workbench, RequestError, make_server
from pico.providers.clients import FakeModelClient
from pico.shared_memory import SharedMemoryRuntime, SharedMemoryStore
from pico.extensions import PolicyChanged


def setup(root, **kwargs):
    root.mkdir(parents=True,exist_ok=True)
    app=Workbench(root,model_factory=lambda:FakeModelClient([]),load_env=False,harness_profile='workbench',**kwargs)
    a=app.goals.action({'action':'create_goal','title':'样本研究'})
    a=app.goals.action({'action':'update_goal','goal_id':a['id'],'revision':a['revision'],'context_notes':'罗非鱼公开样本登记采用批次编号；报告先列证据再列建议。'})
    b=app.goals.action({'action':'create_goal','title':'报告准备','constraints':'CURRENT_GOAL_ONLY'})
    return app,a,b


def opt(app,g,enabled=True):
    owner=g['chat_session_id'];old=app.shared_memory.preference(owner)
    return app.set_memory_sharing({'goal_id':g['id'],'session_id':owner,'enabled':enabled,'revision':old['revision']})


def runtime(app,g):
    a=app._build_agent(g['chat_session_id'],'ask',5)
    a.goal_binding=app.goals.binding(g['id'],'','assistant');a.current_task_state=None
    return a,SharedMemoryRuntime(a,app.shared_memory,app.goals)


def test_sharing_is_double_opt_in_and_settings_survive_restart(tmp_path):
    app,a,b=setup(tmp_path);agent,r=runtime(app,b)
    assert not app.shared_memory.preference(a['chat_session_id'])['enabled']
    assert r.prepare('罗非鱼 样本登记')==[]
    opt(app,a);_,r=runtime(app,b)
    assert r.prepare('罗非鱼 样本登记')==[]
    opt(app,b);_,r=runtime(app,b);notes=r.prepare('罗非鱼 样本登记')
    assert len(notes)==1 and notes[0]['source_goal_id']==a['id']
    assert notes[0]['conditions'] and notes[0]['trust'].startswith('Opt-in historical')
    assert SharedMemoryStore(tmp_path).preference(a['chat_session_id'])['enabled']
    opt(app,a,False);_,r=runtime(app,b)
    assert r.prepare('罗非鱼 样本登记')==[]
    assert app.goals.read()['goals'][b['id']]['constraints']=='CURRENT_GOAL_ONLY'


def test_consent_revision_scope_and_active_run_are_enforced(tmp_path):
    app,a,b=setup(tmp_path)
    with pytest.raises(RequestError):app.set_memory_sharing({'goal_id':a['id'],'session_id':b['chat_session_id'],'enabled':True,'revision':0})
    opt(app,a)
    with pytest.raises(RequestError) as e:
        app.set_memory_sharing({'goal_id':a['id'],'session_id':a['chat_session_id'],'enabled':False,'revision':0})
    assert e.value.status==409
    for value in (None,1,'true'):
        with pytest.raises(RequestError):app.set_memory_sharing({'goal_id':a['id'],'session_id':a['chat_session_id'],'enabled':value,'revision':1})
    app.active_id='running'
    with pytest.raises(RequestError):opt(app,b)
    assert not app.shared_memory.preference(b['chat_session_id'])['enabled']


def test_workspace_boundaries_and_raw_transcripts_are_not_shared(tmp_path):
    app,a,b=setup(tmp_path);opt(app,a);opt(app,b)
    app._build_agent(a['chat_session_id'],'ask',3).record({'role':'user','content':'RAW_TRANSCRIPT_SENTINEL','created_at':'fixture'})
    _,r=runtime(app,b)
    assert r.prepare('RAW_TRANSCRIPT_SENTINEL')==[]
    other,c,d=setup(tmp_path/'other');opt(other,c);opt(other,d)
    _,r=runtime(other,d)
    notes=r.prepare('罗非鱼 样本登记')
    assert notes and all(n['source_goal_id']==c['id'] for n in notes)
    assert a['id'] not in json.dumps(notes)


@pytest.mark.parametrize('mode',['goal_label','session_label','secret_text','sensitive_text'])
def test_sensitive_sources_never_enter_shared_candidates(tmp_path,mode):
    app,a,b=setup(tmp_path,privacy_protection=True);opt(app,a);opt(app,b)
    if mode.endswith('_label'):
        resource='goal:'+a['id'] if mode=='goal_label' else 'session:'+a['chat_session_id']
        app.privacy_store.label(resource,'core' if mode=='goal_label' else 'sensitive')
    else:
        current=app.goals.read()['goals'][a['id']]
        text='罗非鱼样本 sk-SYNTHETICFAKEKEY123456789' if mode=='secret_text' else '罗非鱼样本 contact@example.com'
        app.goals.action({'action':'update_goal','goal_id':a['id'],'revision':current['revision'],'context_notes':text})
    _,r=runtime(app,b)
    assert r.prepare('罗非鱼 样本')==[]


def test_only_active_fresh_owned_memories_share_with_complete_conditions(tmp_path):
    app,a,b=setup(tmp_path);opt(app,a);opt(app,b);ag,r=runtime(app,b)
    learning=ag.capabilities.learning.store
    source=learning.observe('fixture',category='run',session_id=a['chat_session_id'],goal_id=a['id'],
                            task_id='fixture',run_id='r',outcome='passed',observation='Public host evidence')
    item=learning.propose('memory','fish-rule',{'text':'罗非鱼报告必须标明数据版本','conditions':'只适用于公开资料报告；当前目标规则优先'},
                         [source['id']],scope='goal',goal_id=a['id'],session_id=a['chat_session_id'])
    assert not any(n['source']==item['id'] for n in r.prepare('数据版本 罗非鱼'))
    learning.activate(item['id'],item['version'],reviewer='operator')
    _,r=runtime(app,b);notes=r.prepare('数据版本 罗非鱼')
    shared=next(n for n in notes if n['source']==item['id'])
    assert shared['conditions']=='只适用于公开资料报告；当前目标规则优先'
    learning.transition(item['id'],learning.get(item['id'])['version'],'retracted','fixture revoke')
    with pytest.raises(PolicyChanged):r.prepare('数据版本 罗非鱼')
    _,fresh=runtime(app,b)
    assert not any(n['source']==item['id'] for n in fresh.prepare('数据版本 罗非鱼'))


def test_revocation_and_source_change_stop_active_runtime(tmp_path):
    app,a,b=setup(tmp_path);opt(app,a);opt(app,b);_,r=runtime(app,b)
    assert r.prepare('罗非鱼 样本登记')
    current=app.goals.read()['goals'][a['id']]
    app.goals.action({'action':'update_goal','goal_id':a['id'],'revision':current['revision'],'context_notes':'罗非鱼新登记规则'})
    with pytest.raises(PolicyChanged):r.prepare('罗非鱼 样本登记')
    _,r=runtime(app,b);r.prepare('罗非鱼 新登记')
    opt(app,a,False)
    with pytest.raises(PolicyChanged):r.prepare('罗非鱼 样本登记')


@pytest.mark.parametrize('kind',['learning','verified'])
def test_source_file_labels_and_freshness_filter_before_ranking(tmp_path,kind):
    app,a,b=setup(tmp_path,privacy_protection=True);opt(app,a);opt(app,b);ag,r=runtime(app,b)
    (tmp_path/'fixture.txt').write_text('Public synthetic source',encoding='utf-8')
    if kind=='learning':
        store=ag.capabilities.learning.store
        receipt=store.observe('file-receipt',category='run',session_id=a['chat_session_id'],goal_id=a['id'],task_id='fixture',
                              run_id='r',outcome='passed',observation='Host receipt',evidence_paths=['fixture.txt'])
        item=store.propose('memory','file-derived',{'text':'版本登记规则来自文件依据','conditions':'Only this fixture'},[receipt['id']],
                           scope='goal',goal_id=a['id'],session_id=a['chat_session_id'])
        item=store.activate(item['id'],item['version'],reviewer='operator')
    else:
        store=ag.memory.knowledge
        item=store.propose('版本登记规则来自文件依据','file-derived',['fixture.txt'],scope='session',session_id=a['chat_session_id'])
        item=store.activate(item['id'],item['version'],reviewer='operator')
    assert any(n['source']==item['id'] for n in r.prepare('版本登记规则'))
    app.privacy_store.label('file:fixture.txt','core')
    _,fresh=runtime(app,b)
    assert not any(n['source']==item['id'] for n in fresh.prepare('版本登记规则'))
    # Changing the underlying bytes invalidates source-dependent memory too.
    (tmp_path/'fixture.txt').write_text('Changed source',encoding='utf-8')
    _,fresh=runtime(app,b)
    assert not any(n['source']==item['id'] for n in fresh.prepare('版本登记规则'))
    assert not store.fresh(store.get(item['id'])) if kind=='learning' else not any(r['id']==item['id'] for r in store.active(a['chat_session_id']))


def test_large_entries_are_omitted_whole_and_no_remote_embedding_is_used(tmp_path,monkeypatch):
    app,a,b=setup(tmp_path);opt(app,a);opt(app,b);ag,r=runtime(app,b)
    ag.capabilities.index.embedding=type('Embedding',(),{'identity':'must-not-call'})()
    monkeypatch.setattr(ag.capabilities.index,'_dense',lambda *args:pytest.fail('Shared memory must stay local'))
    assert r.prepare('罗非鱼 样本登记')
    assert ag.capabilities.index.last_audit['dense_allowed'] is False
    current=app.goals.read()['goals'][a['id']]
    app.goals.action({'action':'update_goal','goal_id':a['id'],'revision':current['revision'],'context_notes':'罗非鱼 '*600})
    _,r=runtime(app,b)
    assert r.prepare('罗非鱼')==[]


def test_native_request_contains_shared_advisory_but_not_foreign_contracts(tmp_path):
    from pico.providers.native_tools import NativeToolConversation
    class Native(FakeModelClient):
        supports_native_tools=True
        def __init__(self):super().__init__([]);self.sent=[]
        def begin_native_run(self,tools,context,request,command):
            self.native_conversation=NativeToolConversation(tools,context,request,command,require_reasoning=False)
        def complete(self,*args,**kwargs):
            c=self.native_conversation;self.sent.append(c.prepare_payload({'max_tokens':512,**c.payload()}))
            return c.accept({'choices':[{'message':{'content':'Offline advisory integration check.'}}]}, {})
    app=Workbench(tmp_path,model_factory=Native,load_env=False,harness_profile='workbench',native_input_budget=90000,native_context_window=160000)
    a=app.goals.action({'action':'create_goal','title':'Donor','constraints':'FOREIGN_CONSTRAINT_NOT_TRANSFERRED'})
    a=app.goals.action({'action':'update_goal','goal_id':a['id'],'revision':a['revision'],'context_notes':'罗非鱼公开样本登记使用批次编号。'})
    b=app.goals.action({'action':'create_goal','title':'Receiver','constraints':'CURRENT_ONLY'})
    opt(app,a);opt(app,b)
    task=app.task(app.submit({'goal_id':b['id'],'mode':'assistant','prompt':'参考罗非鱼公开样本登记习惯','max_steps':3})['id'])
    with task.condition:assert task.condition.wait_for(lambda:task.finished is not None and app.active_id is None,timeout=25)
    assert task.status=='completed',task.error
    wire=task.agent.model_client.sent[0]
    memories=next(m['content'] for m in wire['messages'] if (m.get('content') or '').startswith('Current retrieved memory'))
    assert a['id'] in memories and 'conditions' in memories
    assert 'FOREIGN_CONSTRAINT_NOT_TRANSFERRED' not in json.dumps(wire)
    assert 'CURRENT_ONLY' in json.dumps(wire)
    assert any(e['event']=='shared_memory_retrieved' for e in task.events)


def test_http_settings_require_csrf_and_bound_owner(tmp_path):
    app,a,b=setup(tmp_path);server=make_server(app,0);threading.Thread(target=server.serve_forever,daemon=True).start()
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));url=f'http://127.0.0.1:{server.server_port}'
    data={'goal_id':a['id'],'session_id':a['chat_session_id'],'enabled':True,'revision':0}
    def post(token):
        return opener.open(urllib.request.Request(url+'/api/memory-sharing',data=json.dumps(data).encode(),headers={'Content-Type':'application/json','X-Pico-Token':token}),timeout=5)
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:post('wrong')
        assert denied.value.code==403
        with post(app.csrf) as reply:assert json.loads(reply.read())=={'enabled':True,'revision':1}
        view=json.loads(opener.open(url+f'/api/tree/context?mode=assistant&goal_id={a["id"]}').read())
        assert view['memory_sharing']=={'enabled':True,'revision':1}
        data.update(session_id=b['chat_session_id'],revision=1)
        with pytest.raises(urllib.error.HTTPError) as denied:post(app.csrf)
        assert denied.value.code==409
    finally:server.shutdown();server.server_close()
