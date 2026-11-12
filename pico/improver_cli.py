"""Host controls for low-frequency proposals; absent from Agent tools and web mutation APIs."""
import argparse
import json
import os
from pathlib import Path

from .flywheel import PrivacySettings,FlywheelStore
from .improvement_cases import CASES
from .improver import ImproverStore,MODEL,BASE_URL


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',required=True,type=Path)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('list');sub.add_parser('audit');sub.add_parser('metrics');sub.add_parser('opportunities')
    export=sub.add_parser('export-metrics');export.add_argument('--output',required=True,type=Path)
    privacy=sub.add_parser('privacy');privacy.add_argument('--mode',choices=('off','local','aggregate'))
    privacy.add_argument('--days',type=int,default=30);privacy.add_argument('--revision',type=int)
    clear=sub.add_parser('clear-metrics');clear.add_argument('--confirm',action='store_true',required=True)
    prepare=sub.add_parser('prepare');prepare.add_argument('--case',choices=CASES,required=True)
    show=sub.add_parser('show');show.add_argument('id')
    request=sub.add_parser('request');request.add_argument('id');request.add_argument('--live',action='store_true',required=True)
    request.add_argument('--env-file',type=Path,default=Path(__file__).resolve().parents[1]/'.env.glm.local')
    verify=sub.add_parser('verify');verify.add_argument('id');verify.add_argument('--version',type=int,required=True)
    verify.add_argument('--baseline',type=Path,required=True);verify.add_argument('--candidate',type=Path,required=True)
    verify.add_argument('--output',type=Path,required=True)
    decide=sub.add_parser('decide');decide.add_argument('id');decide.add_argument('--version',type=int,required=True)
    decide.add_argument('--decision',choices=('accept','reject'),required=True);decide.add_argument('--note',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='privacy':
            settings=PrivacySettings(args.workspace)
            result=settings.change(args.mode,args.days,args.revision) if args.mode else settings.read()
        elif args.command in {'metrics','export-metrics','clear-metrics','opportunities'}:
            store=FlywheelStore(args.workspace)
            result=store.clear() if args.command=='clear-metrics' else store.opportunities() if args.command=='opportunities' else store.summary(export=args.command=='export-metrics')
            if args.command=='export-metrics':
                if args.output.exists():raise ValueError('Metrics exports never overwrite existing files')
                args.output.parent.mkdir(parents=True,exist_ok=True)
                args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        else:
            store=ImproverStore(args.workspace)
            if args.command=='prepare':result=store.prepare(args.case)
            elif args.command=='show':result=store.get(args.id)
            elif args.command=='list':result=store.list()
            elif args.command=='audit':result=store.audit_report()
            elif args.command=='decide':result=store.decide(args.id,args.version,args.decision,args.note)
            elif args.command=='verify':
                from .improver_verifier import verify_pair
                row=store.get(args.id)
                if row['version']!=args.version or row['status'] not in {'proposed','verified','verification_failed'}:
                    raise ValueError('Proposal is not ready for verification')
                receipt=verify_pair(args.baseline,args.candidate,args.output,row['body']['packet'])
                result=store.record_verification(args.id,args.version,receipt)
            else:
                from .config import _parse_env_line
                key=os.getenv('BIGMODEL_API_KEY','').strip()
                if not key and args.env_file.is_file():
                    for line in args.env_file.read_text('utf-8-sig').splitlines():
                        parsed=_parse_env_line(line)
                        if parsed and parsed[0]=='BIGMODEL_API_KEY':key=parsed[1].strip()
                if not key:raise ValueError('Configure the local standard BIGMODEL_API_KEY; no provider fallback')
                os.environ['BIGMODEL_API_KEY']=key
                from .providers.clients import OpenAICompatibleModelClient
                client=OpenAICompatibleModelClient(MODEL,BASE_URL,key,1,120,'chat_completions')
                client.max_transport_attempts=1;client.network_route='direct';client.top_p=.95
                client.request_options={'thinking':{'type':'enabled'},'reasoning_effort':'max',
                                        'response_format':{'type':'json_object'}}
                result=store.request(args.id,client,live=True)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 1 if isinstance(result,dict) and (result.get('status')=='failed' or result.get('valid') is False) else 0
    except (ValueError,OSError,KeyError) as exc:
        # No raw endpoint error or env parsing line (which can contain secrets) reaches stdout.
        print(json.dumps({'error_type':type(exc).__name__,'error':'Improver command rejected; inspect arguments, version and local configuration.'}))
        return 2


if __name__=='__main__':raise SystemExit(main())
