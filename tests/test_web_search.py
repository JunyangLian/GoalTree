"""Search sources, cross-provider evidence and failure semantics, without live APIs."""
import json
import threading
import time
from unittest.mock import patch

import pytest

from pico.web_tools import PublicWeb, WebAccessError
from pico import web_search as search
from pico.web import Workbench
from pico.providers.clients import FakeModelClient


RSS = '<rss><channel><item><title>A</title><link>https://example.org/a</link></item><item><title>B</title><link>https://example.org/b</link></item></channel></rss>'
HTML = '<div class="result c-container"><h3><a href="https://example.org/b#section">B <b>official</b></a></h3><div class="c-abstract">Actual snippet</div></div><div class="result"><h3><a href="https://example.org/c">C</a></h3></div>'
WECHAT = '<ul class="news-list"><li id="sogou_vr_11002601_box_0"><h3><a id="sogou_vr_11002601_title_0" href="https://example.org/b">B official</a></h3><p class="txt-info">Actual snippet</p><span class="all-time-y2">Public account</span></li></ul>'



def metadata(url):
    return {'url':url,'retrieved_at':'fixture','sha256':'a'*64,'bytes':100,'content_type':'fixture'}


def test_compare_queries_both_in_parallel_with_shared_budget_and_deduplicates(monkeypatch):
    barrier=threading.Barrier(3);deadlines=[];client=PublicWeb()
    def get(url,accepted,**kwargs):
        deadlines.append(kwargs['deadline']);barrier.wait(timeout=3)
        return metadata(url),RSS if 'bing.com' in url else WECHAT if 'sogou.com' in url else HTML
    monkeypatch.setattr(client,'get',get)
    result=json.loads(client.search('official docs',5,source='compare'))
    assert result['status']=='ok' and {a['source'] for a in result['attempts']}=={'bing','baidu','wechat'}
    assert len(set(deadlines))==1
    assert [r['url'] for r in result['results']]==['https://example.org/b','https://example.org/a','https://example.org/c']
    assert result['results'][0]['sources']==['bing','baidu','wechat']


def test_partial_failure_keeps_actual_error_without_relabelling_another_provider(monkeypatch):
    client=PublicWeb()
    def get(url,accepted,**kwargs):
        if 'bing.com' in url:raise WebAccessError('fixture connection timeout')
        return metadata(url),HTML
    monkeypatch.setattr(client,'get',get)
    result=json.loads(client.search('docs',3))
    assert result['status']=='partial'
    assert result['attempts'][0]['status']=='error' and 'timeout' in result['attempts'][0]['error']
    assert all('bing' not in r['sources'] and set(r['sources']) <= {'baidu','wechat'} for r in result['results'])


def test_all_failures_are_errors_with_all_source_records(monkeypatch):
    monkeypatch.setattr(PublicWeb,'get',lambda *a,**k:(_ for _ in ()).throw(WebAccessError('fixture denied')))
    with pytest.raises(search.SearchUnavailable) as error:PublicWeb().search('docs')
    assert error.value.search_report['status']=='error'
    assert [a['source'] for a in error.value.search_report['attempts']]==['bing','baidu','wechat']
    assert not error.value.search_report['results']


def test_explicit_source_never_silently_uses_bing(monkeypatch):
    client=PublicWeb();calls=[]
    def get(url,accepted,**kwargs):calls.append(url);raise WebAccessError('fixture denied')
    monkeypatch.setattr(client,'get',get)
    with pytest.raises(search.SearchUnavailable):client.search('fish',source='crossref')
    assert len(calls)==1 and calls[0].startswith('https://api.crossref.org/')


def test_baidu_and_wechat_parse_only_recognized_results_with_provenance():
    parser=search.PublicSearchHTML('baidu','https://www.baidu.com/s')
    parser.feed(HTML+'<a href="/navigation">Not a result</a><script>no execution</script>')
    assert len(parser.results)==2 and parser.results[0]['snippet']=='Actual snippet'
    assert parser.results[0]['title']=='B official' and parser.results[0]['url']=='https://example.org/b'
    parser=search.PublicSearchHTML('wechat','https://weixin.sogou.com/weixin')
    parser.feed(WECHAT)
    assert parser.results[0]['publisher']=='Public account' and parser.results[0]['title']=='B official'


def test_verification_is_distinct_from_empty_or_layout_failure(monkeypatch):
    monkeypatch.setattr(PublicWeb,'get',lambda self,url,accepted,**kwargs:(metadata('https://wappass.baidu.com/static/captcha/index.html'),'<html>Captcha check</html>'))
    with pytest.raises(search.SearchUnavailable) as e:PublicWeb().search('docs',source='baidu')
    assert e.value.search_report['attempts'][0]['failure_type']=='verification_required'
    monkeypatch.setattr(PublicWeb,'get',lambda self,url,accepted,**kwargs:(metadata(url),'<html>unknown layout</html>'))
    with pytest.raises(search.SearchUnavailable) as e:PublicWeb().search('docs',source='wechat')
    assert e.value.search_report['attempts'][0]['failure_type']=='unrecognized_response'


def test_crossref_returns_doi_metadata_and_empty_results_are_not_transport_failures(monkeypatch):
    client=PublicWeb()
    body={'status':'ok','message':{'items':[{'title':['Real <i>title</i>'],'DOI':'10.1234/a',
             'published':{'date-parts':[[2025,2,3]]},'container-title':['Journal']} ]}}
    monkeypatch.setattr(client,'get',lambda url,accepted,**kwargs:(metadata(url),json.dumps(body)))
    result=json.loads(client.search('fish',source='crossref'))
    assert result['results'][0]['url']=='https://doi.org/10.1234/a'
    assert result['results'][0]['published']=='2025-2-3'
    assert 'metadata only' in result['results'][0]['snippet']
    body['message']['items']=[]
    result=json.loads(client.search('unmatched',source='crossref'))
    assert result['status']=='empty' and result['attempts'][0]['status']=='empty'


def test_pubmed_joins_search_and_summaries_without_exposing_reference_text(monkeypatch):
    client=PublicWeb();calls=[]
    def get(url,accepted,**kwargs):
        calls.append((url,kwargs['deadline']))
        body={'esearchresult':{'idlist':['123']}} if 'esearch.fcgi' in url else {
            'result':{'uids':['123'],'123':{'uid':'123','title':'Fish RNA study','pubdate':'2025 Jan','source':'Journal'}}}
        return metadata(url),json.dumps(body)
    monkeypatch.setattr(client,'get',get)
    monkeypatch.setattr(search,'ncbi_slot',lambda check,deadline:check())
    result=json.loads(client.search('fish RNA',source='pubmed'))
    assert len(calls)==2 and calls[0][1]==calls[1][1]
    assert result['results'][0]['url']=='https://pubmed.ncbi.nlm.nih.gov/123/'
    assert 'metadata only' in result['results'][0]['snippet']
    assert len(result['attempts'][0]['requests'])==2


def test_pubmed_empty_ids_skip_summary_and_bad_ids_are_errors(monkeypatch):
    client=PublicWeb();ids=[];calls=[]
    def get(url,accepted,**kwargs):calls.append(url);return metadata(url),json.dumps({'esearchresult':{'idlist':ids}})
    monkeypatch.setattr(client,'get',get)
    monkeypatch.setattr(search,'ncbi_slot',lambda check,deadline:check())
    assert json.loads(client.search('fish',source='pubmed'))['status']=='empty' and len(calls)==1
    ids.append('123&api_key=not-a-real-key')
    with pytest.raises(search.SearchUnavailable):client.search('fish',source='pubmed')
    assert len(calls)==2


@pytest.mark.parametrize('source',['missing','unsupported-engine','360',{},None])
def test_invalid_or_unimplemented_source_is_rejected_before_network(source):
    with patch.object(PublicWeb,'get') as get:
        with pytest.raises(WebAccessError):PublicWeb().search('docs',source=source)
        get.assert_not_called()


def test_expired_shared_deadline_rejects_before_dns():
    with patch('pico.web_tools.public_addresses') as dns:
        with pytest.raises(WebAccessError,match='deadline'):PublicWeb().get('https://example.org/',{'text/html'},deadline=time.monotonic()-1)
        dns.assert_not_called()


def test_ncbi_calls_are_spaced_below_three_per_second_and_cancellable(monkeypatch):
    clock=[100.0];starts=[]
    monkeypatch.setattr(search,'_last_ncbi_request',0.0)
    monkeypatch.setattr(search.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(search.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds+1e-8))
    for _ in range(4):search.ncbi_slot(lambda:None,110.0);starts.append(clock[0])
    assert starts[-1]-starts[0]>=1.05
    def cancelled():raise RuntimeError('cancelled fixture')
    with pytest.raises(RuntimeError,match='cancelled'):search.ncbi_slot(cancelled,110)


def test_partial_and_failed_searches_have_structured_runtime_audit(tmp_path,monkeypatch):
    app=Workbench(tmp_path,harness_profile='workbench',model_factory=lambda:FakeModelClient([]),load_env=False,web_access=True)
    agent=app._build_agent(None,'ask',4,coordinator=True)
    def get(self,url,accepted,**kwargs):
        if 'bing.com' in url:raise WebAccessError('fixture unavailable')
        return metadata(url),WECHAT if 'sogou.com' in url else HTML
    monkeypatch.setattr(PublicWeb,'get',get)
    result=agent.tool_executor.execute('web_search',{'query':'docs'})
    assert result.metadata['tool_status']=='partial_success' and result.metadata['search_status']=='partial'
    assert [a['status'] for a in result.metadata['search_sources']]==['error','ok','ok']
    result=agent.tool_executor.execute('web_search',{'query':'different docs','source':'bing'})
    assert result.metadata['tool_error_code']=='web_search_failed'
    assert result.metadata['search_sources'][0]['source']=='bing'


def test_nonempty_noise_triggers_one_bounded_fallback_and_keeps_provenance(monkeypatch):
    calls=[];deadlines=[];client=PublicWeb()
    noisy='<rss><channel><item><title>Fish dictionary</title><link>https://example.org/fish</link></item></channel></rss>'
    relevant=HTML.replace('B <b>official</b>', 'Fish lipid conference Shantou 2026')
    def get(url,accepted,**kw):
        calls.append(url);deadlines.append(kw['deadline'])
        return metadata(url),noisy if 'bing.com' in url else WECHAT.replace('B official','Fish lipid conference Shantou 2026') if 'sogou.com' in url else relevant
    monkeypatch.setattr(client,'get',get)
    started=time.monotonic();result=json.loads(client.search('Fish lipid conference Shantou 2026',5))
    assert len(calls)==3 and result['attempts'][0]['status']=='ok'
    assert result['attempts'][0]['low_query_overlap'] and not result['attempts'][1]['low_query_overlap']
    assert set(result['results'][0]['sources'])=={'baidu','wechat'}
    assert any(row['sources']==['bing'] for row in result['results'])  # No discarded noise/failure evidence.
    assert deadlines[0]<=started+4.1 and deadlines[1]<=started+8.1
    assert 'not proof of absence' in result['limitations']


def test_relevant_auto_keeps_single_request_and_explicit_bing_never_falls_back(monkeypatch):
    client=PublicWeb();calls=[]
    body=RSS.replace('<title>A</title>','<title>Shantou lipid conference 2026</title>')
    def get(url,accepted,**kw):calls.append(url);return metadata(url),body
    monkeypatch.setattr(client,'get',get)
    assert len(json.loads(client.search('Shantou lipid conference 2026'))['attempts'])==1
    result=json.loads(client.search('unrelated complex topic',source='bing'))
    assert len(calls)==2 and len(result['attempts'])==1 and result['attempts'][0]['low_query_overlap']


def test_overlap_handles_chinese_site_constraints_and_does_not_claim_semantic_quality():
    noisy=[search.record('鱼类百科', 'https://example.org/fish', '鱼类的分类和旅游推荐')]
    assert search.query_overlap('鱼类脂质大会 汕头大学 2026 会议',noisy)['low_query_overlap']
    place_only=[search.record('汕头大学2026会议通知', 'https://eng.stu.edu.cn/notices')]
    assert search.query_overlap('鱼类脂质大会 汕头大学 2026 会议',place_only)['low_query_overlap']
    relevant=[search.record('2026 水产动物脂质营养研讨会', 'https://sci.stu.edu.cn/info/1/2.htm')]
    assert not search.query_overlap('site:stu.edu.cn 脂质 2026',relevant)['low_query_overlap']
    assert search.query_overlap('fish',noisy) is None  # No broad-query forced fan-out.


def test_noise_and_unavailable_fallback_keep_original_results_and_real_error(monkeypatch):
    def get(self,url,accepted,**kw):
        if 'bing.com' not in url:raise WebAccessError('fixture unavailable')
        return metadata(url),RSS
    monkeypatch.setattr(PublicWeb,'get',get)
    result=json.loads(PublicWeb().search('lipid conference Shantou'))
    assert result['status']=='partial' and result['results'][0]['sources']==['bing']
    assert result['attempts'][0]['low_query_overlap'] and result['attempts'][1]['status']=='error'


def test_auto_fallback_baidu_and_wechat_share_deadline_and_run_in_parallel(monkeypatch):
    barrier=threading.Barrier(2);deadlines=[]
    def get(self,url,accepted,**kwargs):
        if 'bing.com' in url:return metadata(url),'<rss><channel/></rss>'
        deadlines.append(kwargs['deadline']);barrier.wait(timeout=2)
        return metadata(url),WECHAT if 'sogou.com' in url else HTML
    monkeypatch.setattr(PublicWeb,'get',get)
    result=json.loads(PublicWeb().search('docs'))
    assert [a['source'] for a in result['attempts']]==['bing','baidu','wechat']
    assert len(deadlines)==2 and deadlines[0]==deadlines[1]


def test_wechat_search_redirect_is_not_presented_as_verified_mp_article():
    parser=search.PublicSearchHTML('wechat','https://weixin.sogou.com/weixin')
    parser.feed(WECHAT.replace('https://example.org/b','/link?url=opaque&amp;query=public words&amp;token=ephemeral')
                +'<script><li id="sogou_vr_11002601_box_5"><h3><a id="sogou_vr_11002601_title_5" href="https://evil.example/">Fake</a></h3></li></script>')
    assert len(parser.results)==1
    row=parser.results[0]
    assert row['url'].startswith('https://weixin.sogou.com/link?') and 'public%20words' in row['url']
    assert 'not yet verified' in row['link_kind'] and row['publisher']=='Public account'


def test_baidu_script_downgrade_is_not_executed_or_called_empty(monkeypatch):
    calls=[]
    def get(self,url,accepted,**kwargs):
        calls.append(url);return metadata(url),'<script>location.replace(location.href.replace("https://","http://"))</script>'
    monkeypatch.setattr(PublicWeb,'get',get)
    with pytest.raises(search.SearchUnavailable) as error:PublicWeb().search('docs',source='baidu')
    assert error.value.search_report['attempts'][0]['failure_type']=='script_required'
    assert len(calls)==1 and calls[0].startswith('https://www.baidu.com/')
