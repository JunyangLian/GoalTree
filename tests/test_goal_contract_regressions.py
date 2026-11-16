"""Regressions from high-level GLM diagnostics, not a model-quality score."""
import hashlib
import json

import pytest

from pico.completion import CompletionGate
from pico.goals import GoalStore
from pico.providers.clients import FakeModelClient
from pico.session_context import start, observe, protected_view
from pico.tool_executor import ToolExecutionResult
from pico.web import Task, Workbench
from test_dynamic_goal_tree import submit, progress, tool, setup


def mutation(item, revision, **fields):
    return tool('goal_action', {'proposal': json.dumps({'action': 'update_task', 'task_id': item['id'],
                                                       'revision': revision, **fields})})


@pytest.mark.parametrize('ancestor', [False, True])
def test_contract_changed_after_submission_requires_current_resubmission(tmp_path, ancestor):
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False)
    goal, parent, leaves = setup(app)
    leaf = leaves[0]
    target = parent if ancestor else leaf
    revision = target['revision'] if ancestor else leaf['revision'] + 1
    corrected = {**leaf, 'revision': leaf['revision'] + 2}
    job, _ = submit(app, goal, [progress(leaf, 'OLD_DRAFT'), mutation(target, revision, acceptance='NEW_CONTRACT'),
        '<final>All submitted.</final>', progress(corrected, 'CURRENT_DRAFT'), '<final>Current draft submitted.</final>'])
    node = app.goals.read()['tasks'][leaf['id']]
    assert node['status'] == 'review'
    assert [d['task_spec_version'] for d in node['deliverables']] == [1, 2]
    assert 'CURRENT_DRAFT' in job.answer and 'OLD_DRAFT' not in job.answer
    assert any(e.get('event') == 'completion_rejected' for e in job.events)
    assert not job.agent.goal_sync_problem()


def test_budget_exhaustion_cannot_turn_stale_output_into_success(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False)
    goal, _, leaves = setup(app)
    leaf = leaves[0]
    app.make_model = lambda: FakeModelClient([progress(leaf, 'OLD_DRAFT'), mutation(leaf, leaf['revision'] + 1, acceptance='NEW_CONTRACT'), '<final>Submitted.</final>'])
    job = app.task(app.submit({'mode': 'assistant', 'goal_id': goal['id'], 'prompt': 'Deliver, then change requirement',
        'max_steps': 2, 'cooperation': {'planning': 'autonomous', 'implementation': 'autonomous'}})['id'])
    with job.condition:
        assert job.condition.wait_for(lambda: job.finished is not None and app.active_id is None, 20)
    assert job.status == 'stopped' and job.stop_reason == 'verification_incomplete'
    assert app.goals.read()['tasks'][leaf['id']]['status'] == 'planned'
    assert not any(m.get('content') == 'Submitted.' and m.get('role') == 'assistant' for m in job.agent.session['history'])


def test_removed_scope_does_not_force_old_output_or_resubmission(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False)
    goal, _, leaves = setup(app)
    leaf = leaves[0]
    archive = tool('goal_action', {'proposal': json.dumps({'action': 'archive_task', 'task_id': leaf['id'], 'revision': leaf['revision'] + 1})})
    job, _ = submit(app, goal, [progress(leaf, 'REMOVED_DRAFT'), archive, '<final>Removed from scope.</final>'])
    assert 'REMOVED_DRAFT' not in job.answer and not job.agent.goal_sync_problem()
    assert app.goals.read()['tasks'][leaf['id']]['deliverables']


@pytest.mark.parametrize('name,args', [
    ('plan_tasks', {'steps': '[{"title":"Research","acceptance":"verification_mode=manual"}]'}),
    ('goal_action', {'proposal': '{"action":"create_task","title":"Research","acceptance":"verification_mode=manual"}'}),
])
def test_missing_mode_is_rejected_before_approval_or_mutation(tmp_path, name, args):
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False)
    goal, _, _ = setup(app)
    app.make_model = lambda: FakeModelClient([tool(name, args), '<final>Missing field rejected.</final>'])
    job = app.task(app.submit({'mode': 'assistant', 'goal_id': goal['id'], 'prompt': 'Only a mode validation probe',
        'max_steps': 3, 'cooperation': {'planning': 'step', 'implementation': 'step'}})['id'])
    before = app.goals.read()
    with job.condition:
        assert job.condition.wait_for(lambda: job.finished is not None or job.approval is not None, 20)
    assert job.approval is None
    with job.condition:
        assert job.condition.wait_for(lambda: job.finished is not None and app.active_id is None, 20)
    assert app.goals.read()['tasks'] == before['tasks']
    assert any('explicit JSON field verification_mode' in e.get('result', '') for e in job.events if e.get('event') == 'tool_executed')


def test_missing_mode_can_be_corrected_explicitly_without_silent_downgrade(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False)
    goal, _, _ = setup(app)
    invalid = tool('plan_tasks', {'steps': '[{"title":"New research","acceptance":"verification_mode=manual"}]'})
    fixed = tool('plan_tasks', {'steps': '[{"title":"New research","verification_mode":"manual"}]'})
    job, _ = submit(app, goal, [invalid, fixed, '<final>Explicit manual task saved.</final>'])
    nodes = [t for t in app.goals.read()['tasks'].values() if t['title'] == 'New research']
    assert len(nodes) == 1 and nodes[0]['verification_mode'] == 'manual'
    statuses = [e['tool_status'] for e in job.events if e.get('event') == 'tool_executed' and e.get('name') == 'plan_tasks']
    assert statuses == ['rejected', 'ok']


@pytest.mark.parametrize('mode', ['manual', 'configured'])
def test_explicit_mode_controls_bound_child_without_weakening_configured_gate(tmp_path, mode):
    command = 'python -B -m unittest discover -v'
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False, verification_command=command, model_factory=lambda: FakeModelClient([]))
    goal = app.goals.action({'action': 'create_goal', 'title': 'Research'})
    node = app.goals.action({'action': 'create_task', 'goal_id': goal['id'], 'title': 'Inspect', 'verification_mode': mode})
    task = Task('Inspect', node['session_id'], 'ask', 5, command)
    task.binding = app.goals.binding(goal['id'], node['id'], 'execute')
    agent = app._build_agent(task.session_id, 'ask', 5)
    app._bind_tree(task, agent)
    assert CompletionGate(agent).enabled is (mode == 'configured')
    assert agent.verification_command == (command if mode == 'configured' else None)
    context = app.goals.context(app.goals.binding(goal['id'], '', 'assistant'))
    assert context['existing_tasks'][0]['verification_mode'] == mode


def coverage(sha, low, high, ref):
    return ToolExecutionResult(f'# records/data.txt (2 total lines) sha256={sha}\n' +
                              '\n'.join(f'{i:4}: line' for i in range(low, high + 1)),
                              {'tool_status': 'ok', 'evidence_id': ref})


def test_file_aliases_merge_coverage_and_migrate_restart_without_cross_version_union(tmp_path):
    path = tmp_path / 'records/data.txt'
    path.parent.mkdir()
    path.write_bytes(b'a\nb\n')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False, model_factory=lambda: FakeModelClient([]))
    agent = app._build_agent(None, 'ask', 6)
    start(agent, 'r1')
    observe(agent, 'read_file', {'path': 'records/data.txt'}, coverage(sha, 1, 1, 'ev_a'), {})
    alias = str(path.resolve())
    observe(agent, 'read_file', {'path': alias}, coverage(sha, 2, 2, 'ev_b'), {})
    row = protected_view(agent)['reads']
    assert list(row) == ['records/data.txt'] and row['records/data.txt']['full_file_returned']
    assert len(row['records/data.txt']['evidence_refs']) == 2
    agent.session['context_ledger']['reads'][alias] = agent.session['context_ledger']['reads'].pop('records/data.txt')
    start(agent, 'r2')
    assert list(agent.session['context_ledger']['reads']) == ['records/data.txt']
    path.write_bytes(b'x\ny\n')
    new_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    observe(agent, 'read_file', {'path': alias}, coverage(new_sha, 2, 2, 'ev_new'), {})
    row = protected_view(agent)['reads']['records/data.txt']
    assert row['source_current'] and not row['full_file_returned'] and row['returned_ranges'] == [[2, 2]]


def test_error_recovery_with_another_spelling_clears_only_same_file_error(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False, model_factory=lambda: FakeModelClient([]))
    agent = app._build_agent(None, 'ask', 6)
    start(agent, 'r1')
    failure = ToolExecutionResult('error missing', {'tool_status': 'error', 'tool_error_code': 'tool_failed'})
    observe(agent, 'read_file', {'path': './a.txt'}, failure, {})
    observe(agent, 'read_file', {'path': 'other.txt'}, failure, {})
    (tmp_path / 'a.txt').write_bytes(b'a\nb\n')
    sha = hashlib.sha256((tmp_path / 'a.txt').read_bytes()).hexdigest()
    observe(agent, 'read_file', {'path': str(tmp_path / 'a.txt')}, coverage(sha, 1, 2, 'ev_success'), {})
    errors = list(protected_view(agent)['errors'].values())
    assert len(errors) == 1 and errors[0]['locator'] == 'other.txt'


def test_actual_file_receipt_is_canonical_but_boundary_still_rejects_escape(tmp_path):
    path = tmp_path / 'records/data.txt'
    path.parent.mkdir()
    path.write_bytes(b'public\n')
    app = Workbench(tmp_path, harness_profile='workbench', load_env=False, model_factory=lambda: FakeModelClient([]))
    agent = app._build_agent(None, 'ask', 6)
    result = agent.execute_tool('read_file', {'path': str(path.resolve())})
    assert result.metadata['source_paths'] == ['records/data.txt']
    assert result.metadata['source_sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    rejected = agent.execute_tool('read_file', {'path': '../outside.txt'})
    assert rejected.metadata['tool_status'] != 'ok'
