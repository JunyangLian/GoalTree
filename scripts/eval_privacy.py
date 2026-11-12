"""Frozen synthetic egress checks against a real local TLS receiver.

No public model, embedding or search requests; never uses a production key.
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from privacy_receiver import receiver
from pico.privacy import PrivacyStore,PrivacyGuard,PrivacyDenied,activate,incoming
from pico.privacy_transport import open_checked


def run(output):
    output=Path(output)
    if output.exists():raise ValueError('Use a new output directory; previous failures are never overwritten')
    output.mkdir(parents=True)
    files=['benchmarks/privacy_v1/cases.json','scripts/eval_privacy.py','tests/privacy_receiver.py',
           'tests/fixtures/privacy/receiver-cert.pem','tests/fixtures/privacy/receiver-key.pem',
           'pico/privacy.py','pico/privacy_runtime.py','pico/privacy_transport.py']
    freeze={f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in files}
    (output/'freeze.json').write_text(json.dumps(freeze,indent=2)+'\n','utf-8')
    cases=json.loads((ROOT/files[0]).read_text('utf-8'))['cases']
    old=os.environ.get('SSL_CERT_FILE');os.environ['SSL_CERT_FILE']=str(ROOT/files[3])
    rows=[];snapshot=[]
    try:
        with receiver() as (url,hits):
            for case in cases:
                case_dir=output/case['id'];case_dir.mkdir()
                store=PrivacyStore(case_dir/'workspace');store.initialize(url)
                policy=store.policy()
                policy['destinations'][0]['purposes']=['model','mcp','embedding','web','search']
                if case.get('unlisted'):policy['destinations']=[]
                store.policy_path.write_text(json.dumps(policy),'utf-8')
                resource='session:'+case['id']
                if case.get('label'):store.label(resource,case['label'])
                if case.get('derived'):
                    store.label('file:private-input.txt','core')
                    store.derive('memory:summary',['file:private-input.txt'])
                    store.derive(resource,['memory:summary'])
                guard=PrivacyGuard(store,[resource]);before=len(hits);started=time.perf_counter()
                destination=url+('/?q='+case['query'] if case.get('query') else '/')
                headers={'Content-Type':'text/plain'}
                if case.get('header'):headers['X-Note']=case['header']
                request=urllib.request.Request(destination,data=case['payload'].encode(),headers=headers,method='POST')
                error=None;rule=None
                try:
                    with activate(guard),open_checked(request,timeout=5,route='direct',purpose=case.get('purpose','model')) as response:
                        incoming(response.read().decode('utf-8'))
                except PrivacyDenied as exc:rule=exc.rule
                except Exception as exc:error=type(exc).__name__
                received=len(hits)>before
                row={'id':case['id'],'expected_allow':case['allow'],'received':received,
                     'passed':received==case['allow'] and error is None,'rule':rule,'error':error,
                     'elapsed_ms':round((time.perf_counter()-started)*1000,3)}
                rows.append(row)
                (case_dir/'result.json').write_text(json.dumps(row,indent=2)+'\n','utf-8')
                with store.connect() as db:audit=[json.loads(r[0]) for r in db.execute('SELECT body FROM audit')]
                (case_dir/'audit.json').write_text(json.dumps(audit,indent=2)+'\n','utf-8')
            snapshot=hits
    finally:
        if old is None:os.environ.pop('SSL_CERT_FILE',None)
        else:os.environ['SSL_CERT_FILE']=old
    (output/'receiver.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2)+'\n','utf-8')
    with (output/'runs.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
    negative=[r for r in rows if not r['expected_allow']];positive=[r for r in rows if r['expected_allow']]
    times=sorted(r['elapsed_ms'] for r in rows)
    summary={'scope':'24 frozen synthetic egress cases; real local TLS receiver, no real model',
        'passed':sum(r['passed'] for r in rows),'cases':len(rows),
        'forbidden_received':sum(r['received'] for r in negative),'forbidden_denominator':len(negative),
        'benign_blocked':sum(not r['received'] for r in positive),'benign_denominator':len(positive),
        'p50_ms':times[math.ceil(.5*len(times))-1],'p95_ms':times[math.ceil(.95*len(times))-1],
        'latency_scope':'full synthetic dispatch/block path incl local TLS, not pure DLP overhead or production latency',
        'receiver_requests':len(snapshot),'public_network_requests':0,'model_calls':0,
        'source_unchanged':all(hashlib.sha256((ROOT/f).read_bytes()).hexdigest()==h for f,h in freeze.items())}
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n','utf-8')
    print(json.dumps(summary));return 0 if summary['passed']==len(rows) and summary['source_unchanged'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    raise SystemExit(run(parser.parse_args().output))
