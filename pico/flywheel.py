"""Local, payload-free process measurements. This module never opens a network connection."""
from collections import Counter
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time

from .persistence import write_json_atomic

MODES = {'off', 'local', 'aggregate'}
TOOLS = {'read_file', 'list_files', 'code_search', 'symbol_info', 'patch_file', 'write_file', 'apply_changes',
         'run_shell', 'load_skill', 'mcp_docs', 'mcp_call', 'web_search', 'web_read', 'web_find', 'goal_action',
         'plan_tasks', 'run_task', 'confirm_task', 'learning_propose', 'recall_evidence', 'find_evidence'}
ERRORS = {'approval_denied', 'policy_hook_denied', 'invalid_arguments', 'path_escape', 'tool_timeout',
          'repeated_tool_call', 'stream_incomplete', 'model_error', 'context_budget', 'persistence_error', 'other'}
STATUSES = {'ok', 'error', 'rejected', 'timeout'}
PRIVATE = re.compile(r'(?i)(?:sk-[\w-]{6,}|ghp_\w+|github_pat_\w+|[\w.+-]+@[\w.-]+\.[a-z]{2,}|https?://|'
                     r'[a-z]:[\\/]|(?:^|\s)/(?:home|users|mnt|tmp)/|\b1[3-9]\d{9}\b|\b\d{17}[\dx]\b|'
                     r'(?:密码|密钥|身份证|手机号|银行卡|未公开|保密|薪资|病历|password|api_key|secret)\s*[:：=]|'
                     r'[￥¥$]\s*\d|\d+(?:\.\d+)?\s*(?:万元|亿元|人民币))')


def privacy_safe(text):
    from .knowledge import KnowledgeStore
    KnowledgeStore.safe_text(text)
    if PRIVATE.search(text):
        raise ValueError('Private content is not eligible for learning')
    return text


class PrivacySettings:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root/'.pico/flywheel/settings.json'

    def read(self):
        self.path.resolve().relative_to(self.root)
        if self.path.is_symlink():
            raise ValueError('Privacy configuration links are unsupported')
        if not self.path.exists():
            return {'mode':'local', 'retention_days':30, 'revision':0}
        if self.path.stat().st_size > 2048:
            raise ValueError('Privacy configuration exceeds limit')
        value = json.loads(self.path.read_text('utf-8'))
        if (not isinstance(value, dict) or set(value) != {'mode','retention_days','revision'} or
            value['mode'] not in MODES or type(value['retention_days']) is not int or
            not 1 <= value['retention_days'] <= 90 or type(value['revision']) is not int or value['revision'] < 0):
            raise ValueError('Invalid privacy configuration')
        return value

    def change(self, mode, days, revision):
        # Operator CLI serializes changes; revisions prevent stale interactive writes.
        current = self.read()
        if current['revision'] != revision:
            raise ValueError('Privacy revision conflict')
        if not isinstance(mode, str) or mode not in MODES or type(days) is not int or not 1 <= days <= 90:
            raise ValueError('Invalid privacy mode/retention')
        value = {'mode':mode,'retention_days':days,'revision':revision+1}
        write_json_atomic(self.path,value)
        return value


class RunMetrics:
    """Keep only counters and ephemeral read fingerprints, never argument values."""
    def __init__(self):
        self.tools=Counter();self.errors=Counter();self.reads=set();self.read_count=0;self.repeats=0
        self.token_known={'input_tokens':0,'output_tokens':0};self.token_unknown=Counter()
        self.models=0;self.retries=0;self.recovered=0;self.failed_tools=set()

    def observe(self, event, data):
        if event == 'tool_executed':
            name=data.get('name');name=name if isinstance(name,str) and name in TOOLS else 'other'
            status=data.get('tool_status');status=status if isinstance(status,str) and status in STATUSES else 'error'
            self.tools[name+':'+status]+=1
            if status!='ok':
                error=data.get('tool_error_code');self.errors[error if isinstance(error,str) and error in ERRORS else 'other']+=1
                self.failed_tools.add(name)
            elif name in self.failed_tools:
                self.recovered+=1;self.failed_tools.remove(name)
            if name=='read_file' and status=='ok':
                args=data.get('args') or {}
                key=hashlib.sha256(json.dumps({'path':args.get('path'),'start':args.get('start',1),
                    'end':args.get('end',200)},sort_keys=True).encode()).digest()
                self.read_count+=1;self.repeats+=key in self.reads;self.reads.add(key)
        elif event in {'model_parsed','model_failed'}:
            self.models+=1
            meta=data.get('completion_metadata') or {}
            for field in self.token_known:
                value=meta.get(field)
                unknown=meta.get('unknown_usage_attempts',0)
                unknown=unknown if type(unknown) is int and 0<=unknown<=10000 else 0
                if type(value) is int and value>=0:
                    self.token_known[field]+=value;self.token_unknown[field]+=unknown
                else:self.token_unknown[field]+=max(1,unknown)
            if event=='model_failed':self.errors['model_error']+=1
        elif event=='model_transport' and data.get('will_retry') is True:self.retries+=1

    def finish(self, state, elapsed_ms, verified):
        return {'status':state.status if state.status in {'completed','failed','stopped'} else 'failed',
                'verified':bool(verified),'tool_steps':int(state.tool_steps),'model_calls':self.models,
                'tools':dict(self.tools),'errors':dict(self.errors),'reads':self.read_count,'repeated_reads':self.repeats,
                'recovered_tools':self.recovered,'transport_retries':self.retries,'elapsed_ms':max(0,int(elapsed_ms)),
                'tokens':{k:{'known':v,'unknown_calls':self.token_unknown[k],
                    'total':None if self.token_unknown[k] else v} for k,v in self.token_known.items()}}


class FlywheelStore:
    def __init__(self, root, clock=time.time):
        self.root=Path(root).resolve();self.settings=PrivacySettings(root);self.clock=clock
        self.path=self.root/'.pico/flywheel/metrics.sqlite3'
        self.path.resolve().relative_to(self.root)
        if self.path.is_symlink():raise ValueError('Metrics database links are unsupported')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('salt',secrets.token_hex(32)))
            owner=hashlib.sha256(os.path.normcase(str(self.root)).encode()).hexdigest()
            db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('owner',owner))
            if db.execute("SELECT value FROM meta WHERE key='owner'").fetchone()[0]!=owner:
                raise ValueError('Metrics belong to another workspace')
            db.execute('CREATE TABLE IF NOT EXISTS runs (key TEXT PRIMARY KEY,at REAL NOT NULL,body TEXT NOT NULL)')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def record(self, run_id, summary):
        if self.settings.read()['mode']=='off':return
        # Reconstruct the accepted shape, never accept arbitrary caller metadata.
        if set(summary) != set(RunMetrics().finish(type('State',(),{'status':'completed','tool_steps':0})(),0,False)):
            raise ValueError('Unexpected metric fields')
        for key in summary['tools']:
            name,sep,status=key.partition(':')
            if not sep or name not in TOOLS|{'other'} or status not in STATUSES:raise ValueError('Unknown tool metric')
        if set(summary['errors'])-ERRORS:raise ValueError('Unknown error metric')
        if summary['status'] not in {'completed','failed','stopped'} or type(summary['verified']) is not bool:
            raise ValueError('Unknown status metric')
        def integer(value):return type(value) is int and 0<=value<=10**12
        if any(not integer(summary[k]) for k in ('tool_steps','model_calls','reads','repeated_reads','recovered_tools','transport_retries','elapsed_ms')):
            raise ValueError('Counters must be bounded non-negative integers')
        if any(not integer(v) for group in ('tools','errors') for v in summary[group].values()):
            raise ValueError('Counter labels cannot contain payloads')
        if not isinstance(summary['tokens'],dict) or set(summary['tokens'])!={'input_tokens','output_tokens'}:
            raise ValueError('Invalid usage metric')
        for token in summary['tokens'].values():
            if (not isinstance(token,dict) or set(token)!={'known','unknown_calls','total'} or
                not integer(token['known']) or not integer(token['unknown_calls']) or
                token['total']!=(None if token['unknown_calls'] else token['known'])):
                raise ValueError('Unknown usage must not be zero or carry metadata')
        if PRIVATE.search(json.dumps(summary)):raise ValueError('Private metric payload rejected')
        with self.connect() as db:
            salt=db.execute("SELECT value FROM meta WHERE key='salt'").fetchone()[0]
            key=hmac.new(salt.encode(),run_id.encode(),hashlib.sha256).hexdigest()
            db.execute('INSERT OR IGNORE INTO runs VALUES (?,?,?)',(key,self.clock(),json.dumps(summary)))
            db.execute('DELETE FROM runs WHERE at<?',(self.clock()-self.settings.read()['retention_days']*86400,))

    def summary(self, *, export=False):
        state=self.settings.read()
        if export and state['mode']!='aggregate':raise ValueError('Aggregate sharing has not been enabled')
        with self.connect() as db:
            rows=[json.loads(r['body']) for r in db.execute('SELECT body FROM runs WHERE at>=?',(self.clock()-state['retention_days']*86400,))]
        errors=Counter();tools=Counter()
        for row in rows:errors.update(row['errors']);tools.update(row['tools'])
        # k suppression is only a reduction of disclosure risk, not an anonymity guarantee.
        threshold=10 if export else 1
        if len(rows)<threshold:return {'schema':1,'suppressed':True,'minimum_runs':threshold}
        return {'schema':1,'suppressed':False,'runs':len(rows),'verified_runs':sum(r['verified'] for r in rows),
                'completed_runs':sum(r['status']=='completed' for r in rows),
                'errors':{k:v for k,v in errors.items() if v>=threshold},
                'tools':{k:v for k,v in tools.items() if v>=threshold},
                'tool_steps':sum(r['tool_steps'] for r in rows),'reads':sum(r['reads'] for r in rows),
                'repeated_reads':sum(r['repeated_reads'] for r in rows),
                'model_calls':sum(r['model_calls'] for r in rows),
                'transport_retries':sum(r['transport_retries'] for r in rows),
                'recovered_tools':sum(r['recovered_tools'] for r in rows),
                'latency_buckets':{label:count for label,count in Counter(
                    next((label for bound,label in ((1000,'le_1s'),(5000,'le_5s'),(30000,'le_30s'),
                        (120000,'le_120s'),(600000,'le_600s')) if row['elapsed_ms']<=bound),'gt_600s') for row in rows).items()
                    if count>=threshold},
                'tokens':{k:{'known':sum(r['tokens'][k]['known'] for r in rows),
                    'unknown_calls':sum(r['tokens'][k]['unknown_calls'] for r in rows)} for k in ('input_tokens','output_tokens')},
                'notice':'Process measurements only; no raw content. Counts are not a formal anonymity guarantee.'}

    def opportunities(self):
        report=self.summary()
        if report.get('suppressed') or report['runs']<3:return []
        candidates=[]
        unknown=sum(v['unknown_calls'] for v in report['tokens'].values())
        if unknown:candidates.append({'case_id':'unknown-usage-v1','signal':'missing_usage','observations':unknown,
            'caution':'Input/output unknown counts may refer to the same request; not independent tasks.'})
        errors=sum(report['errors'].values())
        if errors:candidates.append({'case_id':'bounded-recovery-v1','signal':'tool_or_model_failure','observations':errors,
            'caution':'Failure association only; public reproduction still required.'})
        return candidates

    def clear(self):
        with self.connect() as db:db.execute('DELETE FROM runs')
        return {'metrics_cleared':True,'session_history_deleted':False,'learning_deleted':False}
