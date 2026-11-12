"""Operator-only privacy controls, never registered as Agent tools."""
import argparse
import json
from pathlib import Path
from .privacy import PrivacyStore, LEVELS, PURPOSES, origin, PrivacyDenied
from .persistence import write_json_atomic


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    commands=parser.add_subparsers(dest='command',required=True)
    init=commands.add_parser('init')
    init.add_argument('--model-url',required=True);init.add_argument('--web',action='store_true')
    init.add_argument('--mode',choices=('enforce','observe'),default='enforce')
    label=commands.add_parser('label');label.add_argument('resource');label.add_argument('level',choices=LEVELS)
    allow=commands.add_parser('allow');allow.add_argument('url')
    allow.add_argument('--purpose',choices=sorted(PURPOSES),required=True)
    allow.add_argument('--max-level',choices=('public','general','sensitive'),default='general')
    commands.add_parser('status');commands.add_parser('audit')
    args=parser.parse_args(argv);store=PrivacyStore(args.workspace)
    try:
        if args.command=='init':store.initialize(args.model_url,args.web,args.mode)
        if not store.enabled:raise PrivacyDenied('privacy_not_initialized')
        if args.command=='label':store.label(args.resource,args.level)
        if args.command=='allow':
            policy=store.policy();url=origin(args.url)
            if not url.startswith('https://'):raise ValueError('External destinations require HTTPS')
            if any(d['origin']==url and args.purpose in d['purposes'] for d in policy['destinations']):
                raise ValueError('Destination already configured; edit host policy explicitly to change its clearance')
            if len(policy['destinations'])>=128:raise ValueError('Destination limit reached')
            policy['destinations'].append({'origin':url,'purposes':[args.purpose],'max_level':args.max_level,'local':False})
            write_json_atomic(store.policy_path,policy);store.policy()
        policy=store.policy()
        if args.command=='audit':
            with store.connect() as db:
                rows=[{'seq':seq,'at':at,**json.loads(body)} for seq,at,body in db.execute('SELECT seq,at,body FROM audit ORDER BY seq DESC LIMIT 100')]
            result={'events':list(reversed(rows))}
        else:
            with store.connect() as db:labels=[dict(resource=r,level=l) for r,l in db.execute('SELECT resource,level FROM labels ORDER BY resource')]
            result={'enabled':True,'mode':policy['mode'],'destinations':policy['destinations'],'labels':labels,
                    'scope':'Host policy only; no semantic secrecy guarantee; no private model fallback'}
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (ValueError,OSError) as exc:parser.exit(2,str(exc)+'\n')


if __name__=='__main__':raise SystemExit(main())
