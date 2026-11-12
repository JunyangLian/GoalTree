"""Extension features use real runtime paths; hosted network replies are explicit fixtures."""
import hashlib
import io
import json
import threading
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest

from pico.extension_hub import ExtensionSettings, bundled_catalog, bundled_skill_text
from pico.extensions import PolicyHooks, skill_catalog
from pico.providers.clients import FakeModelClient
from pico.runtime import Pico, SessionStore
from pico.web import Workbench, RequestError, make_server
from pico.workspace import WorkspaceContext
from pico import mcp_connectors as mcp


def app(root, **kwargs):
    return Workbench(root, harness_profile='workbench', load_env=False,
                     model_factory=lambda: FakeModelClient([]), **kwargs)


def agent(root):
    return Pico(FakeModelClient([]), WorkspaceContext.build(root), SessionStore(root/'.pico/sessions'),
                harness_profile='workbench', approval_policy='auto', max_depth=0)


def test_catalog_and_settings_are_workspace_scoped_integrity_checked_and_on_demand(tmp_path):
    other = tmp_path/'other'; other.mkdir()
    settings = ExtensionSettings(tmp_path)
    original = PolicyHooks(tmp_path).fingerprint()
    assert len(bundled_catalog()) == 13
    a = agent(tmp_path)
    for row in bundled_catalog():
        if not settings.skill_enabled(row['id']):
            assert bundled_skill_text(tmp_path,row['id'],preview=True)
            continue
        result = a.execute_tool('load_skill', {'name': 'builtin:'+row['id']})
        assert result.metadata['tool_status'] == 'ok'
        assert row['source']['url'] in result.content
    assert len([s for s in skill_catalog(tmp_path) if s.get('origin') == 'bundled']) == 7
    settings.change('plugins', 'development', False, 0)
    assert PolicyHooks(tmp_path).fingerprint() != original
    assert len(skill_catalog(tmp_path)) == 4 and len(skill_catalog(other)) == 7
    with pytest.raises(ValueError, match='disabled'):
        bundled_skill_text(tmp_path, 'systematic-debugging')
    assert '根因' in bundled_skill_text(tmp_path, 'systematic-debugging', preview=True)
    with pytest.raises(ValueError, match='conflict'):
        settings.change('plugins', 'development', True, 0)
    assert ExtensionSettings(tmp_path).read()['revision'] == 1


@pytest.mark.parametrize('group,key,value', [('mcp','playwright',True), ('mcp','shell',True),
    ('url','https://127.0.0.1',True), ('skills','goal-planning','true'), ('plugins','other',True)])
def test_toggles_cannot_install_or_introduce_endpoints(tmp_path, group, key, value):
    with pytest.raises(ValueError):
        ExtensionSettings(tmp_path).change(group, key, value, 0)
    assert not (tmp_path/'.pico/extensions.json').exists()


def test_manifest_tampering_is_rejected_and_licenses_shipped(tmp_path, monkeypatch):
    from pico import extension_hub as hub
    for row in bundled_catalog():
        directory = hub.SKILL_ROOT/row['id']
        assert hashlib.sha256((directory/'SKILL.md').read_bytes()).hexdigest() == row['sha256']
        assert 'MIT' in (directory/'LICENSE').read_text('utf-8')
        if row['source']['author'] == 'obra/superpowers':
            assert row['source']['commit'] and row['source']['upstream_sha256']
            assert 'Jesse Vincent' in (directory/'LICENSE').read_text('utf-8')
    fake = tmp_path/'bundle'; (fake/'goal-planning').mkdir(parents=True)
    (fake/'goal-planning/SKILL.md').write_text('tampered')
    monkeypatch.setattr(hub, 'SKILL_ROOT', fake)
    with pytest.raises(ValueError, match='integrity'):
        bundled_skill_text(tmp_path, 'goal-planning')


def test_view_does_not_probe_and_templates_do_not_claim_connection(tmp_path, monkeypatch):
    web = app(tmp_path)
    monkeypatch.setattr(mcp.HostedMCP, 'probe', lambda self: pytest.fail('GET must not connect'))
    view = web.extension_hub.view()
    assert len(view['plugins']) == 12 and len(view['skills']) == 13
    assert sum(p['enabled'] for p in view['plugins'])==3
    assert sum(s['enabled'] for s in view['skills'])==7
    servers = {s['id']:s for s in view['mcp']}
    assert servers['playwright']['status'] == 'template' and not servers['playwright']['supported']
    assert servers['context7']['status'] == 'network_disabled'
    assert not (tmp_path/'.pico/extension-checks.json').exists()
    assert '@latest' in web.extension_hub.preview('mcp', 'playwright')['content']
    with pytest.raises(ValueError): web.extension_hub.probe('playwright')


def test_learned_and_repository_skills_remain_scoped_and_read_only(tmp_path):
    from pico.evolution import EvolutionStore
    web = app(tmp_path)
    first = web.goals.action({'action':'create_goal','title':'First'})
    second = web.goals.action({'action':'create_goal','title':'Second'})
    scope = web.learning_scope({'goal_id':first['id']})
    store = EvolutionStore(tmp_path)
    source = store.observe('feedback',category='feedback',task_id='source_task',observation='User: verify report',**scope)
    row = store.propose('skill','report',{'description':'report','when':'report needed','steps':['Read scope','Verify result'],
        'acceptance':'verified report'},[source['id']],**scope)
    skill_dir = tmp_path/'.codetrail/skills/private';skill_dir.mkdir(parents=True)
    (skill_dir/'SKILL.md').write_text('---\ndescription: private workflow\n---\nDo not disclose')
    (tmp_path/'.pico/policy.json').write_text(json.dumps({'protected_paths':['.codetrail/skills/private']}))
    ids = [s['id'] for s in web.extension_hub.view(scope)['skills']]
    assert row['id'] in ids and 'private' not in ids
    other_scope = web.learning_scope({'goal_id':second['id']})
    assert row['id'] not in [s['id'] for s in web.extension_hub.view(other_scope)['skills']]
    with pytest.raises(ValueError):web.extension_hub.preview('skill',row['id'],other_scope)
    with pytest.raises(ValueError):web.extension_hub.settings.change('skills',row['id'],True,0)


def test_mutations_validate_scope_before_effect_and_block_active_runs(tmp_path):
    web = app(tmp_path)
    goal = web.goals.action({'action':'create_goal', 'title':'One'})
    data = {'group':'skills','id':'goal-planning','enabled':False,'revision':0}
    with pytest.raises(RequestError):
        web.extensions_action({**data,'goal_id':goal['id'],'session_id':'wrong_session'})
    assert not web.extension_hub.settings.path.exists()
    web.active_id = 'running'
    with pytest.raises(RequestError) as error: web.extensions_action(data)
    assert error.value.status == 409
    web.active_id = None
    assert web.extensions_action(data)['revision'] == 1


def test_local_docs_probe_is_real_child_process_and_disable_is_enforced(tmp_path):
    (tmp_path/'README.md').write_text('README real local documentation.')
    web = app(tmp_path)
    result = web.extension_hub.probe('local-docs')
    assert result['status'] == 'ok' and result['tools'] == ['search_docs']
    assert web.extension_hub.view()['mcp'][1]['last_check'] == result
    web.extension_hub.settings.change('mcp','local-docs',False,0)
    assert agent(tmp_path).execute_tool('mcp_docs',{'query':'README'}).metadata['tool_status'] == 'error'


def test_skill_and_hosted_tool_gates_preserve_policy(tmp_path, monkeypatch):
    settings = ExtensionSettings(tmp_path)
    settings.change('mcp','context7',True,0)
    a = agent(tmp_path)
    args = {'server':'context7','tool':'resolve-library-id','arguments':'{"libraryName":"react","query":"hooks"}'}
    assert a.execute_tool('mcp_call', args).metadata['tool_status'] == 'error'
    a.mcp_network_allowed = True
    monkeypatch.setattr(mcp.HostedMCP,'call',lambda self,tool,args:{'text':'external untrusted docs'})
    assert a.execute_tool('mcp_call', args).metadata['tool_status'] == 'ok'
    assert a.capabilities.extension_context()['mcp']['context7']['query-docs']['arguments']['libraryId'] == 'str'
    (tmp_path/'.pico/policy.json').write_text(json.dumps({'deny_tools':['mcp_call']}))
    assert a.execute_tool('mcp_call', args).metadata['tool_error_code'] == 'policy_hook_denied'


def test_http_serves_hub_and_requires_csrf_on_workspace_settings(tmp_path):
    web = app(tmp_path); server = make_server(web, 0)
    thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    base = 'http://127.0.0.1:'+str(server.server_port)
    try:
        for path in ('/hub.js','/hub.css','/brand.css','/api/extensions','/icons/goaltree.svg','/icons/github.svg','/icons/glm.png','/icons/context7.ico'):
            with urllib.request.urlopen(base+path) as response: assert response.status == 200
        for path,kind,signature in (('/favicon.ico','image/x-icon',b'\x00\x00\x01\x00'),
                                    ('/icons/goaltree.ico?v=tree-20261111','image/x-icon',b'\x00\x00\x01\x00'),
                                    ('/icons/goaltree.png?v=tree-20261111','image/png',b'\x89PNG\r\n\x1a\n')):
            with urllib.request.urlopen(base+path) as response:
                assert response.headers['Content-Type']==kind
                assert response.read().startswith(signature)
                assert response.headers['Cache-Control']=='no-store'
        with urllib.request.urlopen(base+'/') as response:
            html=response.read().decode('utf-8')
            assert 'rel="shortcut icon" href="/icons/goaltree.ico?v=tree-20261111"' in html
            assert 'rel="icon" type="image/png"' in html
        for path in ('/icons/NOTICE.md','/icons/../index.html','/icons/%2e%2e/index.html','/icons/missing.svg'):
            with pytest.raises(urllib.error.HTTPError) as error:urllib.request.urlopen(base+path)
            assert error.value.code==404
        data = json.dumps({'group':'plugins','id':'research','enabled':False,'revision':0}).encode()
        req = urllib.request.Request(base+'/api/extensions',data=data,headers={'Content-Type':'application/json'})
        with pytest.raises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
        assert error.value.code == 403
        req.add_header('X-Pico-Token',web.csrf)
        with urllib.request.urlopen(req) as response: assert json.load(response)['revision'] == 1
        req.add_header('Origin','https://evil.example')
        with pytest.raises(urllib.error.HTTPError) as error: urllib.request.urlopen(req)
        assert error.value.code == 403
    finally:
        server.shutdown(); server.server_close(); thread.join(5)


def wire(monkeypatch, mode='json', change=None):
    calls=[]
    class Response(io.BytesIO):
        def __init__(self,payload):
            method=payload['method']; headers={'Content-Type':'application/json'}; status=200
            if method=='initialize':
                result={'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'instructions':'secret instructions ignored'}
                headers['Mcp-Session-Id']='session-secret'
            elif method=='notifications/initialized':result={};status=202
            elif method=='tools/list':result={'tools':[{'name':n,'inputSchema':{'type':'object'}} for n in ('resolve-library-id','query-docs','run_shell')]}
            else:result={'content':[{'type':'text','text':'verified fixture text'}]}
            message={'jsonrpc':'2.0','id':payload.get('id'),'result':result}
            if change:status,message=change(method,status,message)
            body=json.dumps(message).encode()
            if mode=='sse':body=b'data: '+body+b'\n\n';headers['Content-Type']='text/event-stream'
            super().__init__(body);self.status=status;self.headers=headers
        def getheader(self,key,default=None):return self.headers.get(key,default)
        def read1(self,n):return self.read(n)
    class Connection:
        def __init__(self,host,address,timeout):
            assert host=='mcp.context7.com' and address=='93.184.216.34'
            self.sock=SimpleNamespace(settimeout=lambda _:None)
        def request(self,method,path,body,headers):
            assert method=='POST' and path=='/mcp'
            self.payload=json.loads(body);calls.append((self.payload,headers))
        def getresponse(self):return Response(self.payload)
        def close(self):pass
    monkeypatch.setattr(mcp,'public_addresses',lambda *_:['93.184.216.34'])
    return calls,Connection


@pytest.mark.parametrize('mode',['json','sse'])
def test_hosted_mcp_negotiates_calls_only_reads_and_never_exports_credentials(monkeypatch,mode):
    monkeypatch.setenv('CONTEXT7_API_KEY','credential-secret')
    calls,conn=wire(monkeypatch,mode)
    client=mcp.HostedMCP('context7',connection_factory=conn)
    result=client.call('resolve-library-id',{'libraryName':'react','query':'hooks'})
    assert [p['method'] for p,_ in calls]==['initialize','notifications/initialized','tools/list','tools/call']
    assert calls[-1][1]['Mcp-Session-Id']=='session-secret'
    assert calls[-1][1]['MCP-Protocol-Version']=='2025-06-18'
    assert calls[-1][1]['CONTEXT7_API_KEY']=='credential-secret'
    assert result['text']=='verified fixture text' and result['requests'][-1]['response_sha256']
    assert all(secret not in json.dumps(result) for secret in ('credential-secret','session-secret','secret instructions'))
    again=client.call('query-docs',{'libraryId':'/react/docs','query':'hooks'})
    assert len(calls)==5 and len(again['requests'])==1
    before=len(calls)
    with pytest.raises(ValueError,match='allowlist'): client.call('run_shell',{})
    assert len(calls)==before


@pytest.mark.parametrize('case',['version','wrong_id','server_request','tool_error','rate_limit','redirect','unknown_tools'])
def test_hosted_mcp_fails_closed_without_retries_or_fallback(monkeypatch,case):
    def change(method,status,message):
        if case=='version' and method=='initialize':message['result']['protocolVersion']='future'
        if case=='unknown_tools' and method=='tools/list':message['result']['tools']=[]
        if method=='tools/call':
            if case=='wrong_id':message['id']=True
            if case=='server_request':message={'jsonrpc':'2.0','id':88,'method':'sampling/createMessage'}
            if case=='tool_error':message['result']['isError']=True
            if case=='rate_limit':status=429
            if case=='redirect':status=302
        return status,message
    calls,conn=wire(monkeypatch,change=change)
    with pytest.raises(ValueError):mcp.HostedMCP('context7',connection_factory=conn).call('query-docs',{'libraryId':'/react/docs','query':'hooks'})
    assert len(calls)<=4


@pytest.mark.parametrize('server,tool,args',[('other','run_shell',{}),('context7','query-docs',{'libraryId':'react','query':'docs'}),
    ('github','search_repositories',{'query':'python','page':True}),('github','delete_file',{'path':'a'}),
    ('context7','resolve-library-id',{'libraryName':'react','query':'docs','url':'http://127.0.0.1'})])
def test_hosted_arguments_reject_unsupported_requests_before_network(server,tool,args):
    with pytest.raises(ValueError):mcp.validate_arguments(server,tool,args)


def test_github_requires_local_credential_not_git_login(monkeypatch):
    monkeypatch.delenv('GITHUB_PERSONAL_ACCESS_TOKEN',raising=False)
    with pytest.raises(ValueError,match='locally'):mcp.HostedMCP('github')


def test_optional_bundles_and_connection_toggles_are_atomic_and_persisted(tmp_path):
    settings=ExtensionSettings(tmp_path)
    assert not settings.skill_enabled('test-driven-development')
    settings.change('plugins','testing',True,0)
    assert ExtensionSettings(tmp_path).skill_enabled('test-driven-development')
    settings.change('plugins','connect-microsoft-learn',True,1)
    state=ExtensionSettings(tmp_path).read()
    assert state['mcp']['microsoft-learn'] and state['plugins']['connect-microsoft-learn']
    settings.change('mcp','microsoft-learn',False,2)
    assert not settings.read()['plugins']['connect-microsoft-learn']
    with pytest.raises(ValueError,match='template'):
        settings.change('plugins','connect-playwright',True,3)
    assert settings.read()['revision']==3


def test_legacy_settings_preserve_enabled_connections_when_directory_expands(tmp_path):
    settings=ExtensionSettings(tmp_path);settings.path.parent.mkdir(parents=True)
    settings.path.write_text(json.dumps({'revision':4,'plugins':{'goals':False},'skills':{},'mcp':{'context7':True}}))
    state=settings.read()
    assert not state['plugins']['goals'] and state['plugins']['connect-context7']
    assert not state['plugins']['testing'] and not state['mcp']['exa']


@pytest.mark.parametrize('server,tool,args',[
    ('deepwiki','ask_question',{'repoName':'owner/repo','question':'overview'}),
    ('deepwiki','read_wiki_structure',{'repoName':'https://example.org/repo'}),
    ('microsoft-learn','microsoft_docs_fetch',{'url':'https://example.org/docs'}),
    ('microsoft-learn','microsoft_docs_fetch',{'url':'http://learn.microsoft.com/docs'})])
def test_new_hosted_tools_reject_non_public_repo_names_and_foreign_fetch(server,tool,args):
    with pytest.raises(ValueError):mcp.validate_arguments(server,tool,args)


def test_new_hosted_tools_match_discovered_protocol_names():
    assert mcp.validate_arguments('deepwiki','ask_wiki_question',{'repoName':'modelcontextprotocol/python-sdk','question':'architecture'})
    assert mcp.validate_arguments('microsoft-learn','microsoft_docs_fetch',{'url':'https://learn.microsoft.com/en-us/training/support/mcp'})


def test_brand_assets_are_local_packaged_and_bounded(tmp_path):
    from pico.web import ASSETS
    view=app(tmp_path).extension_hub.view()
    for row in view['plugins']+view['skills']+view['mcp']:
        assert row['icon_url'].startswith('/icons/')
        asset=ASSETS/row['icon_url'].lstrip('/')
        assert asset.is_file() and 0<asset.stat().st_size<=262144
    own=(ASSETS/'icons/goaltree.svg').read_text('utf-8')
    assert '<script' not in own and '<svg' in own
    assert 'icons/goaltree.svg' in (ASSETS/'index.html').read_text('utf-8')
