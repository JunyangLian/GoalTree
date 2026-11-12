"""Small session-owned web snapshots and per-turn research budgets."""
from contextlib import contextmanager
import hashlib
import json
import threading
import time
import textwrap
from bisect import bisect_right
from types import SimpleNamespace
from urllib.parse import unquote

from .web_tools import NOTICE, PageText, PublicWeb, WebAccessError, public_url

TTL = 900
PAGE_FORMAT = 3
MAX_OUTPUT = 10000  # Characters, not a claim about provider Token counts.


def range_citation(data):
    span=f"L{data['start_line']}-L{data['end_line']}"
    if data.get('document_type')=='pdf':
        data['start_page']=data['lines'][0]['page'];data['end_page']=data['lines'][-1]['page']
        span=f"P{data['start_page']}-P{data['end_page']}:"+span
    return data['page_id']+':'+span


class WebBudgetExceeded(WebAccessError):
    web_budget_exhausted = True


class WebBudget:
    def __init__(self, output_chars=MAX_OUTPUT, result_chars=3800, read_lines=20):
        for name,value,minimum,maximum in (
            ('output_chars',output_chars,10000,1000000),
            ('result_chars',result_chars,3800,32000),('read_lines',read_lines,20,200)):
            if type(value) is not int or not minimum<=value<=maximum:
                raise ValueError(f'{name} must be an integer in {minimum}..{maximum}')
        if result_chars>output_chars:raise ValueError('result_chars must not exceed output_chars')
        self.output_limit,self.result_limit,self.read_limit=output_chars,result_chars,read_lines
        self.searches = self.pages = self.requests = self.output_chars = 0
        self.network_seconds = 0.0
        self.lock = threading.Lock()
        self.operations = threading.Lock()

    def request(self):
        with self.lock:
            if self.requests >= 8:
                raise WebBudgetExceeded('network attempt budget exhausted; answer with existing evidence and disclose gaps')
            self.requests += 1

    @contextmanager
    def network(self, kind):
        # Tool operations are sequential; the sources inside a search may run in parallel.
        with self.operations:
            with self.lock:
                if self.output_chars > self.output_limit-500 or self.network_seconds >= 20 or self.requests >= 8:
                    raise WebBudgetExceeded('web output/network budget exhausted; use existing evidence')
                if (kind == 'search' and self.searches >= 2) or (kind == 'page' and self.pages >= 3):
                    raise WebBudgetExceeded('web search/page budget exhausted; stop expanding research and disclose gaps')
                if kind == 'search':self.searches += 1
                else:self.pages += 1
                allowance = min(8, 20 - self.network_seconds)
            started = time.monotonic()
            try:
                yield started + allowance
            finally:
                with self.lock:self.network_seconds += time.monotonic() - started

    def emit(self, data):
        with self.lock:
            room = min(self.result_limit, self.output_limit - self.output_chars)
            data['web_budget'] = self._state()
            if room < 500:
                raise WebBudgetExceeded('web tool output character quota exhausted (not model context window); use returned evidence and disclose unread ranges')
            while len(json.dumps(data, ensure_ascii=False)) > room:
                if data.get('lines') and len(data['lines']) > 1:
                    data['lines'].pop()
                    data['end_line'] = data['lines'][-1]['line']
                    data['next_start'] = data['end_line'] + 1
                    data['citation'] = range_citation(data)
                    data['truncated'] = True
                elif data.get('results'):
                    data['results'].pop();data['truncated'] = True
                elif data.get('matches') and len(data['matches']) > 1:
                    data['matches'].pop();data['truncated'] = True
                else:
                    raise WebBudgetExceeded('web evidence envelope exceeds remaining tool output character quota (not model context window)')
            result = json.dumps(data, ensure_ascii=False)
            self.output_chars += len(result)
            return result

    def _state(self):
        return {'searches': self.searches, 'new_pages': self.pages, 'network_attempts': self.requests,
                'network_seconds': round(self.network_seconds, 3), 'output_chars_before_result': self.output_chars,
                'limits': {'searches': 2, 'new_pages': 3, 'network_attempts': 8, 'network_seconds': 20,
                           'output_chars':self.output_limit,'result_chars':self.result_limit,'read_lines':self.read_limit}}


class WebContext:
    def __init__(self, agent, search_mcp=None):
        self.agent = agent
        self.client = PublicWeb(agent.cancellation)
        self.client.on_request = lambda: self.budget.request()
        self.search_mcp = search_mcp
        self.scope = {'workspace': str(agent.root.resolve()), 'session': agent.session['id']}

    @property
    def budget(self):
        return self.agent.web_budget

    def _recent(self, kind):
        with self.agent.evidence.connect() as db:
            rows = db.execute('SELECT id,metadata FROM objects WHERE session=? AND name=? ORDER BY rowid DESC LIMIT 32',
                              (self.agent.session['id'], kind)).fetchall()
        return [(row['id'], json.loads(row['metadata'])) for row in rows]

    def _load(self, key, kind):
        if kind=='web_page' and key.startswith('wp_'):key='ev_'+key[3:]
        row = self.agent.evidence.get(key, self.agent.session['id'])
        if row['name'] != kind:
            raise WebAccessError('not a webpage snapshot ID; use page_id wp_... from web_read or the original URL, not a generic tool Evidence ID')
        if row['metadata'].get('web_scope') != self.scope:
            raise WebAccessError('web page/cache does not belong to this workspace and session')
        return json.loads(row['content']), row['metadata']

    def _cached(self, kind, key):
        for page_id, meta in self._recent(kind):
            if (meta.get('web_scope') == self.scope and key in meta.get('cache_keys', [])
                    and (kind!='web_page' or meta.get('extraction_version')==PAGE_FORMAT)
                    and 0 <= time.time() - meta['fetched_unix'] < TTL):
                return page_id, *self._load(page_id, kind)
        return None

    def _save(self, kind, content, keys, metadata):
        metadata = {**metadata, 'web_scope': self.scope, 'cache_keys': keys, 'fetched_unix': time.time()}
        run = getattr(self.agent.current_task_state, 'run_id', '')
        key = self.agent.evidence.put(self.agent.session['id'], run, kind,
                                      json.dumps(content, ensure_ascii=False), metadata)
        return key, content, metadata

    def references(self):
        """Small authoritative pointers for follow-ups, never stale raw tool transcripts."""
        rows=[]
        for key,meta in self._recent('web_page'):
            if meta.get('web_scope')!=self.scope or not self.agent.evidence.authorized(meta):continue
            age=max(0,time.time()-meta['fetched_unix'])
            rows.append({'page_id':'wp_'+key[3:],'url':meta['url'],'retrieved_at':meta['retrieved_at'],
                         'stale':age>=TTL,'extraction_version':meta.get('extraction_version',1),
                         'document_type':meta.get('document_type','html'),
                         'trust':'untrusted historical extracted text snapshot; no JS or OCR; read URL to update an older extractor'})
            if len(rows)==3:break
        return rows

    def search(self, query, limit=5, source='auto', refresh=0):
        from .web_search import search, SEARCH_REVISION, SearchUnavailable
        if type(refresh) is not int or refresh not in {0, 1}:
            raise WebAccessError('refresh must be 0 or 1')
        # Validate before reading cache, without any network or keyword routing.
        if not isinstance(query, str) or not query.strip() or len(query) > 400 or type(limit) is not int or not 1 <= limit <= 5:
            raise WebAccessError('invalid bounded search query/limit')
        from .web_search import SOURCES, GROUPS
        if not isinstance(source,str) or (source not in SOURCES and source not in GROUPS and source not in {'mcp','glm'}):raise WebAccessError('unknown search source')
        hybrid=getattr(self.search_mcp,'kind',None)=='hybrid'
        use_hybrid=hybrid and source in {'auto','glm','duckduckgo','bing-intl'}
        if source=='glm' and not hybrid:raise WebAccessError('GLM search_pro backend is not configured')
        use_mcp = self.search_mcp is not None and not hybrid and source in {'auto','mcp'}
        if use_mcp:
            from .extension_hub import ExtensionSettings
            exa_settings=ExtensionSettings(self.agent.root)
            if not exa_settings.read()['mcp']['exa'] and exa_settings.path.exists():
                raise WebAccessError('Exa Search MCP is disabled in this workspace; choose an explicitly authorized source')
        if source=='mcp' and not use_mcp:raise WebAccessError('Search MCP is not configured; no silent fallback')
        enabled=None
        if use_hybrid:
            from .extension_hub import ExtensionSettings
            switches=ExtensionSettings(self.agent.root).read()['mcp']
            enabled={'glm':switches['glm-search'],'duckduckgo':switches['international-search']}
            if not any(enabled.values()):raise WebAccessError('Both default search providers are disabled in this workspace')
        cache_key = json.dumps([SEARCH_REVISION,'glm-free' if use_hybrid else 'exa-mcp' if use_mcp else 'public',enabled,query.strip(), limit, source], ensure_ascii=False)
        found = self._cached('web_search_cache', cache_key) if not refresh else None
        if found:
            evidence_id, report, meta = found
            report.update(cache='hit', cache_evidence_id=evidence_id, cache_age_seconds=round(time.time()-meta['fetched_unix'], 1))
        else:
            try:
                with self.budget.network('search') as deadline:
                    proxy = SimpleNamespace(check=self.client.check, deadline=deadline,
                        get=lambda url, accepted, **kw:self.client.get(url, accepted, deadline=min(deadline,kw.get('deadline',deadline))))
                    report = (self.search_mcp.search(query,limit,deadline,self.client.check,self.budget.request,source=source,enabled=enabled)
                              if use_hybrid else self.search_mcp.search(query,limit,deadline,self.client.check,self.budget.request)
                              if use_mcp else search(proxy, query, limit, source, rendered=False))
            except SearchUnavailable as exc:
                exc.audit_evidence_id = self._save('web_search_report',exc.search_report,[],{})[0]
                raise
            # Do not cache partial failures; a failed provider must not be sticky for 15 minutes.
            cacheable = report['status'] in {'ok','empty'}
            evidence_id = self._save('web_search_cache' if cacheable else 'web_search_report',
                                     report,[cache_key] if cacheable else [],{})[0]
            report['cache'] = 'miss'
        # Keep complete request URLs/hashes in authorized audit evidence, rather
        # than letting long verification/redirect URLs crowd out useful hits.
        preview = {**{k:v for k,v in report.items() if k!='mcp_response'},'audit_evidence_id':evidence_id,'attempts':[
            {**{k:v for k,v in row.items() if k != 'requests'},'request_count':len(row.get('requests',[]))}
            for row in report['attempts']]}
        return self.budget.emit(preview)

    def read(self, url='', page_id='', start=1, limit=10, refresh=0, pdf_page=0):
        from .pdf_read import document_url,extract_pdf
        if bool(url) == bool(page_id):raise WebAccessError('provide exactly one of url or page_id')
        if type(start) is not int or start < 1 or type(limit) is not int or not 1 <= limit <= self.budget.read_limit:
            raise WebAccessError(f'page start/limit must be positive, limit <={self.budget.read_limit}')
        if type(refresh) is not int or refresh not in {0, 1} or (page_id and refresh):
            raise WebAccessError('refresh=1 requires a URL')
        if type(pdf_page) is not int or not 0<=pdf_page<=40 or (pdf_page and start!=1):
            raise WebAccessError('pdf_page must be 0..40; use start=1 when seeking a PDF page, then continue with global next_start and pdf_page=0')
        cache = 'snapshot'
        if page_id:
            page, meta = self._load(page_id, 'web_page')
        else:
            url = public_url(url)
            target=document_url(url)
            decoded=target
            for _ in range(4):
                if self.agent.redact_text(decoded)!=decoded:raise WebAccessError('sensitive values must not be sent in document URLs')
                next_value=unquote(decoded)
                if next_value==decoded:break
                decoded=next_value
            found = self._cached('web_page', target) if not refresh else None
            if found:
                page_id, page, meta = found;cache = 'hit'
            else:
                with self.budget.network('page') as deadline:
                    metadata, raw = self.client.get(target, {'text/html','application/xhtml+xml','text/plain','application/pdf'},deadline=deadline)
                    parsed=extract_pdf(raw,self.client.check,deadline) if metadata['content_type']=='application/pdf' else None
                if parsed is not None:
                    lines=[];line_pages=[];empty=[]
                    for number,text in enumerate(parsed['pages'],1):
                        text=self.agent.redact_text(text)
                        parts=[part for line in text.splitlines() if line.strip()
                               for part in textwrap.wrap(' '.join(line.split()),width=160)]
                        if not parts:
                            empty.append(number);parts=[f'[PDF page {number}: no extractable text; may require OCR, which was not run]']
                        lines.extend(parts);line_pages.extend([number]*len(parts))
                    status='needs_ocr' if len(empty)==parsed['page_count'] else 'partial_text' if empty else 'text_extracted'
                    page={'title':self.agent.redact_text(parsed['title']) or 'PDF document','lines':lines,'line_pages':line_pages,
                          'document_type':'pdf','embedded_pages':[]}
                    metadata.update(document_type='pdf',page_count=parsed['page_count'],text_extraction_status=status,
                                    empty_text_pages=empty,parser=parsed['parser'],parser_memory_limit_bytes=parsed['memory_limit_bytes'])
                else:
                    if metadata['content_type'] == 'text/plain':title, body = '', raw
                    else:
                        parser=PageText(metadata['url']);parser.feed(raw)
                        title, body=' '.join(parser.title)[:160], ''.join(parser.parts)
                    body=self.agent.redact_text(body)
                    lines=[part for line in body.splitlines() if line.strip()
                           for part in textwrap.wrap(' '.join(line.split()),width=160)] or ['']
                    page={'title':title,'lines':lines,'embedded_pages':parser.embedded_pages if metadata['content_type']!='text/plain' else []}
                meta={**metadata,'extraction_version':PAGE_FORMAT,'text_sha256':hashlib.sha256('\n'.join(lines).encode()).hexdigest()}
                page_id, page, meta=self._save('web_page',page,list(dict.fromkeys([url,target,metadata['url']])),meta);cache='miss'
        lines=page['lines']
        ceiling=len(lines)
        if pdf_page:
            if page.get('document_type')!='pdf':raise WebAccessError('pdf_page requires a PDF document')
            indexes=[i for i,n in enumerate(page['line_pages']) if n==pdf_page]
            if not indexes:raise WebAccessError('PDF page exceeds document page count')
            start=indexes[0]+1;ceiling=indexes[-1]+1
        if page_id.startswith('ev_'):page_id='wp_'+page_id[3:]
        if start > len(lines):raise WebAccessError('start exceeds page line count: '+str(len(lines)))
        end=min(ceiling,start+limit-1)
        entries=[{'line':i,'text':lines[i-1],**({'page':page['line_pages'][i-1]} if page.get('document_type')=='pdf' else {})} for i in range(start,end+1)]
        result={**self._page_meta(page_id,meta),'title':page['title'],'cache':cache,
                'start_line':start,'end_line':end,'total_lines':len(lines),'next_start':end+1 if end<len(lines) else None,
                'truncated':end<len(lines),'lines':entries,
                'rendering':('PDF text layer only; images, table layout and scans not verified; no OCR'
                             if page.get('document_type')=='pdf' else 'static HTML only; scripts not executed; missing details do not prove unpublished content')}
        result['citation']=range_citation(result)
        embedded=page.get('embedded_pages') or [line for line in lines if 'Embedded page (not yet read):' in line]
        if embedded:
            result['embedded_content']={'count':len(embedded),'status':'not_read',
                'document_urls':embedded[:2],
                'warning':'Missing HTML details do not mean unpublished or absent. This HTML does not include the embedded document text. Read its URL with web_read to verify dates, edition and organizers.'}
        return self.budget.emit(result)

    def find(self, page_id, pattern, limit=3):
        if not isinstance(pattern,str) or not 1 <= len(pattern.strip()) <= 160 or type(limit) is not int or not 1 <= limit <= 3:
            raise WebAccessError('find requires literal pattern 1..160 characters and limit 1..3')
        self.client.check()
        page,meta=self._load(page_id,'web_page')
        if page_id.startswith('ev_'):page_id='wp_'+page_id[3:]
        folded=[line.casefold() for line in page['lines']]
        offsets=[];position=0
        for line in folded:offsets.append(position);position+=len(line)+1
        haystack=' '.join(folded);needle=pattern.casefold();hits=[];position=0
        while True:
            index=haystack.find(needle,position)
            if index<0:break
            start=bisect_right(offsets,index);end=bisect_right(offsets,index+len(needle)-1)
            if page.get('document_type')=='pdf':
                first,last=page['line_pages'][start-1],page['line_pages'][end-1]
                if first!=last or first in meta.get('empty_text_pages',[]):
                    position=index+len(needle);continue
            hits.append({'line':start,'end_line':end,'text':' '.join(page['lines'][start-1:end])[:200],
                         **({'page':first} if page.get('document_type')=='pdf' else {}),
                         'citation':f'{page_id}:'+(f'P{first}:' if page.get('document_type')=='pdf' else '')+f'L{start}-L{end}'})
            position=index+len(needle)
        return self.budget.emit({**self._page_meta(page_id,meta),'pattern':pattern,'total_matches':len(hits),
                                 'matches':hits[:limit],'truncated':len(hits)>limit,'network_request':False})

    def _page_meta(self,page_id,meta):
        age=max(0,time.time()-meta['fetched_unix'])
        result={'untrusted':NOTICE,'page_id':page_id,'url':meta['url'],'retrieved_at':meta['retrieved_at'],
                'response_sha256':meta['sha256'],'text_sha256':meta['text_sha256'],
                'age_seconds':round(age,1),'stale':age>=TTL}
        if meta.get('document_type')=='pdf':
            result.update({k:meta[k] for k in ('document_type','page_count','text_extraction_status','empty_text_pages','parser')})
            result['read_scope']='Only the returned text lines are in context; empty-text pages/images were not read or OCRed. PDF layout/table structure is not verified.'
        return result
