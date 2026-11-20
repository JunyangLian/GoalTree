"""Resumable Action/Observation and event-queue contracts."""
import json
from unittest.mock import patch

import pytest

from pico import AgentRun, EnvironmentProfile, FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.providers.clients import OpenAICompatibleModelClient
from test_native_tools import call, reply


PROFILE = EnvironmentProfile(name='external-steps', role='Operate the external environment.')


def agent(tmp_path, outputs, **kwargs):
    (tmp_path / 'README.md').write_text('demo\n', encoding='utf-8')
    return Pico(FakeModelClient(outputs), WorkspaceContext.build(tmp_path),
                SessionStore(tmp_path / '.pico/sessions'), environment=PROFILE,
                approval_policy='auto', max_depth=0, **kwargs)


def test_internal_stepwise_matches_legacy_ask(tmp_path):
    (tmp_path / 'hello.txt').write_text('alpha\n', encoding='utf-8')
    run = agent(tmp_path, [
        '<tool>{"name":"read_file","args":{"path":"hello.txt"}}</tool>',
        '<final>Done</final>',
    ]).start_run('Inspect')
    observation = run.step()
    assert observation.kind == 'observation'
    assert observation.actions[0]['name'] == 'read_file'
    assert 'alpha' in observation.observations[0]['content']
    final = run.step()
    assert final.kind == 'final' and final.answer == 'Done'
    assert run.done and run.final_answer == 'Done'


def test_external_action_is_not_executed_before_observation(tmp_path):
    run = agent(tmp_path, [
        '<tool>{"name":"read_file","args":{"path":"missing.txt"}}</tool>',
        '<final>External done</final>',
    ]).start_run('Read missing', execute_actions=False)
    action = run.step()
    assert action.kind == 'action'
    assert action.actions[0]['name'] == 'read_file'
    assert not any(item.get('role') == 'tool' for item in run.agent.session['history'])
    with pytest.raises(ValueError, match='needs an observation'):
        run.step()
    final = run.step({'content': 'externally supplied contents', 'metadata': None})
    assert final.kind == 'final' and final.answer == 'External done'
    tool = next(item for item in run.agent.session['history'] if item.get('role') == 'tool')
    assert tool['content'] == 'externally supplied contents'


def test_external_observation_count_and_unexpected_input_are_checked(tmp_path):
    run = agent(tmp_path, [
        '<tool>{"name":"read_file","args":{"path":"missing.txt"}}</tool>',
        '<final>Done</final>',
    ]).start_run('Read', execute_actions=False)
    with pytest.raises(ValueError, match='Unexpected observation'):
        run.step('not waiting yet')
    run.step()
    with pytest.raises(ValueError, match='Expected 1 observations, got 2'):
        run.step(['one', 'two'])
    final = run.step('one')
    assert final.kind == 'final' and final.answer == 'Done'


def test_event_injection_reaches_next_text_prompt_and_trace(tmp_path):
    run = agent(tmp_path, [
        '<tool>{"name":"read_file","args":{"path":"missing.txt"}}</tool>',
        '<final>Revised</final>',
    ]).start_run('Initial request', execute_actions=False)
    run.inject_event({'type': 'user_message', 'content': 'Before the first model request'})
    assert run.step().kind == 'action'
    run.inject_event({'type': 'user_message', 'content': 'Use the revised constraints'})
    assert run.step('observed').kind == 'final'
    client = run.agent.model_client
    assert 'Before the first model request' in client.prompts[0]
    assert 'Use the revised constraints' in client.prompts[1]
    trace = [json.loads(line) for line in run.agent.run_store.trace_path(run.current_task_state).read_text().splitlines()]
    assert [event['content'] for event in trace if event['event'] == 'environment_event'] == [
        'Before the first model request', 'Use the revised constraints',
    ]


def test_native_pending_call_is_closed_by_external_observation(tmp_path):
    client = OpenAICompatibleModelClient(
        'deepseek-flash', 'https://api.deepseek.com/v1', 'fake', .2, 30,
        api_format='chat_completions',
    )
    outputs = [
        reply([call('read_file', {'path': 'missing.txt'}, 'external-1')]),
        reply(content='Native external done'),
    ]

    class Response:
        headers = {'Content-Type': 'application/json'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(outputs.pop(0)).encode()

    agent = Pico(client, WorkspaceContext.build(tmp_path), SessionStore(tmp_path / '.pico/sessions'),
                 environment=PROFILE, wire_protocol='native', approval_policy='auto', max_depth=0)
    with patch('urllib.request.urlopen', return_value=Response()):
        run = agent.start_run('Read externally', execute_actions=False)
        action = run.step()
        assert action.kind == 'action'
        assert client.native_conversation.pending == 'external-1'
        final = run.step({'content': 'native external observation', 'metadata': {'tool_status': 'ok'}})
    assert final.kind == 'final' and final.answer == 'Native external done'
    assert client.native_conversation.pending is None
    tool_messages = [message for message in client.native_conversation.messages if message['role'] == 'tool']
    assert tool_messages[-1]['tool_call_id'] == 'external-1'
    assert tool_messages[-1]['content'] == 'native external observation'


def test_event_after_last_action_reaches_budget_finalization(tmp_path):
    run = agent(tmp_path, [
        '<tool>{"name":"read_file","args":{"path":"missing.txt"}}</tool>',
        '<final>Finalized with event</final>',
    ], max_steps=1).start_run('Initial request', execute_actions=False)
    assert run.step().kind == 'action'
    run.inject_event({'type': 'user_message', 'content': 'After the last action'})
    final = run.step('observed')
    assert final.kind == 'final' and final.answer == 'Finalized with event'
    assert 'After the last action' in run.agent.model_client.prompts[-1]


def test_external_observation_uses_shared_redaction_and_pagination(tmp_path, monkeypatch):
    secret = 'sk-test-1234567890-1234567890-1234567890'
    monkeypatch.setenv('PICO_OPENAI_API_KEY', secret)
    raw = ('a' * 3990) + secret + ('b' * 5000)
    run = agent(tmp_path, [
        '<tool>{"name":"read_file","args":{"path":"missing.txt"}}</tool>',
        '<final>Done</final>',
    ]).start_run('Read externally', execute_actions=False)
    assert run.step().kind == 'action'
    assert run.step(raw).kind == 'final'
    safe = run.agent.redact_text(raw)
    tool = next(item for item in run.agent.session['history'] if item.get('role') == 'tool')
    assert tool['content'].startswith('Output ID: out_')
    assert safe[:4000] in tool['content']
    assert secret not in tool['content']
    output_id = tool['content'].split('Output ID: ', 1)[1].split('\n', 1)[0]
    page = run.agent.tool_outputs.read(output_id, offset=4001, limit=4000)
    assert page['text'] == safe[4000:8000]
    assert page['next_offset'] == 8001
    trace = [json.loads(line) for line in run.agent.run_store.trace_path(run.current_task_state).read_text().splitlines()]
    event = next(item for item in trace if item['event'] == 'tool_executed')
    assert event['external_observation'] is True
    assert event['output_total_chars'] == len(safe)


def test_close_closes_native_pending_and_persists_cancelled_outcome(tmp_path):
    client = OpenAICompatibleModelClient(
        'deepseek-flash', 'https://api.deepseek.com/v1', 'fake', .2, 30,
        api_format='chat_completions',
    )
    responses = [reply([call('read_file', {'path': 'missing.txt'}, 'close-1')])]

    class Response:
        headers = {'Content-Type': 'application/json'}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self):
            return json.dumps(responses.pop(0)).encode()

    agent = Pico(client, WorkspaceContext.build(tmp_path), SessionStore(tmp_path / '.pico/sessions'),
                 environment=PROFILE, wire_protocol='native', approval_policy='auto', max_depth=0)
    with patch('urllib.request.urlopen', return_value=Response()):
        run = agent.start_run('Read externally', execute_actions=False)
        assert run.step().kind == 'action'
        assert client.native_conversation.pending == 'close-1'
        run.close()
        run.close()
    assert client.native_conversation.pending is None
    assert run.current_task_state.status == 'stopped'
    assert run.current_task_state.stop_reason == 'cancelled'
    assert run.current_task_state.task_outcome == 'incomplete'
    assert run.step().kind == 'closed'
    report = agent.run_store.load_report(run.current_task_state.run_id)
    assert report['status'] == 'stopped' and report['stop_reason'] == 'cancelled'


def test_stepwise_public_entry_preserves_workbench_boundary(tmp_path):
    run_agent = agent(tmp_path, [], harness_profile='workbench')
    with pytest.raises(ValueError, match='require harness_profile=classic'):
        run_agent.start_run('not enabled')
