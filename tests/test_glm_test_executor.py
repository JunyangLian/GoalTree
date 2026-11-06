import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import pytest
from pico.evaluation.isolated_execution import ExecutionUnavailable
from pico.evaluation.repair_suite import digest
from pico.windows_sandbox import VERIFY
from scripts.glm_test_executor import PinnedTestExecutor


def runtime():
    return {'python':sys.executable,'python_sha256':digest(sys.executable),'version':sys.version}


def test_bridge_rejects_arbitrary_commands_or_changed_interpreter_before_launch(tmp_path):
    executor=PinnedTestExecutor('appcontainer',tmp_path/'execution',runtime())
    with patch('scripts.glm_test_executor.subprocess.run') as launch:
        with pytest.raises(ValueError): executor.execute(tmp_path,'python arbitrary.py')
        with pytest.raises(ValueError,match='outside the workload'): executor.execute(tmp_path,VERIFY)
        executor.runtime={**runtime(),'python_sha256':'wrong'}
        with pytest.raises(ExecutionUnavailable): executor.execute(tmp_path/'workspace',VERIFY)
        launch.assert_not_called()


def test_bridge_will_not_accept_unproven_host_test_result(tmp_path):
    executor=PinnedTestExecutor('appcontainer',tmp_path/'execution',runtime())
    workspace=tmp_path/'workspace';workspace.mkdir()
    class Result:
        returncode=0
        stdout=json.dumps({'returncode':0,'output':'OK'}).encode()
    with patch('scripts.glm_test_executor.subprocess.run',return_value=Result()):
        with pytest.raises(ExecutionUnavailable,match='Kernel isolation proof missing'):
            executor.execute(workspace,VERIFY)


@pytest.mark.skipif(os.name!='nt',reason='Windows kernel integration')
def test_pinned_runtime_creates_private_tempfile_but_cannot_read_host(tmp_path):
    workspace=tmp_path/'workspace';workspace.mkdir()
    secret=tmp_path/'outside.txt';secret.write_text('host-canary',encoding='utf-8')
    code='import pathlib,tempfile,unittest\nclass T(unittest.TestCase):\n'
    code+='    def test_temp(self):\n        with tempfile.TemporaryDirectory() as d:\n            p=pathlib.Path(d)/"value.txt"\n            p.write_text("ok")\n            self.assertEqual(p.read_text(),"ok")\n'
    code+=f'    def test_host(self):\n        with self.assertRaises(PermissionError):\n            pathlib.Path({str(secret)!r}).read_text()\n'
    (workspace/'test_actual.py').write_text(code,encoding='utf-8')
    executor=PinnedTestExecutor('appcontainer',tmp_path/'execution',runtime())
    result=executor.execute(workspace,VERIFY)
    assert result['returncode']==0,result['output']
    assert result['audit']['kernel_token_is_appcontainer'] is True
    assert result['audit']['host_acl_changed'] is False
    assert result['audit']['capabilities']==[]
