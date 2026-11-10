"""Unified conversation routing is model/tool driven, not a keyword classifier."""
import json
import threading

import pytest

from pico.providers.clients import FakeModelClient
from pico.web import Workbench


def complete(app, data):
    task = app.task(app.submit(data)['id'])
    with task.condition:
        assert task.condition.wait_for(lambda: task.finished is not None, 20)
    return task


class ScriptedAssistant(FakeModelClient):
    model = 'offline scripted test'
    def __init__(self):
        super().__init__([])
        self.step = 0

    @staticmethod
    def tool(name, args):
        return '<tool>' + json.dumps({'name': name, 'args': args}) + '</tool>'

    def complete(self, prompt, *args, **kwargs):
        self.prompts.append(prompt)
        self.step += 1
        scope = json.JSONDecoder().raw_decode(prompt.split('GoalTree current scoped task state:\n', 1)[1])[0]
        if scope['mode'] == 'execute':
            if self.step == 1:
                return self.tool('read_file', {'path': 'README.md'})
            return '<final>Read the actual local file.</final>'
        if self.step == 1:
            return self.tool('goal_action', {'proposal': json.dumps({'action': 'create_goal', 'title': 'Research goal'})})
        if self.step == 2:
            return self.tool('plan_tasks', {'steps': json.dumps([{'id': 'read', 'title': 'Read local sources', 'verification_mode': 'manual'}])})
        task_id = scope['existing_tasks'][0]['id']
        if self.step == 3:
            return self.tool('run_task', {'task_id': task_id, 'instruction': 'Read README.md and report actual content.'})
        if self.step == 4:
            return self.tool('confirm_task', {'task_id': task_id, 'reason': 'Operator must inspect the recorded README read evidence.'})
        return '<final>Execution results and operator review were recorded separately.</final>'


class NativeAssistant(ScriptedAssistant):
    supports_native_tools = True
    def begin_native_run(self, tools, context, message, command):
        from pico.providers.native_tools import NativeToolConversation
        self.native_conversation = NativeToolConversation(tools, context, message, command, require_reasoning=False)
        self.sent = []
    def complete(self, prompt, *args, **kwargs):
        self.sent.append(self.native_conversation.payload())
        scripted = super().complete(prompt, *args, **kwargs)
        if scripted.startswith('<tool>'):
            call = json.loads(scripted.removeprefix('<tool>').removesuffix('</tool>'))
            message = {'tool_calls': [{'id': 'native-'+str(self.step), 'type': 'function',
                       'function': {'name': call['name'], 'arguments': json.dumps(call['args'])}}]}
        else:
            message = {'content': scripted.removeprefix('<final>').removesuffix('</final>')}
        return self.native_conversation.accept({'choices': [{'message': message}]}, {})
    def observe_native_tool(self, result):
        self.native_conversation.observe(result)
    def native_notice(self, message):
        self.native_conversation.notice(message)


def test_plain_question_creates_no_goal_or_task(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient(['<final>Hello</final>']))
    initial = app.goals.read()
    run = complete(app, {'mode': 'assistant', 'prompt': 'A normal question'})
    assert run.status == 'completed'
    assert not app.goals.read()['goals'] and not app.goals.read()['tasks']
    assert run.session_id == initial['assistant_session_id']
    assert {'goal_action', 'plan_tasks', 'run_task'} <= run.agent.tools.keys()
    assert not {'write_file', 'patch_file', 'run_shell'} & run.agent.tools.keys()
    app.tree_action({'action': 'new_conversation'})
    assert app.goals.binding('', '', 'assistant')['session_id'] != run.session_id
    assert not app.tree_context('', mode='assistant')['messages']
    assert app.session(run.session_id)['messages']  # old conversation is retained


@pytest.mark.parametrize('allowed', [True, False])
def test_one_message_can_plan_dispatch_and_request_real_approval(tmp_path, allowed):
    (tmp_path/'README.md').write_text('SOURCE_FACT: local evidence', encoding='utf-8')
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=ScriptedAssistant)
    before_session = app.goals.read()['assistant_session_id']
    run = app.task(app.submit({'mode': 'assistant', 'prompt': 'Organize this work', 'max_steps': 10})['id'])
    with run.condition:
        assert run.condition.wait_for(lambda: run.approval is not None or run.finished is not None, 20)
    assert run.approval is not None, run.error
    assert run.approval['name'] == 'confirm_task'
    app.approve(run.id, run.approval['id'], allowed)
    with run.condition:
        assert run.condition.wait_for(lambda: run.finished is not None, 20)
    assert run.status == 'completed', run.error
    state = app.goals.read()
    assert len(state['goals']) == len(state['tasks']) == 1
    goal, task = next(iter(state['goals'].values())), next(iter(state['tasks'].values()))
    assert goal['chat_session_id'] == before_session == run.session_id
    assert state['assistant_session_id'] != before_session
    assert run.binding['goal_id'] == goal['id']
    assert task['status'] == ('completed' if allowed else 'review')
    assert task['acceptance_origin'] == ('operator' if allowed else 'unverified')
    child = app.task(task['runs'][0]['id'])
    assert child.session_id != run.session_id
    assert child.agent.goal_tool_budget is run.agent.goal_tool_budget
    assert run.agent.goal_tool_budget['remaining'] == 5  # four coordinator calls + one child read
    assert 'SOURCE_FACT' in child.agent.history_text()
    assert any(e.get('delegated_task_id') == task['id'] for e in run.events)
    assert child.finished is not None and app.active_id is None


def test_shared_budget_rejects_late_effects(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient(['<final>Hi</final>']))
    run = complete(app, {'mode': 'assistant', 'prompt': 'Question'})
    run.agent.goal_tool_budget['remaining'] = 0
    before = app.goals.read()
    result = run.agent.execute_tool('goal_action', {'proposal': json.dumps({'action': 'create_goal', 'title': 'No budget'})})
    assert result.metadata['tool_error_code'] == 'shared_budget_exhausted'
    assert app.goals.read() == before


def test_native_coordinator_and_child_keep_separate_tool_call_pairs(tmp_path):
    (tmp_path/'README.md').write_text('NATIVE_SOURCE', encoding='utf-8')
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=NativeAssistant)
    run = complete(app, {'mode': 'assistant', 'prompt': 'Plan and inspect', 'policy': 'auto', 'max_steps': 10})
    assert run.status == 'completed', run.error
    task = next(iter(app.goals.read()['tasks'].values()))
    child = app.task(task['runs'][0]['id'])
    parent_conversation, child_conversation = run.agent.model_client.native_conversation, child.agent.model_client.native_conversation
    assert parent_conversation is not child_conversation
    assert parent_conversation.pending is None and child_conversation.pending is None
    assert len([m for m in parent_conversation.messages if m['role'] == 'tool']) == 4
    assert len([m for m in child_conversation.messages if m['role'] == 'tool']) == 1
    assert task['status'] == 'completed'
    assert 'goal_tree' in json.dumps(run.agent.model_client.sent[-1])


def test_agent_cannot_edit_foreign_goal_or_self_accept(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient(['<final>Hi</final>']))
    g = app.goals.action({'action': 'create_goal', 'title': 'Current'})
    foreign = app.goals.action({'action': 'create_goal', 'title': 'Foreign'})
    task = app.goals.action({'action': 'create_task', 'goal_id': foreign['id'], 'title': 'Foreign task'})
    run = complete(app, {'mode': 'assistant', 'goal_id': g['id'], 'prompt': 'Question'})
    before = app.goals.read()
    for proposal in [
        {'action': 'update_task', 'task_id': task['id'], 'revision': task['revision'], 'title': 'Corrupt'},
        {'action': 'accept_task', 'task_id': task['id'], 'reason': 'Fake approval'},
        {'action': 'select', 'goal_id': foreign['id']},
    ]:
        result = run.agent.execute_tool('goal_action', {'proposal': json.dumps(proposal)})
        assert result.metadata['tool_status'] != 'ok'
    assert app.goals.read() == before


def test_readonly_policy_cannot_plan_even_through_coordinator(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient(['<final>Read only</final>']))
    run = complete(app, {'mode': 'assistant', 'policy': 'never', 'prompt': 'Question'})
    before = app.goals.read()
    result = run.agent.execute_tool('goal_action', {'proposal': json.dumps({'action': 'create_goal', 'title': 'Denied'})})
    assert result.metadata['tool_status'] != 'ok'
    assert app.goals.read() == before


def test_goal_constraint_edit_preserves_unified_conversation_and_rotates_task_context(tmp_path):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient(['<final>Ready</final>']))
    g = app.goals.action({'action': 'create_goal', 'title': 'A'})
    t = app.goals.action({'action': 'create_task', 'goal_id': g['id'], 'title': 'Task'})
    run = complete(app, {'mode': 'assistant', 'goal_id': g['id'], 'prompt': 'Update constraints'})
    current = app.goals.read()['goals'][g['id']]
    result = run.agent.execute_tool('goal_action', {'proposal': json.dumps({'action': 'update_goal', 'revision': current['revision'], 'constraints': 'NEW_CONSTRAINT'})})
    assert result.metadata['tool_status'] == 'ok'
    state = app.goals.read()
    assert state['goals'][g['id']]['chat_session_id'] == run.session_id
    assert state['tasks'][t['id']]['session_id'] != t['session_id']
    assert run.agent.goal_context['goal']['constraints'] == 'NEW_CONSTRAINT'


def test_minimal_frontend_has_no_mode_or_tree_mutation_controls():
    from pathlib import Path
    html = Path('pico/web_assets/index.html').read_text(encoding='utf-8')
    assert '目标任务树' in html and 'workspace-select' in html
    for removed in ('interaction-mode', 'entity-dialog', 'plan-goal', 'add-root-task', '执行现场', '代码差异', 'A GOAL.', '让目标，逐步成为结果。'):
        assert removed not in html


@pytest.mark.parametrize('client', [ScriptedAssistant, NativeAssistant])
def test_child_budget_exhaustion_goes_directly_to_finalization(tmp_path, client):
    (tmp_path/'README.md').write_text('Budget source', encoding='utf-8')
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=client)
    run = complete(app, {'mode': 'assistant', 'prompt': 'Plan and read', 'policy': 'auto', 'max_steps': 4})
    task = next(iter(app.goals.read()['tasks'].values()))
    assert run.agent.goal_tool_budget['remaining'] == 0
    assert run.attempts == 4
    assert run.status == 'stopped' and run.stop_reason == 'step_limit_reached'
    assert task['status'] == 'review' and task['acceptance_origin'] == 'unverified'
    if getattr(run.agent.model_client, 'supports_native_tools', False):
        assert run.agent.model_client.native_conversation.pending is None


class DispatchClient(ScriptedAssistant):
    def complete(self, prompt, *args, **kwargs):
        self.step += 1
        self.prompts.append(prompt)
        scope = json.JSONDecoder().raw_decode(prompt.split('GoalTree current scoped task state:\n', 1)[1])[0]
        if self.step > 1:
            return '<final>Inspect the recorded tool outcome; manual acceptance is pending.</final>'
        if scope['mode'] == 'execute':
            return self.tool('write_file', {'path': 'output.txt', 'content': 'from approved child'})
        return self.tool('run_task', {'task_id': scope['existing_tasks'][0]['id'], 'instruction': 'Write output.txt after approval.'})


def existing_task(app):
    g = app.goals.action({'action': 'create_goal', 'title': 'Dispatch goal'})
    t = app.goals.action({'action': 'create_task', 'goal_id': g['id'], 'title': 'Delivery', 'verification_mode': 'manual'})
    return g, t


@pytest.mark.parametrize('allowed', [True, False])
def test_delegated_file_effect_keeps_child_approval_and_parent_audit(tmp_path, allowed):
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=DispatchClient, web_access=True)
    goal, task = existing_task(app)
    run = app.task(app.submit({'mode': 'assistant', 'goal_id': goal['id'], 'prompt': 'Deliver'})['id'])
    with run.condition:
        assert run.condition.wait_for(lambda: run.approval is not None or run.finished is not None, 20)
    assert run.approval['name'] == 'write_file'
    assert run.approval['args']['_task_id'] == task['id']
    app.approve(run.id, run.approval['id'], allowed)
    with run.condition:
        assert run.condition.wait_for(lambda: run.finished is not None, 20)
    assert (tmp_path/'output.txt').exists() is allowed
    dispatch = next(e for e in run.events if e.get('event') == 'tool_executed' and e.get('name') == 'run_task')
    assert dispatch['workspace_changed'] is allowed
    assert dispatch['read_only'] is not allowed
    if allowed:
        assert 'output.txt' in dispatch['source_paths']
    assert app.goals.read()['tasks'][task['id']]['status'] == 'review'
    assert not {'write_file', 'run_shell'} & run.agent.tools.keys()
    child=next(t for t in app.tasks.values() if t.id!=run.id)
    assert child.agent.web_budget is run.agent.web_budget


def test_stop_propagates_to_inflight_child_and_releases_workspace(tmp_path):
    entered = threading.Event()
    class PausedClient(DispatchClient):
        stream = False
        def complete(self, prompt, *args, **kwargs):
            scope = json.JSONDecoder().raw_decode(prompt.split('GoalTree current scoped task state:\n', 1)[1])[0]
            if scope['mode'] == 'execute':
                entered.set()
                assert self.cancellation.event.wait(10)
                self.cancellation.check()
            return super().complete(prompt, *args, **kwargs)
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=PausedClient)
    goal, task = existing_task(app)
    run = app.task(app.submit({'mode': 'assistant', 'goal_id': goal['id'], 'prompt': 'Deliver'})['id'])
    assert entered.wait(15)
    app.cancel(run.id)
    with run.condition:
        assert run.condition.wait_for(lambda: run.finished is not None, 15)
    assert run.status == 'stopped' and app.active_id is None
    current = app.goals.read()['tasks'][task['id']]
    assert current['status'] == 'blocked'
    assert app.task(current['runs'][0]['id']).status == 'stopped'
    assert not (tmp_path/'output.txt').exists()


def test_repeated_read_guard_is_run_scoped_but_still_blocks_same_run_loops(tmp_path):
    call = ScriptedAssistant.tool('read_file', {'path': 'source.txt'})
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=lambda: FakeModelClient([call, '<final>Read actual source</final>']))
    goal, task = existing_task(app)
    for version in range(3):
        (tmp_path/'source.txt').write_text('SOURCE_VERSION_' + str(version), encoding='utf-8')
        run = complete(app, {'goal_id': goal['id'], 'node_id': task['id'], 'mode': 'execute', 'prompt': 'Read current source', 'policy': 'never'})
        event = next(e for e in run.events if e['event'] == 'tool_executed')
        assert event['tool_status'] == 'ok'
        assert 'SOURCE_VERSION_' + str(version) in event['result']
    app.make_model = lambda: FakeModelClient([call, call, call, '<final>Stopped repeating</final>'])
    run = complete(app, {'goal_id': goal['id'], 'node_id': task['id'], 'mode': 'execute', 'prompt': 'Read once, avoid loops', 'policy': 'never'})
    assert any(e.get('tool_error_code') == 'repeated_identical_call' for e in run.events)


@pytest.mark.parametrize('prose', ['Detailed result ' * 1500, 'Visible detail ' + chr(1) * 15000], ids=['long-prose', 'escaped-control-characters'])
def test_long_child_prose_cannot_hide_verification_and_full_result_is_recoverable(tmp_path, prose):
    class LongClient(DispatchClient):
        def complete(self, prompt, *args, **kwargs):
            scope = json.JSONDecoder().raw_decode(prompt.split('GoalTree current scoped task state:\n', 1)[1])[0]
            if scope['mode'] == 'execute':
                return '<final>' + prose + '</final>'
            return super().complete(prompt, *args, **kwargs)
    app = Workbench(tmp_path, harness_profile='workbench', model_factory=LongClient)
    goal, task = existing_task(app)
    run = complete(app, {'mode': 'assistant', 'goal_id': goal['id'], 'prompt': 'Inspect result', 'policy': 'auto'})
    record = next(m for m in run.agent.session['history'] if m.get('role') == 'tool' and m.get('name') == 'run_task')
    response = json.loads(record['content'].split('\nEvidence ID:')[0])
    assert response['answer_truncated'] is True and 0 < len(response['answer']) <= 1200
    assert len(json.dumps(response, ensure_ascii=False)) <= 3500
    assert response['verification']['status'] == 'not_configured'
    full = run.agent.evidence.get(response['detail_evidence_id'], run.session_id)
    assert len(json.loads(full['content'])['answer']) > 10000
    child = app.task(response['id'])
    with pytest.raises(ValueError):
        child.agent.evidence.get(response['detail_evidence_id'], child.session_id)
