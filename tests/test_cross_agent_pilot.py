import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch
import urllib.error

import pytest

from pico.evaluation.cross_agent import (check_tool_permission, clean_environment,
    process_metrics, scope_valid, snapshot, summarize, unittest_result, verify_candidate)
from pico.windows_sandbox import VERIFY
from scripts.apecode_pilot_adapter import SiliconFlowChatModel
from scripts.run_cross_agent_pilot import MODEL, PROTOCOL, report


def task():
    files={'code.py':'value=1\n','test_acceptance.py':'frozen test\n'}
    return {'id':'fixture','files':files,'sha256':{n:hashlib.sha256(v.encode()).hexdigest() for n,v in files.items()},
            'editable_files':['code.py'],'expected_tests':2}


@pytest.mark.parametrize('output,code,expected,accepted',[
    ('Ran 2 tests in 0.01s\n\nOK\n',0,2,True),
    ('Ran 2 tests in 0.01s\n\nFAILED (failures=1)\n',1,2,False),
    ('Ran 2 tests in 0.01s\n\nOK (skipped=1)\n',0,2,False),
    ('Ran 1 test in 0.01s\n\nOK\n',0,2,False),
    ('I fixed it and all tests pass',0,2,False),
    ('Ran 2 tests in 0.01s\n\nOK\n',1,2,False)])
def test_oracle_requires_real_summary_count_and_clean_exit(output,code,expected,accepted):
    assert unittest_result(output,code,expected)['accepted'] is accepted


def test_source_scope_detects_test_tampering_extra_files_and_missing_code(tmp_path):
    t=task()
    for n,v in t['files'].items():(tmp_path/n).write_text(v,encoding='utf-8',newline='\n')
    assert scope_valid(t,snapshot(tmp_path))
    (tmp_path/'.pico').mkdir();(tmp_path/'.pico/state.json').write_text('{}')
    assert scope_valid(t,snapshot(tmp_path))
    (tmp_path/'extra.py').write_text('x=1')
    assert not scope_valid(t,snapshot(tmp_path))
    (tmp_path/'extra.py').unlink();(tmp_path/'test_acceptance.py').write_text('forged')
    assert not scope_valid(t,snapshot(tmp_path))
    (tmp_path/'test_acceptance.py').write_text(t['files']['test_acceptance.py'],newline='\n')
    (tmp_path/'code.py').unlink();assert not scope_valid(t,snapshot(tmp_path))


def test_policy_never_runs_host_shell_or_edits_frozen_tests(tmp_path):
    assert check_tool_permission(tmp_path,['code.py'],'exec_command',{'command':VERIFY})
    assert not check_tool_permission(tmp_path,['code.py'],'exec_command',{'command':VERIFY+'; echo bypass'})
    assert not check_tool_permission(tmp_path,['code.py'],'write_file',{'path':'test_acceptance.py'})
    assert not check_tool_permission(tmp_path,['code.py'],'replace_in_file',{'path':'../code.py'})
    assert not check_tool_permission(tmp_path,['code.py'],'read_file',{'path':'.pico/state.json'})
    assert check_tool_permission(tmp_path,['code.py'],'read_file',{'path':'test_acceptance.py'})
    assert not check_tool_permission(tmp_path,['code.py'],'apply_changes',{'changes':'[{"path":"test_acceptance.py"}]'})
    assert check_tool_permission(tmp_path,['code.py'],'apply_changes',{'changes':'[{"path":"code.py"}]'})


def test_clean_child_environment_does_not_inherit_keys_or_parent_agent_context():
    with patch.dict('os.environ',{'SILICONFLOW_API_KEY':'private-fixture','OPENAI_API_KEY':'private-fixture',
                                 'CODEX_APP_TOOLS_PIPE_PATH':'private-pipe'}):
        env=clean_environment()
    assert 'SILICONFLOW_API_KEY' not in env and 'OPENAI_API_KEY' not in env
    assert 'CODEX_APP_TOOLS_PIPE_PATH' not in env


def test_incomplete_and_duplicate_records_do_not_manufacture_horizontal_scores():
    plan=[{'run_key':'a','agent':'codetrail'},{'run_key':'b','agent':'apecode'}]
    row={'run_key':'a','agent':'codetrail','task_id':'pagination_boundaries','functional_passed':True,
         'workflow_passed':True,'failure_category':'success','input_tokens':None,'elapsed_ms':15}
    result=summarize(plan,[row])
    assert not result['cross_agent_conclusion_available']
    assert result['agents']['apecode']['functional_rate'] is None
    assert result['agents']['codetrail']['input_tokens']['total'] is None
    with pytest.raises(ValueError):summarize(plan,[row,row])


def test_unknown_usage_and_stale_or_mutating_tests_are_not_success():
    t=task();final={**t['sha256'],'code.py':'new'}
    output='Ran 2 tests in 0.01s\n\nOK\n'
    observation={'name':'exec_command','args':{'command':VERIFY},'output':output,'exit_code':0,
                 'before':t['sha256'],'snapshot':final}
    result=process_metrics([observation],[{'input_tokens':100,'output_tokens':None}],t,final)
    assert result['output_tokens'] is None and result['input_tokens']==100
    assert not result['final_test_observed']


def test_external_verifier_restores_tests_and_rejects_mutation(tmp_path):
    t=task();destination=tmp_path/'external'
    def mutate(root,command,timeout):
        assert (root/'test_acceptance.py').read_text()==t['files']['test_acceptance.py']
        (root/'code.py').write_text('changed during verification')
        return {'returncode':0,'output':'Ran 2 tests in 0.01s\n\nOK\n'}
    with patch('pico.evaluation.cross_agent.run_verification',side_effect=mutate):
        result=verify_candidate(t,{'code.py':b'value=2\n'},destination)
    assert not result['accepted'] and not result['workspace_unchanged_during_test']
    assert result['host_fallback'] is False


def test_independent_transport_preserves_reasoning_and_pins_sampler_and_model(tmp_path):
    response={'model':MODEL,'choices':[{'message':{'role':'assistant','content':None,
        'reasoning_content':'provider fixture','tool_calls':[{'id':'x','type':'function','function':{'name':'read_file','arguments':'{}'}}]}}],
        'usage':{'prompt_tokens':120,'completion_tokens':18}}
    class Response(io.BytesIO):
        def __enter__(self):return self
        def __exit__(self,*args):self.close()
    with patch.dict('os.environ',{'SILICONFLOW_API_KEY':'private-fixture'}):
        model=SiliconFlowChatModel(tmp_path)
    seen=[]
    def open_request(req,timeout):
        seen.append(json.loads(req.data));assert timeout==180
        return Response(json.dumps(response).encode())
    model.opener.open=open_request
    message=model.complete(messages=[{'role':'user','content':'test'}],tools=[])
    assert message['reasoning_content']=='provider fixture' and message['tool_calls'][0]['id']=='x'
    assert seen[0]['model']==MODEL and seen[0]['thinking_budget']==4096 and seen[0]['max_tokens']==4096
    assert seen[0]['temperature']==.2 and seen[0]['top_p']==.95
    assert 'private-fixture' not in (tmp_path/'wire.jsonl').read_text()
    assert model.requests==[{'input_tokens':120,'output_tokens':18,'complete':True}]


def test_provider_402_does_not_retry_or_substitute(tmp_path):
    with patch.dict('os.environ',{'SILICONFLOW_API_KEY':'private-fixture'}):model=SiliconFlowChatModel(tmp_path)
    error=urllib.error.HTTPError('https://api.siliconflow.cn',402,'balance',{},io.BytesIO(b'No balance'))
    with patch.object(model.opener,'open',side_effect=error) as mock:
        with pytest.raises(RuntimeError,match='HTTP402'):model.complete(messages=[],tools=[])
    assert mock.call_count==1 and model.requests[0]['input_tokens'] is None
    assert PROTOCOL['agents']==['codetrail','apecode']


def test_interrupted_worker_remains_attempted_failure(tmp_path,capsys):
    plan=[{'run_key':'a','agent':'apecode','task_id':'pagination_boundaries','repeat':1}]
    (tmp_path/'plan.json').write_text(json.dumps(plan));(tmp_path/'runs/a').mkdir(parents=True)
    report(tmp_path)
    rows=json.loads((tmp_path/'results.json').read_text())
    assert rows[0]['failure_category']=='interrupted_without_result'
    assert json.loads((tmp_path/'summary.json').read_text())['attempted_runs']==1
    assert json.loads((tmp_path/'not-started.json').read_text())==[]
