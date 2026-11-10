"""Public search adapters with explicit provenance and bounded multi-source fusion."""
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
import json
import re
import threading
import time
import unicodedata
from urllib.parse import quote, urljoin, urlsplit
import xml.etree.ElementTree as ET

from .web_tools import NOTICE, TOTAL_TIMEOUT, WebAccessError, plain_text, public_url, render

SOURCES = {
    'bing': 'Bing public RSS', 'baidu': 'Baidu public search',
    'wechat': 'WeChat articles via Sogou public index (not full WeChat search)',
    'crossref': 'Crossref bibliographic metadata API', 'pubmed': 'NCBI PubMed E-utilities',
}
GROUPS = {'auto': ('bing', 'baidu', 'wechat'), 'compare': ('bing','baidu','wechat'),
          'research': ('crossref', 'pubmed'), 'all': tuple(SOURCES)}
_rate_lock = threading.Lock()
_last_ncbi_request = 0.0
SEARCH_REVISION = 4
SEARCH_LIMITATION = ('Search results are leads, not proof of absence. Bing public RSS may differ from the browser results; '
                     'low_query_overlap is a lexical warning, not semantic verification or proof a page is unindexed.')


def query_overlap(query, hits):
    """Cheap conservative noise alarm, not an answer-quality score or a query rewriter."""
    query = unicodedata.normalize('NFKC', query).casefold()
    # Site operators are constraints, not words to match in a page title.
    sites = re.findall(r'\bsite:([a-z0-9.-]+)', query)
    query = re.sub(r'\bsite:[a-z0-9.-]+', '', query)
    terms = list(dict.fromkeys(re.findall(r'[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}', query)))[:20]
    if len(terms) + len(sites) < 2:
        return None
    def matches(term, text):
        if re.fullmatch(r'[\u3400-\u9fff]+', term):
            grams = [term[i:i+2] for i in range(len(term)-1)]
            return sum(g in text for g in grams) >= max(1, len(grams)/2)
        return re.search(r'(?<![a-z0-9_])'+re.escape(term)+r'(?![a-z0-9_])',text) is not None
    def score(hit):
        text = unicodedata.normalize('NFKC', hit['title'] + ' ' + hit.get('snippet', '')).casefold()
        host = urlsplit(hit['url']).hostname or ''
        matched = sum(matches(term,text) for term in terms) + sum(host == site or host.endswith('.'+site) for site in sites)
        return matched / (len(terms)+len(sites))
    maximum = max((score(hit) for hit in hits), default=0)
    texts = [unicodedata.normalize('NFKC', hit['title']+' '+hit.get('snippet','')).casefold() for hit in hits]
    missing = [term for term in terms if not term.isdecimal() and not any(matches(term,text) for text in texts)]
    return {'max_query_overlap': round(maximum, 3), 'low_query_overlap': maximum < .5 or bool(missing),
            'unmatched_query_terms': missing[:5]}


def ncbi_slot(check, deadline):
    global _last_ncbi_request
    with _rate_lock:
        while True:
            check()
            now = time.monotonic()
            if now >= deadline:
                raise WebAccessError('PubMed request budget expired')
            delay = .35 - (now - _last_ncbi_request)
            if delay <= 0:
                _last_ncbi_request = now
                return
            time.sleep(min(delay, .05, deadline - now))


def xml_document(text):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise WebAccessError('unsupported XML declarations in search response')
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        raise WebAccessError('search service did not return valid RSS') from None
    if root.tag != 'rss':
        raise WebAccessError('search service did not return RSS')
    return root


def record(title, url, snippet='', published=''):
    if not isinstance(title, str) or not title.strip():
        raise WebAccessError('search result has no valid title')
    return {'title': plain_text(title)[:140], 'url': public_url(url),
            'snippet': plain_text(snippet)[:250], 'published': str(published)[:60]}


def bing(get, query, limit):
    metadata, text = get('https://www.bing.com/search?format=rss&q=' + quote(query, safe=''),
                         {'text/xml', 'application/rss+xml', 'application/xml'})
    results = []
    for item in xml_document(text).findall('./channel/item'):
        try:
            results.append(record(item.findtext('title') or '', item.findtext('link') or '',
                                  item.findtext('description') or '', item.findtext('pubDate') or ''))
        except WebAccessError:
            continue
        if len(results) >= limit:
            break
    return results, [metadata]


class PublicSearchHTML(HTMLParser):
    """Read bounded, recognized result containers; never execute page scripts."""
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','wbr'}
    HIDDEN = {'script','style','noscript','template','svg'}

    def __init__(self, source, base_url):
        super().__init__(convert_charrefs=True)
        self.source, self.base_url = source, base_url
        self.stack, self.results = [], []
        self.current, self.capture = None, None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in self.VOID: return
        self.stack.append(tag)
        if any(t in self.HIDDEN for t in self.stack): return
        classes = set(attrs.get('class','').split())
        box = (self.source == 'wechat' and tag == 'li' and re.fullmatch(r'sogou_vr_11002601_box_\d+',attrs.get('id',''))
               or self.source == 'baidu' and tag == 'div' and bool(classes & {'result','result-op','c-container'}))
        if box and self.current is None:
            self.current = {'depth':len(self.stack),'title':'','url':'','snippet':'','publisher':''}
        if self.current is None or self.capture: return
        title = tag == 'a' and ('h3' in self.stack and (self.source == 'baidu'
                or re.fullmatch(r'sogou_vr_11002601_title_\d+',attrs.get('id',''))))
        if title and not self.current['url']:
            if not attrs.get('href'):return
            try:self.current['url'] = public_url(urljoin(self.base_url, attrs.get('href','').replace(' ','%20')))
            except WebAccessError:return
            self.capture = (len(self.stack),'title',[])
        elif (self.source == 'wechat' and tag == 'p' and 'txt-info' in classes
              or self.source == 'baidu' and bool(classes & {'c-abstract','c-line-clamp3'})):
            self.capture = (len(self.stack),'snippet',[])
        elif self.source == 'wechat' and tag == 'span' and 'all-time-y2' in classes:
            self.capture = (len(self.stack),'publisher',[])

    def handle_data(self, text):
        if self.capture and not any(t in self.HIDDEN for t in self.stack):self.capture[2].append(text)

    def handle_endtag(self, tag):
        if tag not in self.stack:return
        depth = len(self.stack)-self.stack[::-1].index(tag)
        if self.capture and depth <= self.capture[0]:
            _,field,parts = self.capture
            self.current[field] = ' '.join(''.join(parts).split())
            self.capture = None
        if self.current and depth <= self.current['depth']:
            item,self.current = self.current,None
            if item['title'] and item['url']:
                row = record(item['title'],item['url'],item['snippet'])
                if item['publisher']:row['publisher'] = item['publisher'][:80]
                if urlsplit(row['url']).hostname in {'www.baidu.com','weixin.sogou.com'}:
                    row['link_kind'] = 'search_redirect; target/full text not yet verified; may expire'
                self.results.append(row)
        self.stack = self.stack[:depth-1]


class SourceUnavailable(WebAccessError):
    def __init__(self, message, failure_type):
        self.failure_type = failure_type
        super().__init__(message)


def html_search(get, query, limit, source):
    url = ('https://www.baidu.com/s?wd=' + quote(query,safe='') + '&rn=' + str(limit)
           if source == 'baidu' else 'https://weixin.sogou.com/weixin?type=2&query='+quote(query,safe=''))
    metadata,text = get(url,{'text/html','application/xhtml+xml'})
    host,path = urlsplit(metadata['url']).hostname,urlsplit(metadata['url']).path
    if host in {'wappass.baidu.com','passport.baidu.com'} or '/antispider' in path or '/captcha' in path:
        raise SourceUnavailable(source+' requires human verification; no verified search results', 'verification_required')
    parser = PublicSearchHTML(source,metadata['url'])
    parser.feed(text)
    if not parser.results:
        if any(marker in text for marker in ('访问过于频繁','请输入验证码','安全验证','验证码验证')):
            raise SourceUnavailable(source+' requires verification or is rate limited','verification_required')
        if 'location.replace' in text or 'location.href' in text:
            raise SourceUnavailable(source+' returned a script/redirect page; scripts and HTTPS downgrade are not followed','script_required')
        if any(marker in text for marker in ('抱歉，没有找到与','没有找到相关的微信公众号文章','没有找到相关的微信文章')):
            return [],[metadata]
        raise SourceUnavailable(source+' returned no recognized result containers; layout/empty not verified','unrecognized_response')
    return parser.results[:limit], [metadata]


def baidu(get,query,limit):return html_search(get,query,limit,'baidu')


def wechat(get,query,limit):return html_search(get,query,limit,'wechat')


def json_document(get, url):
    metadata, text = get(url, {'application/json', 'text/json'})
    try:
        data = json.loads(text)
    except ValueError:
        raise WebAccessError('search service did not return JSON') from None
    if not isinstance(data, dict):
        raise WebAccessError('invalid search JSON object')
    return data, metadata


def crossref(get, query, limit):
    url = ('https://api.crossref.org/works?query.bibliographic=' + quote(query, safe='')
           + '&rows=' + str(limit) + '&select=DOI,title,URL,published,container-title')
    data, metadata = json_document(get, url)
    items = data.get('message', {}).get('items') if isinstance(data.get('message'), dict) else None
    if data.get('status') != 'ok' or not isinstance(items, list):
        raise WebAccessError('invalid Crossref works response')
    results = []
    for item in items[:limit]:
        try:
            title = item['title'][0]
            doi = item['DOI']
            if not isinstance(doi, str) or not doi.startswith('10.'):
                raise ValueError()
            date = item.get('published', {}).get('date-parts', [[]])[0]
            journal = '; '.join(item.get('container-title', []))
            results.append(record(title, 'https://doi.org/' + quote(doi, safe='/'),
                                  'Bibliographic metadata only. ' + journal + '; DOI: ' + doi,
                                  '-'.join(map(str, date))))
        except (KeyError, TypeError, IndexError, ValueError, AttributeError):
            raise WebAccessError('invalid Crossref bibliographic record') from None
    return results, [metadata]


def pubmed(get, query, limit):
    data, metadata = json_document(get, 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi'
                                  '?db=pubmed&retmode=json&retmax=' + str(limit) + '&term=' + quote(query, safe=''))
    result = data.get('esearchresult')
    if not isinstance(result, dict) or not isinstance(result.get('idlist'), list) or data.get('error'):
        raise WebAccessError('invalid PubMed search response')
    if result.get('errorlist'):
        raise WebAccessError('PubMed rejected query terms: ' + str(result['errorlist'])[:160])
    ids = result['idlist'][:limit]
    if not all(isinstance(i, str) and i.isascii() and i.isdigit() and 1 <= len(i) <= 12 for i in ids):
        raise WebAccessError('invalid PubMed identifiers')
    if not ids:
        return [], [metadata]
    summaries, meta2 = json_document(get, 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi'
                                     '?db=pubmed&retmode=json&id=' + ','.join(ids))
    result = summaries.get('result')
    if not isinstance(result, dict) or summaries.get('error'):
        raise WebAccessError('invalid PubMed summary response')
    hits = []
    for uid in ids:
        item = result.get(uid)
        if not isinstance(item, dict) or item.get('error') or item.get('uid') != uid:
            raise WebAccessError('missing or mismatched PubMed summary')
        hits.append(record(item.get('title'), 'https://pubmed.ncbi.nlm.nih.gov/' + uid + '/',
                           'Bibliographic metadata only. ' + str(item.get('fulljournalname', item.get('source', '')))
                           + '; PMID: ' + uid, item.get('pubdate', '')))
    return hits, [metadata, meta2]


ADAPTERS = {'bing': bing, 'baidu': baidu, 'wechat': wechat, 'crossref': crossref, 'pubmed': pubmed}


def search(client, query, limit=5, source='auto', *, rendered=True):
    if not isinstance(query, str) or not query.strip() or len(query) > 400:
        raise WebAccessError('search query must contain 1..400 characters')
    if type(limit) is not int or not 1 <= limit <= 5:
        raise WebAccessError('search limit must be 1..5')
    if not isinstance(source, str) or (source not in SOURCES and source not in GROUPS):
        raise WebAccessError('source must be auto, compare, bing, baidu, wechat, crossref, pubmed, research or all')
    sources = GROUPS.get(source, (source,))
    deadline = min(time.monotonic() + 8, getattr(client,'deadline',float('inf')))

    def run(name, source_deadline=None):
        started = time.monotonic()
        end = min(deadline, source_deadline or deadline)
        requests = []
        def get(url, accepted):
            client.check()
            if name == 'pubmed':
                ncbi_slot(client.check, end)
            metadata, body = client.get(url, accepted, deadline=end)
            requests.append(metadata)
            return metadata, body
        try:
            hits, _ = ADAPTERS[name](get, query.strip(), limit)
            attempt = {'source': name, 'status': 'ok' if hits else 'empty', 'count': len(hits),
                       'elapsed_ms': int((time.monotonic() - started) * 1000), 'requests': requests}
            overlap = query_overlap(query, hits) if name in {'bing', 'baidu', 'wechat'} else None
            if overlap is not None: attempt.update(overlap)
            return attempt, hits
        except WebAccessError as exc:
            return {'source': name, 'status': 'error', 'count': 0, 'error': str(exc)[:220],
                    'failure_type':getattr(exc,'failure_type','retrieval_failed'),
                    'elapsed_ms': int((time.monotonic() - started) * 1000), 'requests': requests}, []

    if source == 'auto':
        # Reserve part of the SAME eight-second deadline for bounded fallback.
        collected=[run('bing', time.monotonic()+4)]
        first = collected[0]
        if not first[1] or first[0].get('low_query_overlap'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                collected.extend(pool.map(run, ('baidu','wechat')))
    else:
        with ThreadPoolExecutor(max_workers=min(3,len(sources))) as pool:
            collected = list(pool.map(run, sources))
    attempts = [attempt for attempt, _ in collected]
    fused = {}
    for attempt, hits in collected:
        seen = set()
        for rank, hit in enumerate(hits, 1):
            key = public_url(hit['url'])
            if key in seen:
                continue
            seen.add(key)
            entry = fused.setdefault(key, {**hit, 'sources': [], '_score': 0.0, '_low': True})
            entry['sources'].append(attempt['source'])
            entry['_score'] += 1 / (60 + rank)
            entry['_low'] &= attempt['source'] == 'bing' and attempt.get('low_query_overlap', False)
    hits = sorted(fused.values(), key=lambda row: (source == 'auto' and row['_low'], -row['_score']))[:limit]
    for hit in hits:
        del hit['_score']
        del hit['_low']
    failures = sum(a['status'] == 'error' for a in attempts)
    report = {'untrusted': NOTICE, 'source': source, 'query': query, 'attempts': attempts,
              'status': 'partial' if failures else 'ok' if hits else 'empty',
              'results': hits, 'ranking': ('auto: low-overlap Bing results last; ' if source == 'auto' else '')
                  + 'reciprocal rank fusion; not a factual quality score',
              'limitations': SEARCH_LIMITATION}
    if any(a.get('low_query_overlap') for a in attempts):
        report['recovery_hint'] = ('One source missed query terms. If evidence is still insufficient, use the remaining search '
            'with source=baidu or wechat (WeChat via Sogou public article index): site:organizer-domain for general web search, '
            'or the public account/institution name for WeChat articles, plus ONE short distinctive subject phrase '
            '(2..4 Chinese characters, or 1..2 English words), optionally the year. '
            'The site already scopes the institution: do not add the institution name, long guessed event title, '
            'or generic conference/notification words again. '
            'do not retry broad/translated queries on Bing RSS or read a search page to bypass the search budget. '
            'If a related official site is found but the exact article is not, read that site homepage and web_find '
            'the short subject phrase before concluding; do not spend pages verifying obviously unrelated results. '
            'Word overlap is only a warning: verify the exact subject, year and edition in official page contents.')
    if failures == len(attempts):
        report['status'] = 'error'
        raise SearchUnavailable(report)
    return render(report) if rendered else report


class SearchUnavailable(WebAccessError):
    def __init__(self, report):
        self.search_report = report
        super().__init__('all requested search sources failed: ' + '; '.join(
            row['source']+': '+row.get('error','unavailable') for row in report['attempts']))
