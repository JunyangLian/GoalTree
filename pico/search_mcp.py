"""Allowlisted, keyless Exa search over MCP Streamable HTTP; no code execution."""
from datetime import datetime, timezone
import hashlib
import http.client
import json
import re
import threading
import time

from .web_tools import MAX_BYTES, NOTICE, PinnedHTTPSConnection, WebAccessError, plain_text, public_addresses, public_url

ENDPOINT = 'https://mcp.exa.ai/mcp'
HOST = 'mcp.exa.ai'
PROTOCOL = '2025-06-18'
TOOL = 'web_search_exa'


class MCPError(WebAccessError):
    def __init__(self, message, failure_type='mcp_protocol_error'):
        self.failure_type = failure_type
        super().__init__(message)


def rpc_message(value, request_id):
    if not isinstance(value,dict) or value.get('jsonrpc') != '2.0':
        raise MCPError('Search MCP returned invalid JSON-RPC')
    if 'method' in value:
        if 'id' in value:raise MCPError('Search MCP server requests are unsupported; no sampling, roots or execution granted')
        return None  # Bounded server notifications are advisory, never instructions.
    if type(value.get('id')) is not type(request_id) or value['id'] != request_id:raise MCPError('Search MCP response ID mismatch')
    if 'error' in value:raise MCPError('Search MCP returned a JSON-RPC error')
    if not isinstance(value.get('result'),dict):raise MCPError('Search MCP result must be an object')
    return value['result']


def read_message(response, content_type, request_id, check, deadline, sock):
    """Read one bounded JSON/SSE response, not an indefinite background stream."""
    raw = bytearray();event=[];notices=0
    def remaining():
        check()
        left=deadline-time.monotonic()
        if left<=0:raise MCPError('Search MCP response deadline exceeded','mcp_timeout')
        if sock:sock.settimeout(min(5,left))
    def append(chunk):
        raw.extend(chunk)
        if len(raw)>MAX_BYTES:raise MCPError('Search MCP response exceeds 512 KiB','mcp_response_too_large')
    def decode(data):
        try:return rpc_message(json.loads(data),request_id)
        except (ValueError,UnicodeError) as exc:
            if isinstance(exc,MCPError):raise
            raise MCPError('Search MCP returned malformed JSON') from None
    if content_type == 'application/json':
        while True:
            remaining();chunk=response.read1(min(16384,MAX_BYTES+1-len(raw)))
            if not chunk:break
            append(chunk)
        value=decode(bytes(raw))
        if value is None:raise MCPError('Search MCP JSON body did not contain its response')
        return value,bytes(raw)
    if content_type != 'text/event-stream':raise MCPError('Unsupported Search MCP response content type')
    while True:
        remaining();line=response.readline(min(MAX_BYTES+1-len(raw),65537));append(line)
        if len(line)>65536:raise MCPError('Search MCP SSE line exceeds 64 KiB','mcp_response_too_large')
        if not line or not line.strip():
            if event:
                value=decode(b'\n'.join(event));event=[]
                if value is not None:return value,bytes(raw)
                notices+=1
                if notices>32:raise MCPError('Too many Search MCP notifications')
            if not line:raise MCPError('Search MCP stream ended before its response')
        elif line.startswith(b'data:'):
            event.append(line[5:].lstrip(b' '))


def search_hits(result, limit):
    if result.get('isError'):raise MCPError('Search MCP tool reported isError','mcp_tool_error')
    content=result.get('content')
    if not isinstance(content,list):raise MCPError('Search MCP tool did not return content')
    text='\n'.join(row['text'] for row in content if isinstance(row,dict) and row.get('type')=='text' and isinstance(row.get('text'),str))
    hits=[];seen=set()
    for block in re.split(r'\n\s*---\s*\n',text):
        header=re.match(r'\s*Title: ([^\n]+)\nURL: (\S+)\n',block)
        if not header:continue
        try:url=public_url(header[2])
        except WebAccessError:continue
        if url in seen:continue
        published=re.search(r'^Published: ([^\n]+)',block,re.M)
        body=block.partition('Highlights:')[2] or block.partition('Text:')[2]
        hits.append({'title':plain_text(header[1])[:140],'url':url,'snippet':plain_text(body)[:350],
                     'published':published[1][:60] if published and published[1]!='N/A' else '',
                     'sources':['exa_mcp'],'evidence_type':'provider highlights; independently read source for further claims'})
        seen.add(url)
        if len(hits)>=limit:break
    if not hits:
        # Unknown layouts and error prose are not silently classified as empty.
        raise MCPError('Search MCP returned no recognized result records; not proof of no matches','mcp_unrecognized_results')
    return hits


class SearchMCP:
    """One host-owned protocol connection shared only for negotiation, not Goal state."""
    def __init__(self):
        self.lock=threading.RLock();self.ready=False;self.session=None;self.counter=0
        self.schema={};self.server={};self.setup_requests=[]

    def config(self):
        return {'endpoint':ENDPOINT,'tool':TOOL,'protocol':PROTOCOL,'ready':self.ready,
                'server':self.server,'authentication':'anonymous, provider rate limits apply'}

    def _post(self,method,params,deadline,check,on_request,requests,notify=False):
        if method not in {'initialize','notifications/initialized','tools/list','tools/call'}:
            raise MCPError('Search MCP method not allowlisted')
        check();left=deadline-time.monotonic()
        if left<=0:raise MCPError('Search MCP request deadline exceeded','mcp_timeout')
        on_request()
        audit={'url':ENDPOINT,'rpc_method':method,'retrieved_at':datetime.now(timezone.utc).isoformat()}
        requests.append(audit)
        try:addresses=public_addresses(HOST,443)
        except WebAccessError as exc:raise MCPError(str(exc),'mcp_network_rejected') from None
        left=deadline-time.monotonic()
        if left<=0:raise MCPError('Search MCP DNS/request deadline exceeded','mcp_timeout')
        conn=PinnedHTTPSConnection(HOST,addresses[0],min(5,left))
        self.counter+=1;request_id=self.counter
        payload={'jsonrpc':'2.0','method':method}
        if not notify:payload['id']=request_id
        if params is not None:payload['params']=params
        headers={'Content-Type':'application/json','Accept':'application/json, text/event-stream',
                 'Accept-Encoding':'identity','User-Agent':'GoalTree/0.1 (search-mcp client)'}
        if method!='initialize':headers['MCP-Protocol-Version']=PROTOCOL
        if self.session:headers['Mcp-Session-Id']=self.session
        try:
            conn.request('POST','/mcp',body=json.dumps(payload,ensure_ascii=False).encode(),headers=headers)
            left=deadline-time.monotonic()
            if left<=0:raise MCPError('Search MCP request deadline exceeded','mcp_timeout')
            if conn.sock:conn.sock.settimeout(min(5,left))
            response=conn.getresponse();audit['status']=response.status
            if response.status==429:raise MCPError('Search MCP rate limited; no automatic retry','mcp_rate_limited')
            if notify:
                if response.status!=202:raise MCPError('Search MCP notification was not accepted')
                return {}
            if response.status!=200:
                if response.status in {401,403}:raise MCPError('Search MCP requires authentication or denied access','mcp_access_denied')
                raise MCPError('Search MCP returned HTTP '+str(response.status)+'; no redirects or retry','mcp_http_error')
            token=response.getheader('Mcp-Session-Id')
            if method=='initialize' and token:
                if len(token)>4096 or any(not 33<=ord(c)<=126 for c in token):raise MCPError('Invalid Search MCP session header')
                self.session=token  # Session identifiers are never written to audit or UI.
            if response.getheader('Content-Encoding','identity').lower() not in {'','identity'}:
                raise MCPError('Compressed Search MCP response unsupported')
            length=response.getheader('Content-Length')
            if length and (not length.isdecimal() or int(length)>MAX_BYTES):raise MCPError('Search MCP response exceeds 512 KiB','mcp_response_too_large')
            kind=(response.getheader('Content-Type') or '').split(';')[0].strip().lower()
            result,raw=read_message(response,kind,request_id,check,deadline,conn.sock)
            audit.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),hash_scope='consumed response bytes through matching JSON-RPC result')
            return result
        except (OSError,http.client.HTTPException) as exc:
            raise MCPError('Search MCP connection failed or timed out; no automatic retry',
                           'mcp_timeout' if isinstance(exc,TimeoutError) else 'mcp_connection_error') from None
        finally:conn.close()

    def prepare(self,deadline,check=lambda:None,on_request=lambda:None,requests=None):
        requests=[] if requests is None else requests
        if not self.lock.acquire(timeout=max(0,deadline-time.monotonic())):raise MCPError('Search MCP queue deadline exceeded','mcp_timeout')
        try:
            if self.ready:return self.config()
            self.session=None
            init=self._post('initialize',{'protocolVersion':PROTOCOL,'capabilities':{},'clientInfo':{'name':'GoalTree','version':'0.1'}},deadline,check,on_request,requests)
            if (init.get('protocolVersion')!=PROTOCOL or not isinstance(init.get('capabilities'),dict)
                    or 'tools' not in init['capabilities'] or not isinstance(init.get('serverInfo',{}),dict)):
                raise MCPError('Search MCP protocol/capabilities mismatch')
            self._post('notifications/initialized',None,deadline,check,on_request,requests,notify=True)
            listed=self._post('tools/list',{},deadline,check,on_request,requests)
            tools=listed.get('tools',[])
            if not isinstance(tools,list) or len(tools)>100:raise MCPError('Invalid Search MCP tools/list')
            schema=next((t.get('inputSchema') for t in tools if isinstance(t,dict) and t.get('name')==TOOL),None)
            if not isinstance(schema,dict) or not isinstance(schema.get('properties'),dict):raise MCPError('Search MCP advertised an incompatible search schema')
            properties=schema['properties']
            if (schema.get('type')!='object' or not isinstance(properties.get('query'),dict) or properties['query'].get('type')!='string'
                    or not isinstance(schema.get('required',[]),list)
                    or not all(isinstance(k,str) for k in schema.get('required',[]))
                    or any(k not in {'query','numResults','objective'} for k in schema.get('required',[]))
                    or 'objective' in properties and (not isinstance(properties['objective'],dict) or properties['objective'].get('type')!='string')
                    or 'numResults' in properties and (not isinstance(properties['numResults'],dict) or properties['numResults'].get('type') not in {'number','integer'})):
                raise MCPError('Search MCP advertised an incompatible search schema')
            self.schema=schema;self.server={k:str(v)[:160] for k,v in init.get('serverInfo',{}).items() if k in {'name','version'}}
            self.setup_requests=list(requests);self.ready=True
            return self.config()
        finally:self.lock.release()

    def search(self,query,limit,deadline,check,on_request):
        from .web_search import SearchUnavailable
        if not isinstance(query,str) or not 1<=len(query.strip())<=400 or type(limit) is not int or not 1<=limit<=5:
            raise MCPError('Search MCP requires query 1..400 characters and limit 1..5')
        requests=[];started=time.monotonic();raw=None
        try:
            if not self.lock.acquire(timeout=max(0,deadline-time.monotonic())):raise MCPError('Search MCP queue deadline exceeded','mcp_timeout')
            try:
                self.prepare(deadline,check,on_request,requests)
                properties=self.schema['properties'];arguments={'query':query}
                if 'numResults' in properties:arguments['numResults']=limit
                if 'objective' in properties:
                    arguments['objective']='Find authoritative sources answering the query. Prefer official primary pages for the requested date or event. Distinguish older editions and unsupported claims. Return source URLs and concise evidence; do not guess missing details.'
                raw=self._post('tools/call',{'name':TOOL,'arguments':arguments},deadline,check,on_request,requests)
            finally:self.lock.release()
            hits=search_hits(raw,limit)
            attempt={'source':'exa_mcp','status':'ok','count':len(hits),'requests':requests,
                     'elapsed_ms':int((time.monotonic()-started)*1000),'tool':TOOL,'protocol':PROTOCOL}
            return {'untrusted':NOTICE,'source':'mcp','provider':'Exa hosted Search MCP','query':query,'status':'ok',
                    'results':hits,'attempts':[attempt],'mcp_response':raw,'ranking':'provider order; not a correctness score',
                    'limitations':'Provider highlights are not independent full-page verification. Exa is not Bing/Baidu/WeChat and anonymous rate limits apply.'}
        except MCPError as exc:
            attempt={'source':'exa_mcp','status':'error','count':0,'requests':requests,'failure_type':exc.failure_type,'error':str(exc),
                     'elapsed_ms':int((time.monotonic()-started)*1000)}
            raise SearchUnavailable({'untrusted':NOTICE,'source':'mcp','query':query,'status':'error','results':[],
                                     'attempts':[attempt],'mcp_response':raw}) from None
