import json
import threading
import pytest

from pico.improver_cli import main
from pico.improver import ImproverStore
from pico.flywheel import FlywheelStore
from pico.web import Workbench
from pico.providers.clients import FakeModelClient


def test_cli_freezes_shows_audits_and_does_not_enable_export_by_default(tmp_path,capsys):
    args=['--workspace',str(tmp_path)]
    assert main(args+['prepare','--case','feedback-minimization-v1'])==0
    row=json.loads(capsys.readouterr().out)
    assert main(args+['show',row['id']])==0;assert json.loads(capsys.readouterr().out)['status']=='prepared'
    assert main(args+['audit'])==0;assert json.loads(capsys.readouterr().out)['valid']
    assert main(args+['export-metrics','--output',str(tmp_path/'export.json')])==2
    capsys.readouterr();assert not (tmp_path/'export.json').exists()
    assert main(args+['privacy','--mode','aggregate','--revision','0'])==0;capsys.readouterr()
    assert main(args+['export-metrics','--output',str(tmp_path/'export.json')])==0
    assert json.loads(capsys.readouterr().out)['suppressed']


def test_malformed_env_line_never_prints_credential(tmp_path,capsys,monkeypatch):
    monkeypatch.delenv('BIGMODEL_API_KEY',raising=False)
    path=tmp_path/'local.env';path.write_text('bad key = PRIVATE_SECRET_VALUE')
    row=ImproverStore(tmp_path).prepare('feedback-minimization-v1')
    assert main(['--workspace',str(tmp_path),'request',row['id'],'--live','--env-file',str(path)])==2
    assert 'PRIVATE_SECRET_VALUE' not in capsys.readouterr().out
    assert ImproverStore(tmp_path).get(row['id'])['status']=='prepared'


def test_copied_improver_database_cannot_change_workspace_ownership(tmp_path):
    import shutil
    source=tmp_path/'source';target=tmp_path/'target'
    store=ImproverStore(source);store.prepare('feedback-minimization-v1')
    (target/'.pico/improver').mkdir(parents=True)
    shutil.copyfile(store.path,target/'.pico/improver/proposals.sqlite3')
    with pytest.raises(ValueError,match='another workspace'):ImproverStore(target)


def test_real_web_job_collects_only_local_process_metrics(tmp_path):
    app=Workbench(tmp_path,harness_profile='workbench',load_env=False,
        model_factory=lambda:FakeModelClient(['<final>收到，尚未验收。</final>']))
    submitted=app.submit({'prompt':'PRIVATE_WEB_CANARY 调整材料。','mode':'assistant'})
    task=app.task(submitted['id'])
    with task.condition:assert task.condition.wait_for(lambda:task.finished is not None and app.active_id is None,20)
    report=FlywheelStore(tmp_path).summary()
    assert report['runs']==1 and report['verified_runs']==0
    assert 'PRIVATE_WEB_CANARY' not in json.dumps(report)
    assert 'improver' not in app.config()


def test_offline_demo_inside_a_git_parent_pins_the_selected_workspace(tmp_path):
    from pathlib import Path
    import subprocess,sys
    (tmp_path/'.git').mkdir()
    output=tmp_path/'demo'
    script=Path(__file__).resolve().parents[1]/'scripts/run_improver_demo.py'
    run=subprocess.run([sys.executable,'-X','utf8',str(script),'--output',str(output)],
        cwd=tmp_path,capture_output=True,timeout=30)
    assert run.returncode==0,run.stderr.decode('utf-8',errors='replace')
    report=json.loads((output/'report.json').read_text('utf-8'))
    assert report['metrics']['runs']==3 and report['metrics']['verified_runs']==0
    assert report['external_model_calls']==0 and report['human_decision'] is None
    assert not (tmp_path/'.pico').exists()
