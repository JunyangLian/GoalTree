"""Deterministic provider protocol, fallback and permission checks; no live API."""
import io
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from pico import search_hybrid as hybrid
from pico import web_search
from pico.web_tools import WebAccessError


def search(client, **kwargs):
    return client.search('asyncio docs', 3, time.monotonic()+8, lambda: None, lambda: None, **kwargs)


def wire(monkeypatch, payload, status=200, kind='application/json'):
    calls=[]
    class Response(io.BytesIO):
        def __init__(self):
            super().__init__(json.dumps(payload).encode()); self.status=status
        def getheader(self, key, default=None):
            return {'Content-Type':kind}.get(key,default)
    class Connection:
        sock=None
        def __init__(self,*args): pass
        def request(self,method,path,**kwargs): calls.append((method,path,kwargs))
        def getresponse(self): return Response()
        def close(self): pass
    monkeypatch.setenv('BIGMODEL_API_KEY','fixture-secret')
    monkeypatch.setattr(hybrid,'PinnedHTTPSConnection',Connection)
    monkeypatch.setattr(hybrid,'public_addresses',lambda *args:['93.184.216.34'])
    return calls


def test_glm_fixed_endpoint_protocol_and_redacted_audit(monkeypatch):
    calls=wire(monkeypatch,{'search_result':[{'title':'Asyncio','link':'https://docs.python.org/3/library/asyncio.html','content':'Official docs'}]})
    result=search(hybrid.HybridSearch(),source='glm')
    assert len(calls)==1 and calls[0][:2]==('POST','/api/paas/v4/web_search')
    request=calls[0][2]
    assert request['headers']['Authorization']=='Bearer fixture-secret'
    assert json.loads(request['body'])=={'search_engine':'search_pro','search_query':'asyncio docs','count':3,'search_recency_filter':'noLimit','content_size':'medium'}
    assert result['results'][0]['sources']==['glm']
    assert result['attempts'][0]['requests'][0]['sha256']
    assert 'fixture-secret' not in json.dumps(result)


@pytest.mark.parametrize('payload,status,kind',[
    ({'search_result':[]},302,'application/json'),
    ({'error':{'message':'fixture-secret'}},402,'application/json'),
    ({'unexpected':[]},200,'application/json'),
    ({'search_result':[]},200,'text/html'),
    ({'search_result':[{'title':'Bad','link':'http://localhost/'}]},200,'application/json')])
def test_glm_errors_are_not_empty_and_never_retry_or_substitute(monkeypatch,payload,status,kind):
    calls=wire(monkeypatch,payload,status,kind)
    with pytest.raises(web_search.SearchUnavailable) as error:search(hybrid.HybridSearch(),source='glm')
    assert len(calls)==1
    assert error.value.search_report['status']=='error'
    assert 'fixture-secret' not in json.dumps(error.value.search_report)


def test_glm_true_empty_and_missing_key_are_distinct(monkeypatch):
    calls=wire(monkeypatch,{'search_result':[]})
    assert search(hybrid.HybridSearch(),source='glm')['status']=='empty'
    monkeypatch.delenv('BIGMODEL_API_KEY')
    with pytest.raises(web_search.SearchUnavailable) as error:search(hybrid.HybridSearch(),source='glm')
    assert len(calls)==1 and 'requires' in error.value.search_report['attempts'][0]['error']


def test_auto_sources_run_in_parallel_and_fuse_provenance(monkeypatch):
    barrier=threading.Barrier(2);client=hybrid.HybridSearch()
    hit={'title':'Asyncio docs','url':'https://docs.python.org/3/library/asyncio.html','snippet':'official'}
    def glm(*args):barrier.wait(2);return [hit]
    def ddg(*args):barrier.wait(2);return [hit],[]
    monkeypatch.setattr(client,'glm',glm)
    monkeypatch.setitem(hybrid.ADAPTERS,'duckduckgo',ddg)
    result=search(client)
    assert len(result['results'])==1 and result['results'][0]['sources']==['glm','duckduckgo']


def test_verification_failure_is_kept_when_bing_fallback_succeeds(monkeypatch):
    client=hybrid.HybridSearch();seen=[]
    monkeypatch.setattr(client,'glm',lambda *a:[])
    def ddg(*a):seen.append('duckduckgo');raise web_search.SourceUnavailable('verification','verification_required')
    def bing(*a):seen.append('bing-intl');return [{'title':'Docs','url':'https://example.org/docs','snippet':''}],[]
    monkeypatch.setitem(hybrid.ADAPTERS,'duckduckgo',ddg)
    monkeypatch.setitem(hybrid.ADAPTERS,'bing-intl',bing)
    result=search(client)
    assert seen==['duckduckgo','bing-intl'] and result['status']=='partial'
    assert result['attempts'][1]['failure_type']=='verification_required'
    assert result['results'][0]['sources']==['bing-intl']
    seen.clear()
    with pytest.raises(web_search.SearchUnavailable):search(client,source='duckduckgo')
    assert seen==['duckduckgo']  # Explicit source never silently switches.


def test_disabled_sources_do_not_issue_requests(monkeypatch):
    client=hybrid.HybridSearch()
    monkeypatch.setattr(client,'glm',lambda *a:pytest.fail('disabled GLM'))
    monkeypatch.setattr(client,'free_get',lambda *a:pytest.fail('disabled foreign search'))
    with pytest.raises(web_search.SearchUnavailable) as error:
        search(client,enabled={'glm':False,'duckduckgo':False})
    assert {a['failure_type'] for a in error.value.search_report['attempts']}=={'disabled'}


@pytest.mark.parametrize('url',['https://example.org/search','https://www.bing.com/other','https://lite.duckduckgo.com/private','http://127.0.0.1/search'])
def test_system_proxy_path_is_restricted_to_fixed_public_search_endpoints(monkeypatch,url):
    monkeypatch.setattr(hybrid.urllib.request,'build_opener',lambda *a:pytest.fail('must reject before networking'))
    with pytest.raises(WebAccessError):
        hybrid.HybridSearch().free_get(url,{'text/html'},time.monotonic()+5,lambda:None,lambda:None)


def test_duck_lite_unwraps_results_and_keeps_snippets():
    seen=[]
    text='<a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2Flibrary%2Fasyncio.html">Asyncio <b>docs</b></a><td class="result-snippet">Official Python documentation</td>'
    def get(url,accepted):seen.append(url);return {'url':url},text
    hits,_=web_search.duckduckgo(get,'asyncio docs',3)
    assert parse_qs(urlsplit(seen[0]).query)=={'q':['asyncio docs'],'kl':['us-en']}
    assert hits[0]['url']=='https://docs.python.org/3/library/asyncio.html'
    assert hits[0]['title']=='Asyncio docs' and 'Official Python' in hits[0]['snippet']


@pytest.mark.parametrize('text,expected',[
    ('<form id="challenge-form">verify human</form>','verification_required'),
    ('<html>Layout changed</html>','unrecognized_response')])
def test_duck_lite_never_labels_challenge_or_unknown_layout_as_no_matches(text,expected):
    with pytest.raises(web_search.SourceUnavailable) as error:
        web_search.duckduckgo(lambda url,accepted:({'url':url},text),'docs',3)
    assert error.value.failure_type==expected


def test_explicit_glm_launcher_cannot_be_overwritten_by_ancestor_dotenv(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    spec=importlib.util.spec_from_file_location('search_launcher',Path(__file__).parents[1]/'scripts/run_goaltree_glm.py')
    launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    parent=tmp_path/'parent';parent.mkdir();workspace=parent/'workspace';workspace.mkdir()
    (parent/'.env').write_text('BIGMODEL_API_KEY=\n')
    selected=tmp_path/'selected.env';selected.write_text('BIGMODEL_API_KEY=fixture-selected-key\n')
    monkeypatch.delenv('BIGMODEL_API_KEY',raising=False)
    monkeypatch.setattr(launcher,'model_factory',lambda key,*args:lambda: __import__('pico.providers.clients',fromlist=['FakeModelClient']).FakeModelClient([]))
    from pico import context_tokens
    monkeypatch.setattr(context_tokens,'GLMTokenCounter',lambda:type('Counter',(),{'identity':{},'counting':'offline-token-counter'})())
    captured=[]
    def serve(app,port,workspace_manager):
        captured.append(app)
        return type('Server',(),{'server_port':port,'serve_forever':lambda self:None,'server_close':lambda self:None})()
    monkeypatch.setattr(launcher,'make_server',serve)
    assert launcher.main(['--workspace',str(workspace),'--env-file',str(selected),'--port','18774'])==0
    assert captured[0].search_mcp.config()['ready']
    assert hybrid.os.getenv('BIGMODEL_API_KEY')=='fixture-selected-key'
