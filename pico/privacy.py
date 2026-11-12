"""Host-owned disclosure policy and conservative, persistent provenance.

Not a semantic secrecy oracle. Labels never downgrade through summarization.
Policy/labels are operator controls, not tools exposed to the model.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from collections import defaultdict
import base64
from urllib.parse import unquote, urlsplit

from .persistence import write_json_atomic

LEVELS = ('public', 'general', 'sensitive', 'core')
PURPOSES = {'model', 'embedding', 'search', 'mcp', 'web'}
CURRENT = ContextVar('goaltree_privacy', default=None)
SEARCH_ORIGINS = ('https://open.bigmodel.cn', 'https://www.bing.com', 'https://cn.bing.com',
                  'https://www.baidu.com', 'https://weixin.sogou.com', 'https://lite.duckduckgo.com',
                  'https://api.crossref.org', 'https://eutils.ncbi.nlm.nih.gov', 'https://mcp.exa.ai')
MCP_ORIGINS = ('https://mcp.context7.com', 'https://api.githubcopilot.com',
               'https://learn.microsoft.com', 'https://mcp.deepwiki.com')


class PrivacyDenied(ValueError):
    def __init__(self, rule):
        self.rule = rule
        super().__init__('Privacy policy blocked operation: ' + rule + '; no automatic external fallback')


def maximum(*levels):
    return max(levels or ('public',), key=LEVELS.index)


def origin(url):
    try:
        p = urlsplit(url)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        host = p.hostname.encode('idna').decode('ascii').lower()
        port = p.port or (443 if p.scheme == 'https' else 80)
        if any(c in host for c in ('%', '\\')):
            raise ValueError()
        authority = '[' + host + ']' if ':' in host else host
        return p.scheme + '://' + authority + ((':' + str(port)) if port != (443 if p.scheme == 'https' else 80) else '')
    except (ValueError, UnicodeError, TypeError):
        raise PrivacyDenied('invalid_destination') from None


def loopback(url):
    try:
        return ipaddress.ip_address(urlsplit(url).hostname).is_loopback
    except ValueError:
        return False  # "localhost" and private DNS names are not proof of isolation.


def inspect_text(value):
    """Bounded deterministic detector; returns types/level, never matched values."""
    if not isinstance(value, str) or len(value) > 16_777_216:
        raise PrivacyDenied('uninspectable_payload')
    views = [value]
    for _ in range(2):
        decoded = unquote(views[-1])
        if decoded == views[-1]:
            break
        views.append(decoded)
    # Inspect JSON embedded in tool argument strings and small obvious encodings.
    # This is bounded normalization, not a promise to decode arbitrary obfuscation.
    for text in list(views):
        try:parsed=json.loads(text)
        except (ValueError,TypeError):continue
        pending=[(parsed,0)]
        while pending:
            item,depth=pending.pop()
            if depth>4:continue
            if isinstance(item,dict):pending.extend((v,depth+1) for v in item.values())
            elif isinstance(item,list):pending.extend((v,depth+1) for v in item)
            elif isinstance(item,str):
                views.append(item)
                try:nested=json.loads(item)
                except (ValueError,TypeError):continue
                if nested!=item:pending.append((nested,depth+1))
            if len(views)>10000:raise PrivacyDenied('inspection_limit_exceeded')
    for token in re.findall(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{24,4096}={0,2}(?![A-Za-z0-9+/])',value)[:32]:
        try:
            decoded=base64.b64decode(token,validate=True).decode('utf-8')
            if decoded.isprintable():views.append(decoded)
        except (ValueError,UnicodeError):pass
    level, rules = 'public', set()
    for text in views:
        if ('-----BEGIN ' in text and 'PRIVATE KEY-----' in text or
                re.search(r'\b(?:sk-[\w-]{8,}|ghp_[\w]{12,}|github_pat_[\w]{12,}|GT_CORE_[A-Z0-9]{12,})\b', text)):
            level = 'core'; rules.add('credential_pattern')
        if any(len(v) >= 8 and v in text for k, v in os.environ.items()
               if any(m in k.upper() for m in ('API_KEY', 'PASSWORD', 'SECRET', 'TOKEN'))):
            level = 'core'; rules.add('known_credential')
        if re.search(r'(?i)(?:password|密码|密钥)\s*[:=：]\s*[\w!@#$%^&*.-]{8,}', text):
            level = 'core'; rules.add('credential_assignment')
        # Search around actual @ signs rather than repeatedly scanning an
        # arbitrarily long word for a nonexistent @ (quadratic on long input).
        email = False
        for index, match in enumerate(re.finditer('@', text)):
            if index >= 10000:
                raise PrivacyDenied('inspection_limit_exceeded')
            at = match.start()
            if re.search(r'[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}', text[max(0, at-256):at+320]):
                email = True
                break
        if email or re.search(r'\bGT_SENSITIVE_[A-Z0-9]{12,}\b|\b1[3-9]\d{9}\b|\b\d{17}[\dXx]\b', text):
            level = maximum(level, 'sensitive'); rules.add('personal_data_pattern')
        if re.search(r'data:(?:image|application)/[^;]+;base64,', text, re.I):
            level = maximum(level, 'sensitive'); rules.add('uninspected_binary')
    return level, sorted(rules)


class PrivacyStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / '.pico/privacy'
        self.policy_path = self.directory / 'policy.json'
        self.database = self.directory / 'state.sqlite3'

    def safe(self):
        # Reject links/reparse points for operator controls and audit stores.
        for p in (self.root / '.pico', self.directory, self.policy_path, self.database):
            if p.exists() or p.is_symlink():
                if (p.is_symlink() or getattr(p.lstat(), 'st_file_attributes', 0) & 0x400 or
                        p.is_file() and p.stat().st_nlink>1):
                    raise PrivacyDenied('linked_privacy_store')
                if not p.resolve().is_relative_to(self.root):
                    raise PrivacyDenied('escaped_privacy_store')

    @property
    def enabled(self):
        self.safe()
        return self.policy_path.exists() or self.database.exists()

    def initialize(self, model_url=None, web=False, mode='enforce'):
        self.safe()
        if self.enabled:
            return self.policy()
        if mode not in {'enforce', 'observe'}:
            raise ValueError('Invalid privacy mode')
        self.directory.mkdir(parents=True, exist_ok=True)
        destinations = []
        if model_url:
            destinations.append({'origin': origin(model_url), 'purposes': ['model'],
                                 'max_level': 'core' if loopback(model_url) else 'general',
                                 'local': loopback(model_url)})
        if web:
            destinations += [{'origin': u, 'purposes': ['search', 'web'], 'max_level': 'general', 'local': False}
                             for u in SEARCH_ORIGINS]
        body = {'version': 1, 'mode': mode, 'destinations': destinations, 'retention_days': 30,
                'key': secrets.token_hex(32)}
        write_json_atomic(self.policy_path, body)
        self.policy()
        with self.connect():
            pass
        return body

    def policy(self):
        self.safe()
        try:
            if self.policy_path.stat().st_size > 65536:
                raise ValueError()
            p = json.loads(self.policy_path.read_text('utf-8'))
            if (set(p) != {'version','mode','destinations','retention_days','key'} or p['version'] != 1 or
                    p['mode'] not in {'enforce','observe'} or type(p['retention_days']) is not int or
                    not 1 <= p['retention_days'] <= 90 or not re.fullmatch('[0-9a-f]{64}', p['key']) or
                    not isinstance(p['destinations'], list) or len(p['destinations']) > 128):
                raise ValueError()
            for d in p['destinations']:
                if (set(d) != {'origin','purposes','max_level','local'} or origin(d['origin']) != d['origin'] or
                    not isinstance(d['purposes'],list) or not d['purposes'] or
                    not set(d['purposes']) <= PURPOSES or d['max_level'] not in LEVELS or type(d['local']) is not bool or
                    d['local'] and not loopback(d['origin']) or d['max_level'] == 'core' and not d['local'] or
                    not d['local'] and not d['origin'].startswith('https://')):
                    raise ValueError()
            return p
        except (OSError, ValueError, TypeError, KeyError):
            raise PrivacyDenied('invalid_or_missing_policy') from None

    @contextmanager
    def connect(self):
        self.safe()
        self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.database, timeout=10)
        try:
            with db:
                db.executescript('''CREATE TABLE IF NOT EXISTS labels(resource TEXT PRIMARY KEY, level TEXT NOT NULL);
                  CREATE TABLE IF NOT EXISTS edges(target TEXT, source TEXT, PRIMARY KEY(target,source));
                  CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, body TEXT);''')
                yield db
        except sqlite3.Error:
            raise PrivacyDenied('privacy_state_unavailable') from None
        finally:
            db.close()

    def resource(self, resource):
        if not isinstance(resource, str) or not 1 <= len(resource) <= 2048 or ':' not in resource:
            raise ValueError('Use file:, goal:, task:, session:, memory:, evidence: or artifact: resources')
        kind, value = resource.split(':', 1)
        if kind not in {'file','goal','task','session','memory','evidence','artifact'} or not value.strip():
            raise ValueError('Unsupported privacy resource')
        if kind == 'file':
            p = (self.root / value).resolve()
            if not p.is_relative_to(self.root):
                raise ValueError('Privacy resource escaped workspace')
            resource = 'file:' + p.relative_to(self.root).as_posix().casefold()
        return resource

    def level(self, *resources):
        with self.connect() as db:
            labels=dict(db.execute('SELECT resource,level FROM labels'))
            if any(l not in LEVELS for l in labels.values()):raise PrivacyDenied('invalid_label')
            edges=defaultdict(list)
            for target,source in db.execute('SELECT target,source FROM edges'):edges[target].append(source)
        pending=[self.resource(r) for r in resources];seen=set();result='public'
        while pending:
            key=pending.pop()
            if key in seen:continue
            seen.add(key)
            if len(seen)>10000:raise PrivacyDenied('lineage_limit_exceeded')
            result=maximum(result,labels.get(key,'public'))
            if key.startswith('file:'):
                for name,label in labels.items():
                    if name.startswith('file:') and (name=='file:.' or key[5:].startswith(name[5:].rstrip('/')+'/')):
                        result=maximum(result,label)
            pending.extend(edges[key])
        return result

    def label(self, resource, level):
        key = self.resource(resource)
        if level not in LEVELS:
            raise ValueError('Unknown privacy level')
        # Monotonic in v1: new consent cannot silently clear prior lineage.
        if LEVELS.index(level) < LEVELS.index(self.level(key)):
            raise ValueError('Privacy labels cannot be downgraded in v1')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO labels VALUES (?,?)', (key,level))

    def derive(self, target, sources):
        target = self.resource(target)
        sources = [self.resource(s) for s in sources if s != target]
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO edges VALUES (?,?)', [(target,s) for s in sources])

    def audit(self, record, policy):
        # Strict allowlist, no prompt, query, path, matched span or content hash.
        allowed = {'purpose','destination_id','level','decision','rule','bytes','elapsed_ms','mode'}
        if set(record) != allowed:
            raise PrivacyDenied('invalid_audit_record')
        with self.connect() as db:
            db.execute('DELETE FROM audit WHERE at < ?', (time.time()-policy['retention_days']*86400,))
            db.execute('INSERT INTO audit(at,body) VALUES (?,?)', (time.time(),json.dumps(record)))


class PrivacyGuard:
    def __init__(self, store, resources=()):
        self.store = store
        self.resources = tuple(store.resource(r) for r in resources)
        self.start_policy = store.policy()
        self.policy_hash = hashlib.sha256(json.dumps(self.start_policy,sort_keys=True).encode()).hexdigest()

    def policy(self):
        p = self.store.policy()
        if hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest() != self.policy_hash:
            raise PrivacyDenied('policy_changed_during_run')
        return p

    @property
    def level(self):
        return maximum('general', self.store.level(*self.resources))

    def observe(self, sources=(), text='', targets=()):
        level, _ = inspect_text(text)
        for resource in self.resources:
            self.store.derive(resource, list(sources))
            if LEVELS.index(level) > LEVELS.index(self.store.level(resource)):
                self.store.label(resource, level)
        for target in targets:
            self.store.derive(target, list(self.resources) + list(sources))

    def check(self, url, body='', headers=None, purpose='model', credential_headers=()):
        started = time.perf_counter()
        p = self.policy()
        dest = origin(url)
        if purpose not in PURPOSES:
            raise PrivacyDenied('unknown_purpose')
        if isinstance(body, bytes):
            try:body = body.decode('utf-8')
            except UnicodeError:raise PrivacyDenied('uninspectable_payload') from None
        if not isinstance(body, str):
            raise PrivacyDenied('uninspectable_payload')
        try:
            body = json.dumps(json.loads(body),ensure_ascii=False) if body else body
        except (ValueError, TypeError):
            pass
        header_text = json.dumps({k:v for k,v in (headers or {}).items()
                                 if k.casefold() not in {n.casefold() for n in credential_headers}}, ensure_ascii=False)
        detected, rules = inspect_text(url + '\n' + header_text + '\n' + body)
        level = maximum(self.level, detected)
        destinations = [d for d in p['destinations'] if d['origin'] == dest and purpose in d['purposes']]
        target = destinations[0] if destinations else None
        rule = 'allowed'
        if 'uninspected_binary' in rules:
            rule = 'uninspected_binary'
        elif not target:
            rule = 'destination_not_allowed'
        elif level == 'core' and not target['local']:
            rule = 'core_external_forbidden'
        elif LEVELS.index(level) > LEVELS.index(target['max_level']):
            rule = 'destination_clearance_exceeded'
        # Shadow evaluation never weakens known core, unknown destination or binary boundaries.
        hard = rule in {'core_external_forbidden','destination_not_allowed','uninspected_binary'} or level == 'core'
        deny = rule != 'allowed' and (p['mode'] == 'enforce' or hard)
        record = dict(purpose=purpose,destination_id=hmac.new(bytes.fromhex(p['key']),dest.encode(),hashlib.sha256).hexdigest()[:20],
                      level=level,decision='deny' if deny else 'would_deny' if rule!='allowed' else 'allow',rule=rule,
                      bytes=len(body.encode()),elapsed_ms=round((time.perf_counter()-started)*1000,3),mode=p['mode'])
        self.store.audit(record,p)  # Audit failure prevents dispatch too.
        if deny:raise PrivacyDenied(rule)
        return record

    def incoming(self, text):
        try:inspected=json.dumps(json.loads(text),ensure_ascii=False)
        except (ValueError,TypeError):inspected=text
        _, rules = inspect_text(inspected)
        if any(r in rules for r in ('known_credential','credential_pattern','credential_assignment')):
            p=self.policy()
            self.store.audit(dict(purpose='model',destination_id='response',level='core',decision='deny',
                rule='credential_in_response',bytes=len(text.encode()),elapsed_ms=0,mode=p['mode']),p)
            raise PrivacyDenied('credential_in_response')
        self.observe(text=inspected)
        return text


@contextmanager
def activate(guard):
    token = CURRENT.set(guard)
    try:yield guard
    finally:CURRENT.reset(token)


def outbound(url, body='', headers=None, purpose='model', credential_headers=()):
    guard = CURRENT.get()
    return guard.check(url,body,headers,purpose,credential_headers) if guard else None


def incoming(text):
    guard = CURRENT.get()
    return guard.incoming(text) if guard else text


def scrub(value, *, force=False):
    """Known credential shapes are not retained in local transcripts either."""
    if not CURRENT.get() and not force:return value
    # One fresh snapshot per artifact, not one environment traversal per key
    # and value. Never cache across calls: a rotated key must be masked at once.
    secrets = tuple(v for k,v in os.environ.items() if len(v)>=8 and
                    any(m in k.upper() for m in ('API_KEY','PASSWORD','SECRET','TOKEN')))
    def visit(item):
        if isinstance(item,dict):
            return {visit(k):'<redacted>' if re.search(r'(?i)(?:api[_-]?key|password|secret)$',str(k)) else visit(v)
                    for k,v in item.items()}
        if isinstance(item,list):return [visit(v) for v in item]
        if isinstance(item,tuple):return tuple(visit(v) for v in item)
        if isinstance(item,str):
            for secret in secrets:item=item.replace(secret,'<redacted>')
            item=re.sub(r'\b(?:sk-[\w-]{8,}|ghp_[\w]{12,}|github_pat_[\w]{12,}|GT_CORE_[A-Z0-9]{12,})\b','<redacted>',item)
            item=re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----','<redacted>',item,flags=re.S)
            item=re.sub(r'(?i)((?:password|密码|密钥)\s*[:=：]\s*)[\w!@#$%^&*.-]{8,}',r'\1<redacted>',item)
        return item
    return visit(value)


def inherited_worker(fn):
    """Thread pools do not inherit contextvars; bind the same scoped guard explicitly."""
    guard = CURRENT.get()
    def run(*args, **kwargs):
        with activate(guard):return fn(*args, **kwargs)
    return run
