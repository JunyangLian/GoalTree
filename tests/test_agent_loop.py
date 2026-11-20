import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.agent_loop import AgentLoop


def build_agent(tmp_path, outputs, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        **kwargs,
    )


def test_agent_loop_runs_same_control_flow_as_pico_ask(tmp_path):
    (tmp_path / "hello.txt").write_text("alpha\n", encoding="utf-8")
    agent = build_agent(
        tmp_path,
        [
            '<tool>{"name":"read_file","args":{"path":"hello.txt","start":1,"end":1}}</tool>',
            "<final>Done.</final>",
        ],
    )

    answer = AgentLoop(agent).run("Inspect hello.txt")

    assert answer == "Done."
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.task_outcome == "unknown"
    assert agent.current_task_state.resource_exhausted == ""
    assert agent.run_store.report_path(agent.current_task_state.run_id).exists()


def test_pico_ask_delegates_to_agent_loop(tmp_path):
    agent = build_agent(tmp_path, ["<final>Facade works.</final>"])

    assert agent.ask("Use facade") == "Facade works."


def test_agent_loop_reserves_a_final_answer_after_tool_budget_is_exhausted(tmp_path):
    (tmp_path / "facts.txt").write_text("one\ntwo\nthree\nfour\nfive\nsix\n", encoding="utf-8")
    tool_outputs = [
        f'<tool>{{"name":"read_file","args":{{"path":"facts.txt","start":{line},"end":{line}}}}}</tool>'
        for line in range(1, 7)
    ]
    agent = build_agent(
        tmp_path,
        [*tool_outputs, "<final>All six facts were inspected.</final>"],
        max_steps=6,
    )

    answer = agent.ask("Inspect all facts and summarize them")

    assert answer == "All six facts were inspected."
    assert agent.current_task_state.status == "completed"
    assert agent.current_task_state.task_outcome == "unknown"
    assert agent.current_task_state.resource_exhausted == "tool_budget"
    assert agent.current_task_state.tool_steps == 6
    assert agent.current_task_state.attempts == 7
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["task_outcome"] == "unknown"
    assert report["resource_exhausted"] == "tool_budget"
    assert report["task_state"]["task_outcome"] == "unknown"
    trace_path = agent.run_store.trace_path(agent.current_task_state)
    trace_events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert any(
        event["event"] == "model_requested" and event.get("purpose") == "finalization"
        for event in trace_events
    )
    finished = next(event for event in reversed(trace_events) if event["event"] == "run_finished")
    assert finished["task_outcome"] == "unknown"
    assert finished["resource_exhausted"] == "tool_budget"


def test_agent_loop_persists_model_failure_before_reraising(tmp_path):
    class FailingModelClient:
        supports_prompt_cache = False
        last_completion_metadata = {
            "stop_reason": "max_tokens",
            "content_block_types": ["thinking"],
        }

        def complete(self, *args, **kwargs):
            raise RuntimeError(
                "Anthropic-compatible response ended before a text block "
                "(stop_reason=max_tokens, content_types=thinking)"
            )

    agent = build_agent(tmp_path, [])
    agent.model_client = FailingModelClient()

    with pytest.raises(RuntimeError, match="ended before a text block"):
        agent.ask("Inspect the tests")

    state = agent.current_task_state
    assert state.status == "failed"
    assert state.stop_reason == "model_error"
    assert state.attempts == 1
    assert agent.run_store.task_state_path(state).exists()
    assert agent.run_store.report_path(state).exists()
    report = agent.run_store.load_report(state.run_id)
    assert report["stop_reason"] == "model_error"
    assert report["task_outcome"] == "failed"
    assert report["resource_exhausted"] == ""
    assert report["prompt_metadata"]["stop_reason"] == "max_tokens"
    trace_events = [json.loads(line) for line in agent.run_store.trace_path(state).read_text(encoding="utf-8").splitlines()]
    finished = next(event for event in reversed(trace_events) if event["event"] == "run_finished")
    assert finished["task_outcome"] == "failed"
    assert finished["resource_exhausted"] == ""


def test_budget_finalization_with_passed_verification_is_completed(tmp_path):
    command = 'python -B -c "pass"'
    verified_call = '<tool>' + json.dumps({
        'name': 'run_shell', 'args': {'command': command, 'timeout': 20},
    }) + '</tool>'
    agent = build_agent(
        tmp_path, [verified_call, '<final>Verified.</final>'],
        max_steps=1, verification_command=command,
    )

    assert agent.ask('Verify current state') == 'Verified.'
    state = agent.current_task_state
    assert state.status == 'completed'
    assert state.stop_reason == 'final_answer_returned'
    assert state.task_outcome == 'completed'
    assert state.resource_exhausted == 'tool_budget'
    report = agent.run_store.load_report(state.run_id)
    assert report['task_outcome'] == 'completed'
    assert report['resource_exhausted'] == 'tool_budget'


def test_budget_finalization_without_valid_final_is_incomplete(tmp_path):
    (tmp_path / 'facts.txt').write_text('fact\n', encoding='utf-8')
    call = '<tool>' + json.dumps({
        'name': 'read_file', 'args': {'path': 'facts.txt', 'start': 1, 'end': 1},
    }) + '</tool>'
    agent = build_agent(tmp_path, [call, call], max_steps=1)

    answer = agent.ask('Inspect the fact')
    state = agent.current_task_state
    assert answer == 'Stopped after reaching the step limit without a final answer.'
    assert state.status == 'stopped'
    assert state.stop_reason == 'step_limit_reached'
    assert state.task_outcome == 'incomplete'
    assert state.resource_exhausted == 'tool_budget'
    report = agent.run_store.load_report(state.run_id)
    assert report['task_outcome'] == 'incomplete'
    assert report['resource_exhausted'] == 'tool_budget'
