import pytest
from pico.evaluation.benchmark_score import CATEGORIES, score
from pico.evaluation.repair_suite import write_json
from scripts.glm_live_support import Budget, BudgetExceeded
from scripts.run_glm_benchmark import prepare
from pico.evaluation.repair_suite import load_json, digest
import sys
from pathlib import Path
from unittest.mock import patch


def fixture(repeats=1):
    plan = [{'agent': 'a', 'task_id': c, 'category': c, 'repeat': r, 'run_key': c+str(r)}
            for c in CATEGORIES for r in range(1, repeats+1)]
    rows = [{**p, 'scope_valid': True, 'functional_passed': True, 'workflow_passed': True,
             'audit_valid': True, 'input_tokens': 100, 'output_tokens': 10, 'tool_steps': 5} for p in plan]
    return plan, rows


def test_fast_incorrect_and_unverified_patches_cannot_receive_process_credit():
    plan, rows = fixture()
    rows[0].update(functional_passed=False, tool_steps=0, input_tokens=0)
    rows[1]['workflow_passed'] = False
    rows[2]['audit_valid'] = False
    result = score(plan, rows)['agents']['a']
    assert result['verified_score'] == 25 and result['functional_score'] == 75


def test_missing_runs_make_score_provisional_instead_of_dropping_denominator():
    plan, rows = fixture()
    result = score(plan, rows[:-1])['agents']['a']
    assert result['verified_score'] is None and result['verified_bounds'] == [75, 100]
    assert result['input_tokens'] is None


def test_three_repeats_are_not_three_independent_tasks_or_best_of_three():
    plan, rows = fixture(3)
    rows[0]['workflow_passed'] = False
    result = score(plan, rows, 3)
    assert result['unique_tasks'] == 4 and result['planned_runs'] == 12
    assert result['agents']['a']['stable_score'] == 75
    assert result['agents']['a']['verified_score'] == pytest.approx(100*11/12)


def test_category_macro_does_not_overweight_a_larger_easy_category():
    plan, rows = fixture()
    rows[0]['workflow_passed'] = False
    extra = {**plan[1], 'task_id': 'extra', 'run_key': 'extra'}
    plan.append(extra)
    rows.append({**rows[1], **extra})
    assert score(plan, rows)['agents']['a']['verified_score'] == 75


def test_duplicates_identity_changes_and_missing_usage_are_not_silently_repaired():
    plan, rows = fixture()
    with pytest.raises(ValueError): score(plan, rows+[rows[0]])
    changed = [{**rows[0], 'agent': 'b'}, *rows[1:]]
    with pytest.raises(ValueError): score(plan, changed)
    rows[0]['input_tokens'] = None
    assert score(plan, rows)['agents']['a']['input_tokens'] is None


def test_token_reservations_cover_unknown_requests_and_prevent_next_transmission(tmp_path):
    write_json(tmp_path/'budget.json', {'max_spend_cny': '5', 'max_total_tokens': 25000})
    gate = Budget(tmp_path)
    payload = {'messages': [], 'max_tokens': 4096}
    first = gate.before(payload, 'first')
    _, reserved = gate.token_state()
    assert reserved > 12000
    gate.after(first, {})
    assert gate.token_state() == (0, reserved)
    gate.before(payload, 'second')
    with pytest.raises(BudgetExceeded): gate.before(payload, 'third')


def test_token_settlement_includes_both_input_and_output_and_earlier_failures(tmp_path):
    write_json(tmp_path/'budget.json', {'max_spend_cny': '5', 'max_total_tokens': 2000000})
    gate = Budget(tmp_path)
    first = gate.before({'max_tokens': 4096}, 'failed-repair')
    gate.after(first, {'usage': {'prompt_tokens': 200, 'completion_tokens': 50}})
    assert gate.token_state() == (250, 0)


def test_frozen_plan_uses_identical_tasks_once_per_agent_without_reference_assets(tmp_path):
    # Real preparation on the actual frozen task assets, but no API or worker launched.
    class PythonVersion:
        stdout = 'Python 3.13.11\n'
    source = tmp_path/'unit-upstream'
    source.mkdir()
    (source/'LICENSE').write_text('unit fixture; no agent executed', encoding='utf-8')
    hashes = {'LICENSE': digest(source/'LICENSE')}
    with patch('scripts.run_glm_benchmark.subprocess.run', return_value=PythonVersion()), \
         patch('scripts.run_glm_benchmark.source_info', return_value=hashes), \
         patch('scripts.run_glm_live_pilot.source_info', return_value=hashes):
        prepare(tmp_path/'cohort', source, Path(sys.executable))
        prepare(tmp_path/'core-only', source, Path(sys.executable),core_only=True)
    plan = load_json(tmp_path/'cohort/plan.json')
    assert len(plan) == 60 and len({p['task_id'] for p in plan}) == 30
    for index in range(0, 60, 2):
        assert plan[index]['task_id'] == plan[index+1]['task_id']
        assert plan[index]['agent'] != plan[index+1]['agent']
    assert load_json(tmp_path/'cohort/summary.json')['agents']['codetrail']['verified_score'] is None
    core = load_json(tmp_path/'cohort/protocol.json')['core_task_ids']
    assert len(core) == len(set(core)) == 12
    assert {p['task_id'] for p in plan[:24]} == set(core)
    assert all(len({p['task_id'] for p in plan[:24] if p['category']==c}) == 3 for c in CATEGORIES)
    subset=load_json(tmp_path/'core-only/plan.json')
    assert len(subset)==24 and {p['task_id'] for p in subset}==set(core)
    assert len(load_json(tmp_path/'core-only/tasks.json'))==12
    assert not (tmp_path/'cohort/runs').exists()


def test_score_refuses_unmatched_agent_task_sets():
    plan, rows = fixture()
    other = [{**p, 'agent': 'b', 'run_key': 'b'+p['run_key']} for p in plan]
    other[-1]['task_id'] = 'different'
    with pytest.raises(ValueError): score(plan+other, rows)
