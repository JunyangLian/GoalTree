"""Offline wiring demo; no external model, real human decision, or benchmark score."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pico.runtime import Pico,SessionStore
from pico.workspace import WorkspaceContext
from pico.providers.clients import FakeModelClient
from pico.flywheel import FlywheelStore
from pico.improver import ImproverStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();output=args.output.resolve()
    if output.exists():parser.error('Use a new output directory to preserve previous records')
    workspace=output/'workspace';workspace.mkdir(parents=True)
    for i in range(3):
        agent=Pico(FakeModelClient(['<final>离线预设回复，未验收任务。</final>']),WorkspaceContext.build(workspace,repo_root_override=workspace),
            SessionStore(workspace/'.pico/sessions'),harness_profile='workbench',max_depth=0)
        agent.ask('临时说明 PUBLIC_SYNTHETIC_CANARY_'+str(i)+'，请调整报告。')
        assert 'PUBLIC_SYNTHETIC_CANARY' not in json.dumps(agent.capabilities.learning.store.sources(session_id=agent.session['id']))
    store=ImproverStore(workspace);prepared=store.prepare('feedback-minimization-v1')
    proposal={'title':'Minimize learning sources','hypothesis':'Duplicated ordinary feedback creates unnecessary private copies.',
        'changes':['Keep a generic source receipt and retain only explicitly requested safe presentation preferences.'],
        'targets':['pico/learning_runtime.py','pico/flywheel.py'],
        'validation':['Randomized external canary test, preference regression and permission boundary test.'],
        'risks':['Pattern checks do not prove all derived text is non-sensitive.'],
        'rollback':'Operator reviews code revision; history deletion is a separate explicit action.',
        'evidence_case':'feedback-minimization-v1'}
    row=store.request(prepared['id'],FakeModelClient([json.dumps(proposal)]))
    metrics=FlywheelStore(workspace)
    result={'mode':'offline_fixture','external_model_calls':0,'proposal_id':row['id'],'proposal_status':row['status'],
            'human_decision':None,'metrics':metrics.summary(),'opportunities':metrics.opportunities(),'audit':store.audit_report()}
    (output/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
