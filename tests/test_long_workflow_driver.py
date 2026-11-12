import json
from types import SimpleNamespace

from scripts.eval_long_workflows_live import ROOT, PROTOCOL, plan, approval_allowed
from scripts.eval_context_sharing_live import make_app
from scripts.verify_long_workflows import obj

def test_frozen_workflows_have_twelve_turns_and_separate_gold():
    tasks=json.loads((ROOT/'benchmarks/long_workflows_v1/tasks.json').read_text('utf-8'))
    assert len(tasks)==3
    for task in tasks:
        turns=plan(task)
        assert len(turns)==12 and len({t['id'] for t in turns})==12
        assert next(t for t in turns if t['id']=='t06')['gold']['error_code']=='invalid_arguments'
        assert next(t for t in turns if t['id']=='t10')['gold']['shared_code']==task['shared']
        assert task['shared'] not in next(t for t in turns if t['id']=='t07')['prompt']

def test_batch_approvals_never_authorize_acceptance_shell_or_foreign_goals():
    for action in ('accept_task','archive_task','create_goal'):
        assert not approval_allowed({'name':'goal_action','args':{'action':action}},'g')
    assert not approval_allowed({'name':'run_shell','args':{'command':'python -B -m unittest discover -v'}},'g')
    assert not approval_allowed({'name':'goal_action','args':{'action':'update_task','goal_id':'other'}},'g')
    assert approval_allowed({'name':'goal_action','args':{'proposal':'{"action":"create_task","goal_id":"g"}'}},'g')

def test_missing_file_gold_matches_real_tool_receipt_before_paid_run(tmp_path):
    app=make_app(tmp_path/'workspace','offline-synthetic',SimpleNamespace(protocol=PROTOCOL),None)
    agent=app._build_agent(None,'ask',3)
    result=agent.execute_tool('read_file',{'path':'missing-required.txt'})
    assert result.metadata['tool_error_code']=='invalid_arguments'
    assert result.metadata['tool_status']=='rejected'
    assert app.shell_backend=='appcontainer'

def test_oracle_rejects_extra_prose_or_wrong_type():
    assert obj('Explained\n```json\n{"quota":17}\n```')=={}
    assert obj('```json\n{"quota":17}\n```')=={'quota':17}
    assert obj('[]')=={}
