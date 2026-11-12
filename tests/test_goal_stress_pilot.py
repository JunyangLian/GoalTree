"""Freeze/oracle/budget probes for a diagnostic, not Agent ability scores."""
import copy
import json
from pathlib import Path

import pytest

from scripts import eval_goal_stress as runner
from scripts.verify_goal_stress import answer_object, evaluate_fields, verify
from scripts.glm_live_support import BudgetExceeded
from pico.providers.clients import FakeModelClient
from pico.web import Workbench


def test_oracle_rejects_wrong_types_missing_fields_and_prose_success():
    gold = {'budget': [4500], 'species': ['罗非鱼']}
    assert evaluate_fields('{"budget":4500,"species":"罗非鱼"}', gold)['passed']
    for text in ('{"budget":"4500","species":"罗非鱼"}', '{"budget":4500}', '全部记住了'):
        assert not evaluate_fields(text, gold)['passed']
    assert answer_object('prefix {"budget":4500}') is None


def test_frozen_concept_checks_keep_order_instead_of_bag_of_words():
    expected = {'order': {'all_patterns': ['先.*批次.*再.*差异']}}
    assert evaluate_fields('{"order":"先检查批次效应，再进行差异分析"}', expected)['passed']
    assert not evaluate_fields('{"order":"先差异分析，再检查批次"}', expected)['passed']


def test_reasoning_omission_does_not_modify_actual_provider_payload():
    payload = {'messages': [{'reasoning_content': 'private thought', 'content': 'actual answer'}]}
    original = copy.deepcopy(payload)
    assert 'private thought' not in json.dumps(runner.without_reasoning(payload))
    assert payload == original


def test_approval_never_authorizes_acceptance_switch_or_shell():
    def approval(action, **fields):
        return {'name': 'goal_action', 'args': {'proposal': json.dumps({'action': action, **fields})}}
    assert runner.authorized_approval(approval('update_goal', goal_id='A'), 'A')
    for action in ('accept_task', 'select', 'create_goal'):
        assert not runner.authorized_approval(approval(action, goal_id='A'), 'A')
    assert not runner.authorized_approval(approval('update_goal', goal_id='B'), 'A')
    assert not runner.authorized_approval({'name': 'run_shell', 'args': {}}, 'A')
    assert not runner.authorized_approval({'name': 'confirm_task', 'args': {}}, 'A')


def test_fixture_does_not_place_oracle_or_harness_assets_in_workspace(tmp_path):
    cases = json.loads((runner.ASSETS / 'tasks.json').read_text(encoding='utf-8'))
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False,
                    model_factory=lambda: FakeModelClient([]))
    fixture = runner.create_fixture(app, cases[1])
    assert len(fixture['goals']) == 3
    assert fixture['conversation_session_id'] == app.goals.binding(fixture['goals']['A']['id'], '', 'assistant')['session_id']
    assert not list(tmp_path.rglob('gold.json'))
    assert not list(tmp_path.rglob('verify_goal_stress.py'))


def test_budget_denial_is_before_dispatch_and_remains_halted(tmp_path, monkeypatch):
    runner.dump(tmp_path / 'budget.json', {'max_spend_cny': '1', 'max_total_tokens': 1000})
    monkeypatch.setattr(runner, 'sources', lambda: {'fixed': 'hash'})
    observer = runner.Observer(tmp_path, {'maximum_requests': 3}, {'fixed': 'hash'})
    observer.turn_dir = tmp_path / 'case/turn'
    with pytest.raises(BudgetExceeded):
        observer.before({'messages': [], 'max_tokens': 128}, {})
    assert observer.halted
    assert observer.budget.token_state() == (0, 0)
    assert json.loads((tmp_path / 'ledger.jsonl').read_text())['event'] == 'denied'
    with pytest.raises(BudgetExceeded):
        observer.before({'messages': [], 'max_tokens': 128}, {})


def test_source_drift_stops_before_budget_reservation(tmp_path, monkeypatch):
    runner.dump(tmp_path / 'budget.json', {'max_spend_cny': '1', 'max_total_tokens': 50000})
    monkeypatch.setattr(runner, 'sources', lambda: {'changed': 'new'})
    observer = runner.Observer(tmp_path, {'maximum_requests': 3}, {'fixed': 'old'})
    observer.turn_dir = tmp_path / 'case/turn'
    with pytest.raises(BudgetExceeded, match='source changed'):
        observer.before({'messages': [], 'max_tokens': 128}, {})
    assert not (tmp_path / 'ledger.jsonl').exists()


def test_correct_prose_focus_does_not_pass_host_goal_routing(tmp_path):
    case = {'id': 'switch', 'kind': 'switch', 'turns': [{'id': 't1', 'target': 'B'}]}
    runner.dump(tmp_path / 'freeze.json', {'tasks': [case], 'gold': {'switch': {'t1': {'fields': {'当前目标': ['B']}}}},
                                        'protocol': {'minimum_compacted_requests': 2, 'minimum_distinct_reads': 10}})
    runner.dump(tmp_path / 'switch/fixture.json', {'goals': {'A': {'id': 'a'}, 'B': {'id': 'b'}}, 'conversation_session_id': 's'})
    runner.dump(tmp_path / 'switch/t1/result.json', {'answer': '{"当前目标":"B","目标标识":"b"}', 'status': 'completed',
                    'binding': {'goal_id': 'a'}, 'session_id': 's', 'state_after': {'goals': {}, 'tasks': {}}})
    result = verify(tmp_path)
    row = result['rows'][0]
    assert row['answer_passed'] and row['declared_goal_matches_target']
    assert not row['binding_matches_target']
    assert row['failure_category'] == 'goal_routing_not_completed'


def test_missing_result_is_unstarted_not_silent_pass(tmp_path):
    runner.dump(tmp_path / 'freeze.json', {'tasks': [{'id': 'long', 'kind': 'long', 'turns': [{'id': 't1'}]}],
            'gold': {'long': {'t1': {'fields': {'fact': ['correct']}}}},
            'protocol': {'minimum_compacted_requests': 2, 'minimum_distinct_reads': 10}})
    result = verify(tmp_path)
    assert result['cases']['long']['checkpoint_total'] == 1
    assert result['cases']['long']['checkpoint_passed'] == 0
    assert result['rows'][0]['status'] == 'unstarted'
    assert not result['cases']['long']['pressure_qualified']


def test_real_workbench_turn_keeps_original_conversation_binding(tmp_path):
    cases = json.loads((runner.ASSETS / 'tasks.json').read_text(encoding='utf-8'))
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    app = Workbench(workspace, harness_profile='workbench', load_env=False,
                    model_factory=lambda: FakeModelClient(['<final>离线机制检查，不代表模型理解。</final>']))
    fixture = runner.create_fixture(app, cases[1])
    class Observation:
        turn_dir = tmp_path / 'turn'
        protocol = {'max_tool_steps_per_turn': 3, 'turn_deadline_seconds': 10}
        halted = None
    result = runner.run_turn(app, cases[1], {'prompt': '仅回答你好，不修改目标。', 'target': 'B'}, fixture, Observation())
    assert result['session_id'] == fixture['conversation_session_id']
    assert result['binding']['goal_id'] == fixture['goals']['A']['id']
    assert result['status'] == 'completed'


@pytest.mark.parametrize('value', ['1', True, None])
def test_wrong_revision_type_reports_parameter_error_without_mutating_state(tmp_path, value):
    from pico.goals import GoalStore, GoalError
    store = GoalStore(tmp_path)
    goal = store.action({'action': 'create_goal', 'title': 'version check'})
    before = store.read()
    with pytest.raises(GoalError, match='JSON整数') as caught:
        store.action({'action': 'update_goal', 'goal_id': goal['id'], 'revision': value, 'context_notes': 'must not apply'})
    assert caught.value.status == 400
    assert store.read() == before


def test_stale_integer_revision_still_keeps_cas_boundary(tmp_path):
    from pico.goals import GoalStore, GoalError
    store = GoalStore(tmp_path)
    goal = store.action({'action': 'create_goal', 'title': 'version check'})
    with pytest.raises(GoalError) as caught:
        store.action({'action': 'update_goal', 'goal_id': goal['id'], 'revision': goal['revision'] + 1, 'context_notes': 'must not apply'})
    assert caught.value.status == 409


def test_unknown_budget_reservation_is_not_treated_as_zero(tmp_path):
    runner.dump(tmp_path / 'budget.json', {'max_spend_cny': '1', 'max_total_tokens': 50000})
    from scripts.glm_live_support import Budget
    budget = Budget(tmp_path)
    identifier = budget.before({'messages': [{'role': 'user', 'content': 'public input'}], 'max_tokens': 1024}, 'first')
    budget.after(identifier, {'usage': {}})
    known, pending = budget.token_state()
    assert known == 0 and pending > 8192
    assert budget.state()[1] > 0
