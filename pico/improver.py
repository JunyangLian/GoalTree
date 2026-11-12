"""Operator-only low-frequency proposals. No model tools, live patching or implicit upload."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import time
import uuid

from .improvement_cases import encoded, digest, public_packet
from .knowledge import KnowledgeStore

MODEL='glm-5.3'
BASE_URL='https://open.bigmodel.cn/api/paas/v4'
MAX_OUTPUT=4096
REQUEST_LIMIT=2  # per UTC day/workspace, includes errors and unknown billed attempts
PROMPT = '''You are GoalTree's Improver. Analyze only this public synthetic evidence packet.
Return ONE JSON object, no markdown fence, with exactly these fields:
title:string, hypothesis:string, changes:array of 1..6 strings, targets:array of file paths selected from case.targets,
validation:array of 1..6 strings, risks:array of 1..6 strings, rollback:string, evidence_case:string (the supplied case_id).
Do not claim a patch was applied or tests passed. Do not emit executable patches, commands or references to private user data.
If evidence is insufficient, explain that in hypothesis. Do not change permissions or acceptance standards.
Each string <=1600 characters. Evidence packet:\n'''


def validate_proposal(value, packet):
    fields={'title','hypothesis','changes','targets','validation','risks','rollback','evidence_case'}
    if not isinstance(value,dict) or set(value)!=fields:raise ValueError('Improver returned invalid proposal fields')
    for key in ('title','hypothesis','rollback','evidence_case'):
        KnowledgeStore.safe_text(value[key])
        if len(value[key])>1600:raise ValueError('Proposal exceeds text bound')
    for key in ('changes','targets','validation','risks'):
        if not isinstance(value[key],list) or not 1<=len(value[key])<=6:raise ValueError('Proposal list exceeds bound')
        for text in value[key]:
            KnowledgeStore.safe_text(text)
            if len(text)>1600:raise ValueError('Proposal item exceeds text bound')
    if set(value['targets'])-set(packet['case']['targets']):raise ValueError('Improver target is outside this case')
    if value['evidence_case']!=packet['case_id']:raise ValueError('Proposal cites a different case')
    return value


class ImproverStore:
    def __init__(self, workspace):
        self.root=Path(workspace).resolve();self.path=self.root/'.pico/improver/proposals.sqlite3'
        self.path.resolve().relative_to(self.root)
        if self.path.is_symlink():raise ValueError('Improver database links are unsupported')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY,version INTEGER NOT NULL,status TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY,day TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY,body TEXT NOT NULL,sha256 TEXT NOT NULL);''')
            db.execute('CREATE TABLE IF NOT EXISTS owner (id TEXT PRIMARY KEY)')
            owner=digest(os.path.normcase(str(self.root)))
            db.execute('INSERT INTO owner SELECT ? WHERE NOT EXISTS (SELECT 1 FROM owner)',(owner,))
            if db.execute('SELECT id FROM owner').fetchone()[0]!=owner:
                raise ValueError('Improver records belong to another workspace')

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path,timeout=5);db.row_factory=sqlite3.Row
        try:
            with db:yield db
        finally:db.close()

    def audit(self,db,kind,details):
        last=db.execute('SELECT sha256 FROM audit ORDER BY seq DESC LIMIT 1').fetchone()
        value={'kind':kind,'at':time.time(),'details':details,'previous':last[0] if last else ''}
        db.execute('INSERT INTO audit(body,sha256) VALUES (?,?)',(encoded(value),digest(value)))

    def prepare(self,case_id):
        packet=public_packet(case_id);key='imp_'+uuid.uuid4().hex
        body={'packet':packet,'packet_sha256':digest(packet),'model':MODEL,'max_output_tokens':MAX_OUTPUT,
              'thinking':'enabled','reasoning_effort':'max','transport_attempts':1,'timeout_seconds':120,
              'proposal':None,'verification':None,'decision':None}
        with self.connect() as db:
            db.execute('INSERT INTO proposals VALUES (?,?,?,?)',(key,1,'prepared',encoded(body)))
            self.audit(db,'prepared',{'id':key,'packet_sha256':digest(packet)})
        return self.get(key)

    def get(self,key):
        with self.connect() as db:row=db.execute('SELECT * FROM proposals WHERE id=?',(key,)).fetchone()
        if row is None:raise ValueError('Unknown improvement proposal')
        return {**dict(row),'body':json.loads(row['body'])}

    def list(self):
        with self.connect() as db:rows=db.execute('SELECT id,version,status FROM proposals ORDER BY rowid DESC LIMIT 50').fetchall()
        return [dict(r) for r in rows]

    def transition(self,key,version,status,body,kind):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('UPDATE proposals SET version=version+1,status=?,body=? WHERE id=? AND version=?',
                          (status,encoded(body),key,version)).rowcount!=1:raise ValueError('Improver revision conflict')
            self.audit(db,kind,{'id':key,'version':version+1,'body_sha256':digest(body)})
        return self.get(key)

    def request(self,key,client, *, live=False):
        row=self.get(key);body=row['body'];packet=body['packet']
        if row['status']!='prepared':raise ValueError('A prepared packet can be dispatched only once; failures remain preserved')
        if packet!=public_packet(packet['case_id']) or digest(packet)!=body['packet_sha256']:
            raise ValueError('Public evidence changed after freeze')
        if live and (getattr(client,'model',None)!=MODEL or getattr(client,'base_url','')!=BASE_URL):
            raise ValueError('Improver requires the explicit standard BigModel GLM-5.3 endpoint')
        prompt=PROMPT+encoded(packet)
        if len(prompt.encode())>16000:raise ValueError('Improver input exceeds 16 KiB')
        day=datetime.now(timezone.utc).date().isoformat();call='call_'+uuid.uuid4().hex
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT version,status FROM proposals WHERE id=?',(key,)).fetchone()
            if current['version']!=row['version'] or current['status']!='prepared':raise ValueError('Improver revision conflict')
            if live and db.execute("SELECT count(*) FROM calls WHERE day=? AND json_extract(body,'$.live')=1",(day,)).fetchone()[0]>=REQUEST_LIMIT:
                raise ValueError('Daily live Improver request limit reached')
            reservation={'live':live,'model':MODEL if live else 'offline_fixture','input_bytes':len(prompt.encode()),
                         'max_output_tokens':MAX_OUTPUT,'prompt_sha256':digest(prompt),'usage':None,'cost':None,
                         'proposal_id':key,'started_at':time.time()}
            db.execute('INSERT INTO calls VALUES (?,?,?,?)',(call,day,'started',encoded(reservation)))
            body['call_id']=call
            db.execute("UPDATE proposals SET status='requesting',version=version+1,body=? WHERE id=?",(encoded(body),key))
            self.audit(db,'dispatch_reserved',{'id':key,'call':call,'live':live,'prompt_sha256':digest(prompt)})
        started=time.monotonic();body['call_id']=call
        stage='provider_request'
        try:
            from .privacy import PrivacyStore,PrivacyGuard,activate
            privacy=PrivacyStore(self.root)
            guard=PrivacyGuard(privacy,['artifact:public-improver-packet']) if privacy.enabled else None
            client.privacy_root=self.root
            with activate(guard):raw=client.complete(prompt,MAX_OUTPUT)
            stage='response_validation'
            metadata=getattr(client,'last_completion_metadata',{}) or {}
            body['usage']={k:metadata.get(k) if type(metadata.get(k)) is int and metadata[k]>=0 else None
                           for k in ('input_tokens','output_tokens')}
            if live and (metadata.get('provider_model')!=MODEL or metadata.get('provider_finish_reason')!='stop'):
                raise ValueError('Model identity/complete response not confirmed; no fallback or truncated proposal accepted')
            if not isinstance(raw,str) or len(raw.encode())>24000:raise ValueError('Improver output exceeds limit')
            body['proposal']=validate_proposal(json.loads(raw),packet)
            status='proposed';body['error']=None
        except Exception as exc:
            status='failed';body['error']={'type':type(exc).__name__,'stage':stage,
                'message':'Request or schema validation failed; raw provider error was not copied.'}
            body.setdefault('usage',{'input_tokens':None,'output_tokens':None})
        metadata=getattr(client,'last_completion_metadata',{}) or {}
        body['transport']=[{'ok':e.get('ok') is True,'status_code':e.get('status_code') if type(e.get('status_code')) is int else None,
            'phase':e.get('phase') if e.get('phase') in {'connect_or_response_headers','response_body'} else 'unknown'}
            for e in (metadata.get('transport_events') or [])[:3] if isinstance(e,dict)]
        body['live']=live;body['elapsed_ms']=int((time.monotonic()-started)*1000)
        body['executed_model']=MODEL if live and metadata.get('provider_model')==MODEL else 'offline_fixture' if not live else None
        with self.connect() as db:
            reservation['usage']=body['usage'];reservation['elapsed_ms']=body['elapsed_ms']
            db.execute('UPDATE calls SET state=?,body=? WHERE id=?',(status,encoded(reservation),call))
        return self.transition(key,row['version']+1,status,body,'proposal_'+status)

    def record_verification(self,key,version,receipt):
        row=self.get(key)
        if row['version']!=version or row['status'] not in {'proposed','verified','verification_failed'}:raise ValueError('Proposal is not awaiting verification')
        from .improver_verifier import validate_receipt
        validate_receipt(receipt,row['body']['packet'])
        body=row['body'];body['verification']=receipt
        unexpected=set(receipt['changed_files'])-set(body['proposal']['targets'])
        if unexpected:
            raise ValueError('Verified changes exceed the model proposal target scope')
        status='verified' if receipt['candidate']['passed'] and not receipt['forbidden_changes'] else 'verification_failed'
        return self.transition(key,version,status,body,'external_verification')

    def decide(self,key,version,decision,note):
        row=self.get(key)
        if decision not in {'accept','reject'} or row['version']!=version or row['status'] not in {'proposed','verified','verification_failed'}:
            raise ValueError('Invalid or stale operator decision')
        KnowledgeStore.safe_text(note)
        body=row['body']
        if decision=='accept' and (row['status']!='verified' or not body['verification']['changed_files']):
            raise ValueError('Accept requires a verified changed candidate; model opinion is not evidence')
        if decision=='accept':
            from .improver_verifier import validate_receipt
            validate_receipt(body['verification'],body['packet'])
        body['decision']={'decision':decision,'note':note,'actor':'operator_cli','at':time.time(),
                          'candidate_sha256':(body.get('verification') or {}).get('candidate_sha256')}
        # Acceptance records a decision only: deployment/merge is deliberately separate.
        return self.transition(key,version,'accepted' if decision=='accept' else 'rejected',body,'operator_decision')

    def audit_report(self):
        previous='';valid=True;count=0
        with self.connect() as db:
            for row in db.execute('SELECT * FROM audit ORDER BY seq'):
                value=json.loads(row['body']);valid &= value['previous']==previous and digest(value)==row['sha256']
                previous=row['sha256'];count+=1
            unfinished=db.execute("SELECT count(*) FROM calls WHERE state='started'").fetchone()[0]
        return {'valid':bool(valid),'events':count,'head':previous,'unfinished_calls':unfinished,
                'scope':'local consistency, not externally anchored tamper-proof storage'}
