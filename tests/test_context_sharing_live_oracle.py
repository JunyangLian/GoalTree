"""Offline checks of the new live-test driver and independent oracle."""
import copy
import json

from scripts.eval_context_sharing_live import PLAN, PROTOCOL, dump
from scripts.verify_context_sharing_live import parse, verify


def assets(root, plan):
    dump(root/'freeze.json',{'plan':plan,'protocol':PROTOCOL})
    dump(root/'fixtures.json',{'donor':{'id':'a','context_notes':'fixture'},'handoff_task':{'id':'t'}})


def test_driver_keeps_budget_retries_and_model_explicit():
    assert PROTOCOL['known_token_limit']==123403 and PROTOCOL['estimated_cny_limit']=='0.5'
    assert PROTOCOL['model']=='glm-5.3-flash' and PROTOCOL['transport_attempts']==1
    assert len({r['id'] for r in PLAN})==7
    assert all('gold' in r for r in PLAN)


def test_oracle_requires_typed_fields_and_no_prose_success():
    assert parse('Everything succeeded') is None
    assert parse('```json\n{"n":8}\n```')=={'n':8}
    assert parse('prefix {"n":8}') is None


def test_unstarted_slots_remain_in_denominator(tmp_path):
    assets(tmp_path,PLAN)
    result=verify(tmp_path)
    assert result['planned_cases']==7 and result['started_cases']==0 and result['passed_cases']==0
    assert all(r['failure']=='unstarted' for r in result['rows'])


def test_correct_self_report_does_not_pass_without_real_handoff(tmp_path):
    spec=next(r for r in PLAN if r['kind']=='handoff');assets(tmp_path,[spec])
    dump(tmp_path/spec['id']/'result.json',{'status':'completed','answer':json.dumps(spec['gold']),
         'events':[],'state_after':{'tasks':{'t':{'status':'planned'}}}})
    row=verify(tmp_path)['rows'][0]
    assert all(row['checks'][k] for k in row['checks'] if k.startswith('field:'))
    assert not row['passed'] and not row['checks']['one_actual_child']


def test_unknown_usage_is_preserved_not_zero(tmp_path):
    assets(tmp_path,[])
    (tmp_path/'ledger.jsonl').write_text(json.dumps({'event':'reserved','id':'unknown','reserved_tokens':12345})+'\n')
    result=verify(tmp_path)
    assert result['unknown_requests']==1 and result['unknown_reserved_tokens']==12345


def test_started_setup_failure_is_not_relabelled_unstarted(tmp_path):
    assets(tmp_path,[PLAN[0]])
    dump(tmp_path/'slots.json',[{'id':PLAN[0]['id'],'status':'runner_failed'}])
    result=verify(tmp_path)
    assert result['started_cases']==1 and result['passed_cases']==0
    assert result['rows'][0]['failure']=='runner_or_setup_failure'


def test_appcontainer_runtime_is_constructible_before_any_model_dispatch(tmp_path):
    from types import SimpleNamespace
    from scripts.eval_context_sharing_live import make_app
    app=make_app(tmp_path/'workspace','offline-fixture-only',SimpleNamespace(protocol=PROTOCOL),None)
    a=app._build_agent(None,'ask',3)
    assert a.shell_backend=='appcontainer'
    assert a.verification_command==PROTOCOL['verification_command']
