"""Offline integration of real pinned ApeCode loop/tools; not a model benchmark."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from pico.evaluation.cross_agent import snapshot,verify_candidate
from pico.evaluation.repair_suite import install,utcnow,write_json
from scripts.run_cross_agent_pilot import source_info,prompt
from scripts.apecode_pilot_adapter import run_apecode
from pico.windows_sandbox import VERIFY


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--apecode-source',type=Path,default=ROOT/'artifacts/cross-agent-sources/apecode')
    args=parser.parse_args();out=args.output.resolve();source=args.apecode_source.resolve()
    if sys.version_info<(3,13):parser.error('Use Python >=3.13 as required by pinned upstream')
    out.mkdir(parents=True,exist_ok=False)
    files={'code.py':'value = 1\n','test_acceptance.py':
        'import unittest\nfrom code import value\nclass Acceptance(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value, 2)\n',
        'README.md':'Offline adapter fixture. Not a formal benchmark task.\n'}
    task={'id':'offline_adapter_fixture','files':files,'editable_files':['code.py'],'expected_tests':1,
          'sha256':{n:hashlib.sha256(v.encode()).hexdigest() for n,v in files.items()},'prompt':'Fix value in code.py. Run tests before and after.'}
    workspace=out/'workspace';install(workspace,files);directory=out/'adapter';directory.mkdir()
    write_json(out/'external-agent.json',{'source':str(source),'source_hashes':source_info(source)})
    write_json(out/'freeze.json',{'at':utcnow(),'scope':'Non-task offline integration; decisions scripted; no API calls',
        'fixture_sha256':task['sha256']})
    write_json(out/'prompts.json',{task['id']:prompt(task)})
    class ScriptedClient:
        def __init__(self,directory):self.requests=[];self.index=0
        def complete(self,*,messages,tools):
            assert {'exec_command','read_file','replace_in_file'}<={t['function']['name'] for t in tools}
            actions=[('exec_command',{'command':'python -c "print(123)"'}),
                     ('write_file',{'path':'test_acceptance.py','content':'forged'}),
                     ('exec_command',{'command':VERIFY}),('read_file',{'path':'code.py'}),
                     ('replace_in_file',{'path':'code.py','old':'value = 1','new':'value = 2'}),
                     ('exec_command',{'command':VERIFY})]
            i=self.index;self.index+=1
            self.requests.append({'input_tokens':None,'output_tokens':None,'complete':False})
            if i==len(actions):return {'role':'assistant','content':'Offline scripted integration complete.'}
            name,args=actions[i]
            return {'role':'assistant','content':None,'tool_calls':[{'id':f'fixture-{i}','type':'function',
                'function':{'name':name,'arguments':json.dumps(args)}}]}
    result=run_apecode(out,task,workspace,directory,ScriptedClient)
    independent=verify_candidate(task,{'code.py':(workspace/'code.py').read_bytes()},out/'external')
    checks={'original_loop_completed':result['completed'],'baseline_test_observed':result['baseline_test_observed'],
        'current_final_test_observed':result['final_test_observed'],'external_acceptance':independent['accepted'],
        'frozen_tests_unchanged':snapshot(workspace)['test_acceptance.py']==task['sha256']['test_acceptance.py'],
        'shell_and_test_write_denied':result['security_denials']==2,
        'no_fake_token_usage':result['input_tokens'] is None and result['output_tokens'] is None}
    write_json(out/'result.json',{'scope':'offline scripted adapter integration, not live model score','api_calls':0,
        'checks':checks,'passed':sum(checks.values()),'total':len(checks),'adapter':result,'independent':independent},exclusive=True)
    print(json.dumps(checks),flush=True)
    if not all(checks.values()):raise SystemExit(1)


if __name__=='__main__':main()
