"""GLM search_pro + free international search, fixed endpoints and explicit provenance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import hashlib
import http.client
from .privacy import outbound, incoming, inherited_worker
import json
import os
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit

from .web_tools import MAX_BYTES,NOTICE,PublicWeb,PinnedHTTPSConnection,WebAccessError,public_addresses,public_url
from .web_search import SearchUnavailable,record,query_overlap,ADAPTERS


class HybridSearch:
    kind='hybrid'

    def config(self):
        return {'kind':self.kind,'sources':['glm','duckduckgo'],'fallback':'bing-intl',
                'engine':'search_pro','ready':bool(os.getenv('BIGMODEL_API_KEY')),
                'credentials':{'glm':bool(os.getenv('BIGMODEL_API_KEY')),'duckduckgo':'not_required'},
                'default_timeout_seconds':8,'foreign_route':'system network configuration',
                'automatic_retries':0,'tavily':False,'exa':False}

    def free_get(self,url,accepted,deadline,check,on_request):
        """Only the two fixed search endpoints may use configured system proxies.

        Arbitrary page reading stays on the existing pinned-IP PublicWeb path.
        No credentials, cookies, cross-host redirects or TLS exceptions are added.
        """
        url=public_url(url);part=urlsplit(url)
        if (part.hostname,part.path) not in {('lite.duckduckgo.com','/lite/'),('www.bing.com','/search')}:
            raise WebAccessError('Not a fixed international search endpoint')
        outbound(url,purpose='search')
        check();public_addresses(part.hostname,443)
        left=deadline-time.monotonic()
        if left<=0:raise WebAccessError('International search DNS deadline exceeded')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler(),NoRedirect())
        request=urllib.request.Request(url,headers={'User-Agent':'GoalTree/0.1 (public-web reader)',
            'Accept':', '.join(accepted),'Accept-Encoding':'identity'})
        on_request();started=time.monotonic()
        try:
            with opener.open(request,timeout=min(5,left)) as response:
                if response.status!=200:raise WebAccessError('International search HTTP '+str(response.status))
                kind=(response.headers.get('Content-Type') or '').split(';')[0].strip().lower()
                if kind not in accepted:raise WebAccessError('Unexpected international search content type')
                if response.headers.get('Content-Encoding','identity').lower() not in {'','identity'}:
                    raise WebAccessError('Compressed international search response unsupported')
                raw=bytearray()
                while True:
                    check()
                    if time.monotonic()>=deadline:raise WebAccessError('International search body deadline exceeded')
                    try:response.fp.raw._sock.settimeout(min(5,deadline-time.monotonic()))
                    except AttributeError:pass
                    chunk=response.read1(min(16384,MAX_BYTES+1-len(raw)))
                    if not chunk:break
                    raw.extend(chunk)
                    if len(raw)>MAX_BYTES:raise WebAccessError('International search response exceeds 512 KiB')
                text=incoming(raw.decode('utf-8','replace'))
                return {'url':url,'status':200,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
                        'network_route':'system','elapsed_ms':int((time.monotonic()-started)*1000)},text
        except urllib.error.HTTPError as exc:
            raise WebAccessError('International search HTTP '+str(exc.code)+'; no redirects, verification bypass or automatic retry') from None
        except (OSError,urllib.error.URLError,http.client.HTTPException):
            raise WebAccessError('International search connection failed or timed out under system network configuration') from None

    def glm(self,query,limit,deadline,check,on_request,requests):
        key=os.getenv('BIGMODEL_API_KEY','').strip()
        if not key:raise WebAccessError('GLM search_pro requires the local standard BIGMODEL_API_KEY')
        if any(ord(c)<33 or ord(c)>126 for c in key):raise WebAccessError('Invalid local GLM credential format')
        check();left=deadline-time.monotonic()
        if left<=0:raise WebAccessError('GLM search deadline exceeded')
        outbound('https://open.bigmodel.cn/api/paas/v4/web_search',query,purpose='search')
        addresses=public_addresses('open.bigmodel.cn',443)
        left=deadline-time.monotonic()
        if left<=0:raise WebAccessError('GLM search DNS deadline exceeded')
        conn=PinnedHTTPSConnection('open.bigmodel.cn',addresses[0],min(5,left))
        body=json.dumps({'search_engine':'search_pro','search_query':query,'count':limit,
                         'search_recency_filter':'noLimit','content_size':'medium'}).encode()
        audit={'url':'https://open.bigmodel.cn/api/paas/v4/web_search','method':'POST',
               'request_sha256':hashlib.sha256(body).hexdigest(),'retrieved_at':datetime.now(timezone.utc).isoformat()}
        requests.append(audit);on_request()
        try:
            conn.request('POST','/api/paas/v4/web_search',body=body,
                headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'application/json',
                         'Accept-Encoding':'identity','User-Agent':'GoalTree/0.1 (search_pro client)'})
            left=deadline-time.monotonic()
            if left<=0:raise WebAccessError('GLM search deadline exceeded')
            if conn.sock:conn.sock.settimeout(min(5,left))
            response=conn.getresponse();audit['status']=response.status
            if response.status!=200:raise WebAccessError('GLM search_pro HTTP '+str(response.status)+'; no automatic retry or engine substitution')
            if (response.getheader('Content-Type') or '').split(';')[0].strip().lower()!='application/json':
                raise WebAccessError('GLM search did not return JSON')
            if response.getheader('Content-Encoding','identity').lower() not in {'','identity'}:
                raise WebAccessError('Compressed search response unsupported')
            raw=bytearray()
            while True:
                check();left=deadline-time.monotonic()
                if left<=0:raise WebAccessError('GLM search body deadline exceeded')
                if conn.sock:conn.sock.settimeout(min(5,left))
                chunk=response.read1(min(16384,MAX_BYTES+1-len(raw)))
                if not chunk:break
                raw.extend(chunk)
                if len(raw)>MAX_BYTES:raise WebAccessError('GLM search response exceeds 512 KiB')
            audit.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
            try:value=json.loads(incoming(raw.decode('utf-8')))
            except (ValueError,UnicodeError):raise WebAccessError('GLM search returned malformed JSON') from None
            if not isinstance(value,dict) or 'error' in value or not isinstance(value.get('search_result'),list):
                raise WebAccessError('GLM search result schema is unrecognized; not proof of no matches')
            hits=[]
            for item in value['search_result'][:50]:
                if not isinstance(item,dict):continue
                try:hits.append(record(item.get('title',''),item.get('link',''),item.get('content',''),item.get('publish_date','')))
                except (ValueError,TypeError):continue
                if len(hits)>=limit:break
            if value['search_result'] and not hits:raise WebAccessError('GLM search returned no valid public result links')
            return hits
        except (OSError,http.client.HTTPException):
            raise WebAccessError('GLM search connection failed or timed out; no automatic retry') from None
        finally:conn.close()

    def search(self,query,limit,deadline,check,on_request,*,source='auto',enabled=None):
        if not isinstance(query,str) or not query.strip() or len(query)>400 or type(limit) is not int or not 1<=limit<=5:
            raise WebAccessError('Invalid bounded search query or limit')
        if source not in {'auto','glm','duckduckgo','bing-intl'}:raise WebAccessError('Unknown hybrid search source')
        enabled={'glm':True,'duckduckgo':True} if enabled is None else enabled
        deadline=min(deadline,time.monotonic()+8)
        def run(name):
            started=time.monotonic();requests=[];rows=[]
            def one(provider,end):
                if provider=='glm':return self.glm(query.strip(),limit,end,check,on_request,requests)
                def get(url,accepted):
                    meta,text=self.free_get(url,accepted,end,check,on_request);requests.append(meta);return meta,text
                return ADAPTERS[provider](get,query.strip(),limit)[0]
            if not enabled.get('glm' if name=='glm' else 'duckduckgo',False):
                return [({'source':name,'status':'error','failure_type':'disabled','error':'Source disabled in this workspace','count':0,'requests':[]},[])]
            providers=(name,'bing-intl') if name=='duckduckgo' and source=='auto' else (name,)
            for provider in providers:
                begin=time.monotonic();offset=len(requests)
                end=min(deadline,begin+4) if provider=='duckduckgo' and len(providers)>1 else deadline
                try:
                    hits=one(provider,end)
                    attempt={'source':provider,'status':'ok' if hits else 'empty','count':len(hits),
                             'elapsed_ms':int((time.monotonic()-begin)*1000),'requests':requests[offset:]}
                    overlap=query_overlap(query,hits)
                    if overlap:attempt.update(overlap)
                    rows.append((attempt,hits))
                    if hits:break
                except WebAccessError as exc:
                    rows.append(({'source':provider,'status':'error','count':0,'error':str(exc)[:240],
                        'failure_type':getattr(exc,'failure_type','retrieval_failed'),
                        'elapsed_ms':int((time.monotonic()-begin)*1000),'requests':requests[offset:]},[]))
                if time.monotonic()>=deadline:break
            return rows
        sources=('glm','duckduckgo') if source=='auto' else (source,)
        with ThreadPoolExecutor(max_workers=len(sources)) as pool:groups=list(pool.map(inherited_worker(run),sources))
        collected=[row for group in groups for row in group];fused={}
        for attempt,hits in collected:
            seen=set()
            for rank,hit in enumerate(hits,1):
                url=public_url(hit['url'])
                if url in seen:continue
                seen.add(url)
                row=fused.setdefault(url,{**hit,'sources':[],'_score':0.0})
                row['sources'].append(attempt['source']);row['_score']+=1/(60+rank)
        hits=sorted(fused.values(),key=lambda h:-h['_score'])[:limit]
        for row in hits:del row['_score']
        attempts=[a for a,_ in collected];failures=sum(a['status']=='error' for a in attempts)
        report={'untrusted':NOTICE,'source':source,'query':query,'attempts':attempts,'results':hits,
                'status':'partial' if failures else 'ok' if hits else 'empty',
                'ranking':'Reciprocal rank fusion of requested providers; not a factual confidence score.',
                'limitations':'GLM search_pro and free public international search return leads, not verified facts. Captcha and rate limits are not bypassed. Tavily and Exa are not queried.'}
        if attempts and failures==len(attempts):report['status']='error';raise SearchUnavailable(report)
        return report
