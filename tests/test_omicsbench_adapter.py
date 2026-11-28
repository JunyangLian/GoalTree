"""Offline tests for the OmicsBench adapter: rendering, no-leak invariants,
answer classification, and a mock end-to-end through GoalTree's stepwise loop."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext

from benchmarks.omicsbench.adapter import OmicsBenchAdapter, classify_answer
from benchmarks.omicsbench.env import MockEnvironment
from benchmarks.omicsbench.prompt_renderer import (
    derive_output_schema,
    render_first_node_prompt,
    render_node_prompt,
)
from benchmarks.omicsbench.question import load_question


SYNTH_QUESTION = {
    "id": "synth_demo",
    "version": "0.1",
    "description": "Synthetic task for offline adapter tests.",
    "difficulty": None,
    "data": [
        {
            "name": "expr",
            "description": "fake expression table",
            "format": "csv.gz",
            "data_modalities": ["bulk-rna-seq"],
            "organisms": ["Mus musculus"],
            "platforms": [],
            "uri": "../data/expr.csv.gz",
            "checksum": None,
        }
    ],
    "nodes": [
        {
            "id": "n_de",
            "type": "differential-expression",
            "depends_on": [],
            "instruction": "Compare group A and B on log2(count+1) without normalization; "
            "report only depot-biased markers at p<0.05 grouped by direction.",
            "grading": {
                "output_filename": "markers.json",
                "aggregation": "mean",
                "graders": [
                    {
                        "name": "marker_direction",
                        "type": "grouped_entity_set",
                        "parameters": {
                            "group_match": "case_insensitive",
                            "item_match": "gene",
                            "organism": "Mus musculus",
                            "item_beta": 1.0,
                        },
                        "ground_truth": [
                            {"group": "higher in A", "items": ["GENE_ALPHA", "GENE_BETA"]},
                            {"group": "higher in B", "items": ["GENE_GAMMA"]},
                        ],
                    }
                ],
            },
        },
        {
            "id": "n_origin",
            "type": "interpretation",
            "depends_on": ["n_de"],
            "instruction": "Choose the best-supported origin among the competing hypotheses.",
            "grading": {
                "output_filename": "origin.json",
                "aggregation": "mean",
                "graders": [
                    {
                        "name": "origin",
                        "type": "multiple_choice",
                        "parameters": {
                            "choices": ["progenitor X", "progenitor Y", "progenitor Z"],
                            "string_match": "case_insensitive",
                        },
                        "ground_truth": "progenitor Z",
                    }
                ],
            },
        },
    ],
}


@pytest.fixture
def synth_question(tmp_path) -> "object":
    path = tmp_path / "synth_demo.json"
    path.write_text(json.dumps(SYNTH_QUESTION), encoding="utf-8")
    return load_question(path)


def test_load_question_topology_and_nodes(synth_question):
    assert synth_question.id == "synth_demo"
    assert synth_question.topological_order() == ["n_de", "n_origin"]
    assert synth_question.node("n_de").grading.output_filename == "markers.json"
    assert synth_question.node("n_origin").depends_on == ["n_de"]


def test_output_schema_shows_shape_not_values(synth_question):
    node = synth_question.node("n_de")
    schema = derive_output_schema(node.grading.graders)
    assert '"marker_direction"' in schema
    # Leaves are placeholders; no ground-truth items leak.
    assert "GENE_ALPHA" not in schema
    assert "GENE_BETA" not in schema
    assert "GENE_GAMMA" not in schema
    assert '"<string>"' in schema


def test_grouped_labels_revealed_but_items_not(synth_question):
    node = synth_question.node("n_de")
    prompt = render_node_prompt(node)
    assert "higher in A" in prompt  # group label is prompt-visible, per official renderer
    assert "higher in B" in prompt
    assert "GENE_ALPHA" not in prompt  # items never leak
    assert "GENE_BETA" not in prompt
    assert "GENE_GAMMA" not in prompt


def test_choice_options_from_parameters_not_ground_truth(synth_question):
    node = synth_question.node("n_origin")
    prompt = render_node_prompt(node)
    for choice in ["progenitor X", "progenitor Y", "progenitor Z"]:
        assert choice in prompt  # all choices are listed (author-provided)


def test_first_node_includes_system_and_data(synth_question):
    prompt = render_first_node_prompt(synth_question, synth_question.node("n_de"))
    assert "computational biologist" in prompt
    assert "Files available in /data/:" in prompt


def test_render_does_not_include_future_node(synth_question):
    # Rendering n_de (order 0) must not mention the dependent node n_origin.
    prompt = render_first_node_prompt(synth_question, synth_question.node("n_de"))
    assert "best-supported origin" not in prompt
    assert "origin.json" not in prompt
    assert "progenitor X" not in prompt


def test_classify_answer_outcomes(synth_question):
    node = synth_question.node("n_de")
    assert classify_answer(node, None)[0] == "file_missing"
    assert classify_answer(node, "not json")[0] == "invalid_json"
    assert classify_answer(node, "[1,2]")[0] == "schema_mismatch"  # top-level not dict
    assert classify_answer(node, '{"wrong": []}')[0] == "schema_mismatch"  # wrong key
    # Value must be a list for grouped_entity_set; a string is a mismatch.
    assert classify_answer(node, '{"marker_direction": "oops"}')[0] == "schema_mismatch"
    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    assert classify_answer(node, json.dumps(good))[0] == "valid"
    # Scalar category (multiple_choice): an object/list is a mismatch.
    origin = synth_question.node("n_origin")
    assert classify_answer(origin, '{"origin": ["progenitor Z"]}')[0] == "schema_mismatch"
    assert classify_answer(origin, '{"origin": "progenitor Z"}')[0] == "valid"
    # Official parity: a repeated top-level key is a schema mismatch, and the
    # non-finite tokens json.loads accepts by default are invalid JSON there.
    assert classify_answer(node, '{"marker_direction": [], "marker_direction": []}')[0] == "schema_mismatch"
    assert classify_answer(node, '{"marker_direction": NaN}')[0] == "invalid_json"
    assert classify_answer(node, '{"marker_direction": Infinity}')[0] == "invalid_json"


def _agent(tmp_path, outputs):
    return Pico(
        FakeModelClient(outputs),
        WorkspaceContext.build(tmp_path),
        SessionStore(tmp_path / ".pico/sessions"),
        approval_policy="auto",
        max_depth=0,
    )


def test_end_to_end_mock_agent_writes_valid_answer(tmp_path, synth_question):
    node = synth_question.node("n_de")
    answer = {"marker_direction": [
        {"group": "higher in A", "items": ["GENE_ALPHA", "GENE_BETA"]},
        {"group": "higher in B", "items": ["GENE_GAMMA"]},
    ]}
    tool = "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(answer)},
    }) + "</tool>"
    agent = _agent(tmp_path, [tool, "<final>done</final>"])

    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node)

    assert sub.outcome == "valid"
    assert sub.answer == answer
    # Early exit: the model's <final> is not consumed once the answer file is valid.
    assert sub.final_answer is None
    # The observation came from real (mock) tool execution, recorded on the env.
    assert any(c["name"] == "write_file" for c in env.tool_calls)


def test_run_question_drives_all_nodes_without_future_leak(tmp_path, synth_question):
    markers = {"marker_direction": [
        {"group": "higher in A", "items": ["GENE_ALPHA"]},
        {"group": "higher in B", "items": ["GENE_GAMMA"]},
    ]}
    origin = {"origin": "progenitor Z"}
    # Under early-exit, each node consumes exactly one write action; the model's
    # trailing <final> is never requested.
    outputs = [
        "<tool>" + json.dumps({"name": "write_file", "args": {
            "path": "/workspace/answers/markers.json", "content": json.dumps(markers)}}) + "</tool>",
        "<tool>" + json.dumps({"name": "write_file", "args": {
            "path": "/workspace/answers/origin.json", "content": json.dumps(origin)}}) + "</tool>",
    ]
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    subs = adapter.run_question(lambda: _agent(tmp_path, outputs))

    assert [s.node_id for s in subs] == ["n_de", "n_origin"]
    assert [s.outcome for s in subs] == ["valid", "valid"]
    assert subs[1].answer == origin


def test_run_question_scopes_tool_calls_per_node(tmp_path, synth_question):
    markers = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    origin = {"origin": "progenitor Z"}
    outputs = [
        "<tool>" + json.dumps({"name": "write_file", "args": {
            "path": "/workspace/answers/markers.json", "content": json.dumps(markers)}}) + "</tool>",
        "<tool>" + json.dumps({"name": "write_file", "args": {
            "path": "/workspace/answers/origin.json", "content": json.dumps(origin)}}) + "</tool>",
    ]
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    subs = adapter.run_question(lambda: _agent(tmp_path, outputs))
    # Each node's tool-call log must be its own slice, not the shared session prefix.
    assert [len(s.tool_calls) for s in subs] == [1, 1]
    assert subs[0].tool_calls[0]["args"]["path"].endswith("markers.json")
    assert subs[1].tool_calls[0]["args"]["path"].endswith("origin.json")


def test_drive_node_targeted_recovery_persists_answer(tmp_path, synth_question):
    node = synth_question.node("n_de")
    answer = {"marker_direction": [
        {"group": "higher in A", "items": ["GENE_ALPHA", "GENE_BETA"]},
        {"group": "higher in B", "items": ["GENE_GAMMA"]},
    ]}
    write_tool = "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(answer)},
    }) + "</tool>"
    outputs = [
        "<final>I computed it but forgot to write the file</final>",  # file_missing
        write_tool,                                                     # recovery write
        "<final>done</final>",
    ]
    agent = _agent(tmp_path, outputs)
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                             recovery_attempts=1)
    assert sub.outcome == "valid"
    assert sub.answer == answer


def test_drive_node_early_exits_once_answer_file_is_valid(tmp_path, synth_question):
    node = synth_question.node("n_de")
    answer = {"marker_direction": [
        {"group": "higher in A", "items": ["GENE_ALPHA"]},
        {"group": "higher in B", "items": ["GENE_GAMMA"]},
    ]}
    write_tool = "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(answer)},
    }) + "</tool>"
    outputs = [write_tool, "<final>should not be consumed</final>"]
    agent = _agent(tmp_path, outputs)
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node)
    assert sub.outcome == "valid"
    # Early exit: the trailing <final> was never requested from the model.
    assert agent.model_client.outputs == ["<final>should not be consumed</final>"]


def test_drive_node_scientific_detects_method_drift(tmp_path, synth_question):
    import goaltree.scientific as sci

    node = synth_question.node("n_de")
    # The model executes a command using adjusted p (BH) -> violates "p<0.05" unadjusted.
    drift = "<tool>" + json.dumps({
        "name": "run_shell", "args": {"command": "padj = p.adjust(p, method='BH')"},
    }) + "</tool>"
    agent = _agent(tmp_path, [drift, "<final>done</final>"])
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                             recovery_attempts=0, scientific=sci)
    assert sub.contract is not None
    assert any(r.status == "VIOLATION" for r in sub.verification)


def test_recovery_prompt_includes_scientific_correction(synth_question):
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    msg = adapter._recovery_prompt(
        synth_question.node("n_de"),
        "Scientific validation detected a mismatch in this attempt.",
    )
    assert "Scientific validation detected a mismatch" in msg
    assert "markers.json" in msg


def test_scientific_stages_are_independent(tmp_path, synth_question):
    from benchmarks.omicsbench.adapter import ScientificStages

    node = synth_question.node("n_de")
    answer = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(answer)},
    }) + "</tool>"

    # constraints only: contract compiled, verifier NOT run.
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(_agent(tmp_path, [write]),
                             render_first_node_prompt(synth_question, node), node,
                             scientific=ScientificStages(constraints=True))
    assert sub.contract is not None
    assert sub.verification == []


def test_host_environment_linux_hint(tmp_path):
    from benchmarks.omicsbench.env import HostEnvironment

    e = HostEnvironment(tmp_path / "data", tmp_path / "work")
    assert "Linux shell tool" in e._linux_hint("zcat /data/x.csv.gz")
    assert "Linux shell tool" in e._linux_hint("ls /data")
    assert "Linux shell tool" in e._linux_hint("cat > x.py << 'EOF'")
    assert "heredoc" in e._linux_hint("python - << 'EOF'")
    # Python passes through (it is the sanctioned way), as does a Windows-legal command.
    assert e._linux_hint("python -c 'print(1)'") is None
    assert e._linux_hint("python -c \"import pandas as pd\"") is None


def test_host_environment_translates_bare_paths(tmp_path):
    from benchmarks.omicsbench.env import HostEnvironment

    data = tmp_path / "dd"
    work = tmp_path / "ww"
    e = HostEnvironment(data, work)
    translated = e._translate("os.listdir('/data'); os.listdir('/workspace')")
    assert "'/data'" not in translated
    assert "'/workspace'" not in translated
    assert data.as_posix() in translated
    assert work.as_posix() in translated


def test_wrapup_nudge_after_many_shell_calls(tmp_path, synth_question):
    node = synth_question.node("n_de")
    shell = lambda i: "<tool>" + json.dumps({
        "name": "run_shell", "args": {"command": f"cmd{i}"},
    }) + "</tool>"
    answer = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(answer)},
    }) + "</tool>"
    outputs = [shell(i) for i in range(3)] + [write, "<final>done</final>"]
    agent = _agent(tmp_path, outputs)
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                             wrapup_after_tool_calls=3)
    assert sub.outcome == "valid"
    assert any("write your final answer" in p for p in agent.model_client.prompts)


def test_write_file_empty_args_returns_clear_error(tmp_path):
    from benchmarks.omicsbench.env import HostEnvironment, MockEnvironment

    for env in (MockEnvironment(), HostEnvironment(tmp_path / "d", tmp_path / "w")):
        assert "path" in env.execute("write_file", {})
        assert "content" in env.execute("write_file", {"path": "/workspace/answers/x.json"})


def test_recovery_prompt_schema_mismatch_lists_expected_keys(synth_question):
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    msg = adapter._recovery_prompt(synth_question.node("n_de"), outcome="schema_mismatch")
    assert "strict JSON object" in msg
    assert "marker_direction" in msg
    # file_missing prompt emphasizes write_file args
    msg2 = adapter._recovery_prompt(synth_question.node("n_de"), outcome="file_missing")
    assert "non-empty 'path'" in msg2


def test_recovery_prompt_schema_mismatch_shows_actual_vs_expected_keys(synth_question):
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    node = synth_question.node("n_de")
    # Wrong top-level key: the prompt names both what the model wrote and what is expected.
    msg = adapter._recovery_prompt(node, outcome="schema_mismatch",
                                   parsed={"markers": ["GENE_ALPHA"]})
    assert "'markers'" in msg
    assert "'marker_direction'" in msg
    assert "wrong top-level keys" in msg


def test_recovery_prompt_schema_mismatch_non_object_top_level(synth_question):
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    node = synth_question.node("n_de")
    msg = adapter._recovery_prompt(node, outcome="schema_mismatch", parsed=["GENE_ALPHA"])
    assert "strict JSON object" in msg
    assert "list" in msg  # names the actual top-level type


def test_recovery_prompt_schema_mismatch_right_keys_wrong_shape(synth_question):
    """Wrong value shape -> recovery shows the required structure (placeholders)."""
    from benchmarks.omicsbench.adapter import OmicsBenchAdapter
    from benchmarks.omicsbench.env import MockEnvironment

    node = synth_question.node("n_de")
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    # Real regression: a gene->label dict where a list of groups was required.
    msg = adapter._recovery_prompt(node, outcome="schema_mismatch",
                                   parsed={"marker_direction": {"GENE_ALPHA": "higher in A"}})
    assert "The required structure is:" in msg
    assert "<string>" in msg
    assert "GENE_ALPHA" not in msg  # never leaks answer values
    msg = adapter._recovery_prompt(node, outcome="schema_mismatch",
                                   parsed={"marker_direction": "oops"})
    assert "right top-level keys" in msg
    assert "wrong type or shape" in msg


def test_drift_recovery_loop_corrects_method(tmp_path, synth_question):
    from benchmarks.omicsbench.adapter import ScientificStages

    node = synth_question.node("n_de")  # prompt has "p<0.05" (unadjusted)
    drift = "<tool>" + json.dumps({
        "name": "run_shell", "args": {"command": "padj = p.adjust(p, method='BH')"},
    }) + "</tool>"
    wrong = {"marker_direction": [{"group": "higher in A", "items": ["GENE_WRONG"]}]}
    correct = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = lambda ans: "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(ans)},
    }) + "</tool>"

    agent = _agent(tmp_path, [drift, write(wrong), write(correct)])
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                             scientific=ScientificStages(True, True, True), recovery_attempts=1)
    assert sub.outcome == "valid"
    # The method drift was detected and the recovery produced the corrected answer.
    assert sub.answer == correct


def test_drive_node_records_per_node_token_usage(tmp_path, synth_question):
    node = synth_question.node("n_de")
    answer = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(answer)},
    }) + "</tool>"

    class UsageFake(FakeModelClient):
        def __init__(self, outputs):
            super().__init__(outputs)
            self._usage_totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0}
            self.last_completion_metadata = {"input_tokens": 120, "output_tokens": 30, "cached_tokens": 40}

        def complete(self, prompt, max_new_tokens, **kwargs):
            result = super().complete(prompt, max_new_tokens, **kwargs)
            self._usage_totals["input_tokens"] += 120
            self._usage_totals["output_tokens"] += 30
            self._usage_totals["cached_tokens"] += 40
            self._usage_totals["calls"] += 1
            return result

    agent = Pico(UsageFake([write, "<final>done</final>"]),
                 WorkspaceContext.build(tmp_path),
                 SessionStore(tmp_path / ".pico/sessions"),
                 approval_policy="auto", max_depth=0)
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node)
    assert sub.usage is not None
    assert sub.usage["input_tokens"] == 120
    assert sub.usage["output_tokens"] == 30
    assert sub.usage["cached_tokens"] == 40
    assert sub.usage["calls"] == 1


def test_usage_delta_none_when_client_untracked(tmp_path, synth_question):
    node = synth_question.node("n_de")
    answer = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = "<tool>" + json.dumps({
        "name": "write_file",
        "args": {"path": "/workspace/answers/markers.json", "content": json.dumps(answer)},
    }) + "</tool>"
    agent = _agent(tmp_path, [write])
    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node)
    assert sub.usage is None  # FakeModelClient has no _usage_totals hook


def test_make_usage_tracker_and_sum():
    from benchmarks.omicsbench.experiment import _make_usage_tracker, _sum_usage

    class Fake:
        def __init__(self):
            self.last_completion_metadata = {}

        def complete(self, *args, **kwargs):
            self.last_completion_metadata = {"input_tokens": 10, "output_tokens": 5, "cached_tokens": 2}
            return "ok"

    client = Fake()
    _make_usage_tracker(client)
    client.complete("a", 8)
    client.complete("b", 8)
    assert client._usage_totals == {"input_tokens": 20, "output_tokens": 10, "cached_tokens": 4, "calls": 2}
    one = {"input_tokens": 20, "output_tokens": 10, "cached_tokens": 4, "calls": 2}
    assert _sum_usage([one, None]) == one
    assert _sum_usage([None, None]) is None
    assert _sum_usage([]) is None


def test_observed_execution_text_scopes_to_node_calls(synth_question):
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    # A prior node's drift command already sits on the shared environment log.
    adapter.environment.tool_calls.append(
        {"name": "run_shell", "args": {"command": "padj = p.adjust(p, method='BH')"}})
    node_calls = [
        {"name": "run_shell", "args": {"command": "python run_scipy_ttest.py"}},
        {"name": "write_file", "args": {"path": "/workspace/answers/markers.json"}},
    ]
    text = adapter._observed_execution_text(node_calls)
    assert "padj" not in text  # previous node's drift must not leak into this node
    assert "run_scipy_ttest" in text


def test_drive_node_clears_stale_answer_before_start(tmp_path, synth_question):
    node = synth_question.node("n_de")
    stale = json.dumps({"marker_direction": [
        {"group": "higher in A", "items": ["GENE_ALPHA", "GENE_BETA"]},
        {"group": "higher in B", "items": ["GENE_GAMMA"]},
    ]})
    env = MockEnvironment()
    env.files["/workspace/answers/markers.json"] = stale  # leftover from a prior run
    # This run only shells around and never writes the answer file.
    outputs = [
        "<tool>" + json.dumps({"name": "run_shell", "args": {"command": "echo hi"}}) + "</tool>",
        "<final>done</final>",
    ]
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(_agent(tmp_path, outputs), render_first_node_prompt(synth_question, node),
                             node, recovery_attempts=0)
    assert sub.outcome == "file_missing"  # stale file must NOT count as this run's answer
    assert env.read_answer("markers.json") is None


def test_run_question_marks_previous_failure_in_next_prompt(tmp_path, synth_question, monkeypatch):
    import benchmarks.omicsbench.adapter as ad

    captured = []
    real = ad.render_node_prompt

    def spy(node, **kw):
        captured.append((node.id, kw))
        return real(node, **kw)

    monkeypatch.setattr(ad, "render_node_prompt", spy)
    # Neither node writes an answer, so node1 fails and node2 must be warned.
    outputs = ["<final>done</final>", "<final>done</final>"]
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    adapter.run_question(lambda: _agent(tmp_path, outputs), recovery_attempts=0)
    kw = dict(captured)["n_origin"]
    assert kw["previous_retried"] is True
    assert kw["previous_all_failed"] is True
    assert kw["previous_node_id"] == "n_de"


def test_step_loop_survives_model_exception(tmp_path, synth_question):
    node = synth_question.node("n_de")
    # One action, then the fake model is exhausted and raises mid-step.
    outputs = ['<tool>' + json.dumps({"name": "run_shell", "args": {"command": "echo hi"}}) + '</tool>']
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(_agent(tmp_path, outputs), render_first_node_prompt(synth_question, node),
                             node, recovery_attempts=0)
    assert sub.outcome == "error"  # transport error surfaced, node did not crash the sweep
    assert sub.error and "fake model ran out of outputs" in sub.error


def test_step_stops_after_valid_answer_within_action_batch(tmp_path, synth_question):
    from pico.stepwise import AgentStep

    node = synth_question.node("n_de")
    good = {"marker_direction": [
        {"group": "higher in A", "items": ["GENE_ALPHA", "GENE_BETA"]},
        {"group": "higher in B", "items": ["GENE_GAMMA"]},
    ]}
    a1 = {"name": "write_file", "args": {"path": "/workspace/answers/markers.json",
                                         "content": json.dumps(good)}}
    a2 = {"name": "write_file", "args": {"path": "/workspace/answers/markers.json",
                                         "content": "{not json"}}  # would clobber a1

    class FakeRun:
        def __init__(self):
            self.final_answer = None
            self.closed = False
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                return AgentStep("action", actions=(a1, a2), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            self.closed = True

        def inject_event(self, event):
            pass

    class FakeAgent:
        def start_run(self, prompt, execute_actions=False):
            return FakeRun()

    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(FakeAgent(), render_first_node_prompt(synth_question, node),
                             node, recovery_attempts=0)
    assert sub.outcome == "valid"
    assert len(sub.tool_calls) == 1  # the clobbering second write must not have run
    assert json.loads(env.read_answer("markers.json")) == good


def test_load_official_public_question_if_present():
    upstream = Path(__file__).resolve().parents[2] / "tmp" / "omicsbench_upstream" / "questions" / "bovine_imf_sf_preadipocyte_origin.json"
    # Fall back to the workspace tmp location.
    if not upstream.exists():
        upstream = Path("d:/1_yanjiusheng/实习项目/agent/tmp/omicsbench_upstream/questions/bovine_imf_sf_preadipocyte_origin.json")
    if not upstream.exists():
        pytest.skip("official question not downloaded")
    q = load_question(upstream)
    assert q.id == "bovine_imf_sf_preadipocyte_origin"
    assert len(q.nodes) == 3
    assert q.topological_order() == [
        "preadipocyte_marker_divergence",
        "lineage_regulator_selection",
        "imf_preadipocyte_origin",
    ]


def test_real_question_schema_and_options_no_ground_truth_leak():
    from benchmarks.omicsbench.prompt_renderer import (
        derive_group_label_options,
    )

    upstream = Path(__file__).resolve().parents[2] / "tmp" / "omicsbench_upstream" / "questions" / "bovine_imf_sf_preadipocyte_origin.json"
    if not upstream.exists():
        upstream = Path("d:/1_yanjiusheng/实习项目/agent/tmp/omicsbench_upstream/questions/bovine_imf_sf_preadipocyte_origin.json")
    if not upstream.exists():
        pytest.skip("official question not downloaded")
    q = load_question(upstream)

    # node1 grouped_entity_set: grouped *items* (which genes belong where) never leak.
    n1 = q.node("preadipocyte_marker_divergence")
    schema1 = derive_output_schema(n1.grading.graders)
    for gene in ("PDGFRA", "CD34", "PPARG"):
        assert gene not in schema1
    labels1 = json.dumps(derive_group_label_options(n1.grading.graders))
    assert "higher in IMF" in labels1 and "higher in SF" in labels1  # labels are prompt-visible
    for gene in ("PDGFRA", "CD34", "PPARG"):
        assert gene not in labels1

    # node2 multi_select: the schema is a placeholder list, not the literal answer.
    n2 = q.node("lineage_regulator_selection")
    schema2 = derive_output_schema(n2.grading.graders)
    assert '"regulators_up"' in schema2
    for gene in ("MYOD1", "MYOG", "MYF5"):
        assert gene not in schema2

    # node3 multiple_choice: the schema shows a placeholder, not the answer value.
    n3 = q.node("imf_preadipocyte_origin")
    schema3 = derive_output_schema(n3.grading.graders)
    assert '"origin": "<string>"' in schema3
    assert "satellite (myogenic) cells" not in schema3


def test_run_question_isolates_node_exception(tmp_path, synth_question):
    """One node's setup failure must not abort the remaining nodes."""
    from pico.stepwise import AgentStep

    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": json.dumps(good)}}

    class FakeRun:
        final_answer = None

        def __init__(self):
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                return AgentStep("action", actions=(write,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class FlakyAgent:
        def __init__(self):
            self.starts = 0

        def start_run(self, prompt, execute_actions=False):
            self.starts += 1
            if self.starts > 1:
                raise RuntimeError("synthetic env failure")
            return FakeRun()

    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    subs = adapter.run_question(lambda: FlakyAgent())

    assert subs[0].outcome == "valid"
    assert subs[1].node_id == "n_origin"
    assert subs[1].outcome == "error"
    assert "synthetic env failure" in subs[1].error


def test_step_loop_stops_before_actions_when_deadline_passed(tmp_path, synth_question, monkeypatch):
    """A model step that outlasts the wall budget must not start its actions."""
    from pico.stepwise import AgentStep

    import benchmarks.omicsbench.adapter as adapter_mod

    node = synth_question.node("n_de")
    clock = {"t": 0.0}
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock["t"])

    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": "{}"}}

    class SlowRun:
        final_answer = None

        def step(self, observation=None):
            clock["t"] += 10.0  # the step outlasts the 1s node budget
            return AgentStep("action", actions=(write,), task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class SlowAgent:
        def start_run(self, prompt, execute_actions=False):
            return SlowRun()

    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(SlowAgent(), render_first_node_prompt(synth_question, node),
                             node, wall_timeout=1.0, recovery_attempts=0)

    assert sub.outcome == "timeout"
    assert sub.timed_out is True
    assert env.tool_calls == []  # the late action batch never executed


def test_valid_answer_left_uncorrected_by_wall_expiry_is_flagged_timed_out(
        tmp_path, synth_question, monkeypatch):
    """A valid file plus an unfinished correction must not look like a choice.

    expD9's D run1 kept an off-menu answer after 370.6s against a 360s budget and
    was reported as ``valid``; the flag separates "ran out of clock" from "the
    model picked wrongly".
    """
    from pico.stepwise import AgentStep

    import benchmarks.omicsbench.adapter as adapter_mod

    node = synth_question.node("n_origin")
    clock = {"t": 0.0}
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock["t"])

    off = {"name": "write_file", "args": {
        "path": "/workspace/answers/origin.json",
        "content": json.dumps({"origin": "progenitor W"})}}

    class FirstRun:
        final_answer = None

        def step(self, observation=None):
            return AgentStep("action", actions=(off,), task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class SlowRun:
        final_answer = None

        def step(self, observation=None):
            clock["t"] += 20.0  # one correction turn outlasts the 10s budget
            return AgentStep("action", actions=(off,), task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def start_run(self, prompt, execute_actions=False):
            return SlowRun() if "Validation" in prompt else FirstRun()

    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(Agent(), render_first_node_prompt(synth_question, node),
                             node, wall_timeout=10.0, recovery_attempts=1)

    assert sub.outcome == "valid"  # official shape semantics are unchanged
    assert sub.timed_out is True   # ... but the correction never finished
    assert sub.answer == {"origin": "progenitor W"}


def test_drive_node_retries_after_transient_model_error(tmp_path, synth_question):
    """A transport error before the deadline must not consume the retry budget."""
    from pico.stepwise import AgentStep

    node = synth_question.node("n_de")
    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": json.dumps(good)}}

    class GoodRun:
        final_answer = None

        def __init__(self):
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                return AgentStep("action", actions=(write,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class BadRun:
        final_answer = None

        def step(self, observation=None):
            raise RuntimeError("transient 502")

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class FlakyAgent:
        def __init__(self):
            self.starts = 0

        def start_run(self, prompt, execute_actions=False):
            self.starts += 1
            return BadRun() if self.starts == 1 else GoodRun()

    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(FlakyAgent(), render_first_node_prompt(synth_question, node),
                             node, recovery_attempts=1)

    assert sub.outcome == "valid"
    assert sub.answer == good
    assert "transient 502" in sub.error  # error kept in the record, not hidden


def test_dev_score_flags_off_menu_multiple_choice(synth_question):
    """An answer outside the offered choices is flagged distinctly (dev metric)."""
    from benchmarks.omicsbench.experiment import dev_score_node

    node = synth_question.node("n_origin")
    score, detail = dev_score_node(node, {"origin": "progenitor Z"})
    assert score == 1.0 and detail["origin"] == "correct"

    score, detail = dev_score_node(node, {"origin": "progenitor Y"})
    assert score == 0.0 and detail["origin"] == "wrong"

    # expR6-style: a truncated value that is not one of the options.
    score, detail = dev_score_node(node, {"origin": "progenitor"})
    assert score == 0.0 and detail["origin"] == "wrong_off_menu"


def test_observed_execution_text_tolerates_malformed_tool_log(synth_question):
    """A malformed tool-log entry must not abort scientific verification."""
    env = MockEnvironment()
    env.tool_calls = [{"name": "run_shell", "args": ["oops"]}, "junk", {"name": "write_file"}]
    adapter = OmicsBenchAdapter(synth_question, env)
    text = adapter._observed_execution_text()
    # Malformed entries are tolerated; the well-formed write is still described.
    assert "write_file" in text


def test_step_loop_closes_run_when_round_budget_exhausted(tmp_path, synth_question):
    """Exhausting max_rounds must release the run instead of leaking it."""
    from pico.stepwise import AgentStep

    node = synth_question.node("n_de")
    created = []

    class EndlessRun:
        final_answer = None

        def __init__(self):
            self.closed = False

        def step(self, observation=None):
            action = {"name": "run_shell", "args": {"command": "echo hi"}}
            return AgentStep("action", actions=(action,), task_state=None)

        def close(self):
            self.closed = True

        def inject_event(self, event):
            pass

    class EndlessAgent:
        def start_run(self, prompt, execute_actions=False):
            run = EndlessRun()
            created.append(run)
            return run

    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(EndlessAgent(), render_first_node_prompt(synth_question, node),
                             node, max_rounds=2, recovery_attempts=0)

    assert sub.outcome == "file_missing"
    assert len(sub.tool_calls) == 2
    assert created and created[0].closed is True


def test_injected_padj_command_triggers_blocking_and_recovery(tmp_path, synth_question):
    """Controlled drift probe: an injected padj selection must be detected.

    The harness runs one padj-based command after the agent's attempt; with the
    full pipeline that must surface as a blocking C001 and a correction turn.
    """
    from pico.stepwise import AgentStep

    from benchmarks.omicsbench.adapter import ScientificStages

    node = synth_question.node("n_de")
    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": json.dumps(good)}}

    class Run:
        final_answer = None

        def __init__(self):
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                return AgentStep("action", actions=(write,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts = []

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            return Run()

    env = MockEnvironment()
    adapter = OmicsBenchAdapter(synth_question, env)
    agent = Agent()
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                             scientific=ScientificStages(True, True, True),
                             recovery_attempts=1,
                             inject_command="python -c \"sel = df.padj < 0.05\"")

    blocking = [(v.constraint_id, v.status, v.severity) for v in sub.verification
                if getattr(v, "severity", "") == "blocking"]
    assert ("C001", "VIOLATION", "blocking") in blocking
    assert any("Scientific validation" in p for p in agent.prompts)  # correction injected
    injected = [c for c in sub.tool_calls
                if str((c.get("args") or {}).get("command", "")).startswith("python -c")]
    assert injected and injected[0]["status"] == "ok"


def test_injected_probe_evidence_clears_once_model_rewrites_answer(tmp_path, synth_question):
    """The harness probe must not report drift forever after the model corrects.

    The injection overwrites the graded file with a padj-based answer. Once the
    model writes a new answer, the probe no longer describes the artifact under
    review, so it must be dropped from the evidence; otherwise the blocking
    violation could never clear and recovery success would be unmeasurable.
    """
    from pico.stepwise import AgentStep

    from benchmarks.omicsbench.adapter import ScientificStages

    node = synth_question.node("n_de")
    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    drifted = {"marker_direction": [{"group": "higher in A",
                                     "items": ["GENE_ALPHA", "GENE_BETA"]}]}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": json.dumps(good)}}

    class DriftEnv(MockEnvironment):
        def execute(self, name, args):
            out = super().execute(name, args)
            if name == "run_shell" and "padj" in str(args.get("command", "")):
                self.files["/workspace/answers/markers.json"] = json.dumps(drifted)
            return out

    class Run:
        final_answer = None

        def __init__(self, counter):
            self._counter = counter

        def step(self, observation=None):
            self._counter[0] += 1
            if self._counter[0] <= 2:      # first attempt, then the correction
                return AgentStep("action", actions=(write,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts = []
            self._counter = [0]

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            return Run(self._counter)

    agent = Agent()
    adapter = OmicsBenchAdapter(synth_question, DriftEnv())
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                             scientific=ScientificStages(True, True, True),
                             recovery_attempts=1,
                             inject_command="python -c \"sel = df.padj < 0.05\"")

    # The probe was detected and a correction was injected ...
    assert any("Scientific validation" in p for p in agent.prompts)
    # ... and after the rewrite the probe is no longer blocking evidence.
    assert not [v for v in sub.verification if getattr(v, "severity", "") == "blocking"]
    c001 = [v for v in sub.verification if v.constraint_id == "C001"]
    assert c001 and c001[0].status == "UNVERIFIED"
    assert sub.answer == good


def test_scientific_recovery_is_not_ended_by_stale_valid_answer(tmp_path, synth_question):
    """A correction turn must not end after the model's first action.

    Regression (expR25): the drift probe leaves a schema-valid file behind. The
    old early-exit treated that stale file as "done" right after the model's
    first recovery action (usually the recompute), so the correction could never
    replace it and recovery was a no-op. Completion now requires a *rewritten*
    answer.
    """
    from pico.stepwise import AgentStep

    from benchmarks.omicsbench.adapter import ScientificStages

    node = synth_question.node("n_de")
    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    drifted = {"marker_direction": [{"group": "higher in A",
                                     "items": ["GENE_ALPHA", "GENE_BETA"]}]}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": json.dumps(good)}}
    recompute = {"name": "run_shell", "args": {"command": "python scratch/markers.py"}}

    class DriftEnv(MockEnvironment):
        def execute(self, name, args):
            out = super().execute(name, args)
            if name == "run_shell" and "padj" in str(args.get("command", "")):
                self.files["/workspace/answers/markers.json"] = json.dumps(drifted)
            return out

    class Run:
        final_answer = None

        def __init__(self, script):
            self._script = script
            self._i = 0

        def step(self, observation=None):
            if self._i >= len(self._script):
                return AgentStep("final", answer="done", task_state=None)
            step = self._script[self._i]
            self._i += 1
            return step

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts = []
            self._scripts = [
                [AgentStep("action", actions=(write,), task_state=None),
                 AgentStep("final", answer="done", task_state=None)],
                [AgentStep("action", actions=(recompute,), task_state=None),
                 AgentStep("action", actions=(write,), task_state=None),
                 AgentStep("final", answer="done", task_state=None)],
            ]

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            return Run(self._scripts[min(len(self.prompts) - 1, len(self._scripts) - 1)])

    agent = Agent()
    env = DriftEnv()
    adapter = OmicsBenchAdapter(synth_question, env)
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                             scientific=ScientificStages(True, True, True),
                             recovery_attempts=1,
                             inject_command="python -c \"sel = df.padj < 0.05\"")

    assert any("Scientific validation" in p for p in agent.prompts)
    # The method-only correction must still name the graded artifact and its shape
    # (expR26: wrote answers/answer.json; expR27: renamed the required labels).
    correction_prompt = [p for p in agent.prompts if "Scientific validation" in p][0]
    assert "/workspace/answers/markers.json" in correction_prompt
    assert "marker_direction" in correction_prompt
    assert "'higher in A'" in correction_prompt and "'higher in B'" in correction_prompt
    # The recompute action did not end the turn; the model's rewrite is the answer.
    assert sub.answer == good
    repair_calls = [c for c in sub.tool_calls
                    if str((c.get("args") or {}).get("command", "")) == "python scratch/markers.py"]
    assert repair_calls


def test_injected_padj_command_is_ignored_without_scientific_stages(tmp_path, synth_question):
    """Control arm: with no stages the drifted answer simply survives."""
    from pico.stepwise import AgentStep

    node = synth_question.node("n_de")
    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": json.dumps(good)}}

    class Run:
        final_answer = None

        def __init__(self):
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                return AgentStep("action", actions=(write,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def start_run(self, prompt, execute_actions=False):
            return Run()

    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(Agent(), render_first_node_prompt(synth_question, node), node,
                             recovery_attempts=1,
                             inject_command="python -c \"sel = df.padj < 0.05\"")

    assert sub.verification == []
    assert sub.outcome == "valid"


def test_offmenu_probe_uses_offered_options_and_never_ground_truth(synth_question):
    from benchmarks.omicsbench.experiment import build_offmenu_probe_command

    target = build_offmenu_probe_command(synth_question)
    assert target is not None
    node_id, command = target
    assert node_id == "n_origin"
    assert "origin.json" in command
    assert "probe off-menu value" in command
    # The injected value must not be the graded truth (it would fake a pass).
    assert "progenitor Z" not in command


def test_offmenu_probe_keeps_one_option_for_multi_select(tmp_path):
    from benchmarks.omicsbench.experiment import build_offmenu_probe_command

    raw = json.loads(json.dumps(SYNTH_QUESTION))
    origin = raw["nodes"][1]["grading"]["graders"][0]
    origin["type"] = "multi_select_choice"
    origin["parameters"]["choices"] = ["PRDM16", "MYOG", "UCP1"]
    origin["ground_truth"] = ["PRDM16", "MYOG"]
    path = tmp_path / "multi.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    target = build_offmenu_probe_command(load_question(path))
    assert target is not None
    node_id, command = target
    assert node_id == "n_origin"
    # One offered option is kept so the answer stays list-shaped; the sentinel is
    # the extra item outside the candidate list (#41).
    assert "PRDM16" in command
    assert "probe off-menu value" in command


def test_offmenu_probe_follows_the_node_filter(synth_question):
    from benchmarks.omicsbench.experiment import build_offmenu_probe_command

    # A probe on a node that is not driven would never fire, so selection must
    # respect --nodes and report "nothing to probe" instead of silently skipping.
    assert build_offmenu_probe_command(synth_question, ["n_de"]) is None
    target = build_offmenu_probe_command(synth_question, ["n_de", "n_origin"])
    assert target is not None and target[0] == "n_origin"


def test_offmenu_probe_returns_none_without_a_choice_node(tmp_path):
    from benchmarks.omicsbench.experiment import build_offmenu_probe_command

    raw = json.loads(json.dumps(SYNTH_QUESTION))
    raw["nodes"] = [raw["nodes"][0]]
    path = tmp_path / "no_choice.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert build_offmenu_probe_command(load_question(path)) is None


def _record_question(tmp_path) -> "object":
    raw = json.loads(json.dumps(SYNTH_QUESTION))
    raw["nodes"] = [raw["nodes"][0]]
    graders = raw["nodes"][0]["grading"]["graders"]
    graders[0] = {
        "name": "axis_effects",
        "type": "differential_feature_set",
        "parameters": {"feature_match": "gene", "effect_full_credit_within": 0.5,
                       "score_direction": True},
        "ground_truth": [
            {"feature": "GENE_A", "effect": -1.7, "direction": "down", "p_value": 0.01},
            {"feature": "GENE_B", "effect": 2.4, "direction": "up", "p_value": 0.02},
        ],
    }
    path = tmp_path / "records.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_question(path)


def test_malformed_records_probe_writes_bare_strings(tmp_path):
    from benchmarks.omicsbench.experiment import build_malformed_records_command

    target = build_malformed_records_command(_record_question(tmp_path))
    assert target is not None
    node_id, command = target
    assert node_id == "n_de"
    assert "markers.json" in command
    assert "probe feature A" in command  # a record-shaped answer as bare strings
    assert "GENE_A" not in command       # ground truth is never injected


def test_bare_string_feature_records_get_a_targeted_rewrite(tmp_path):
    """expD11: official top-level validation accepts a list, so only this check catches it."""
    from pico.stepwise import AgentStep

    q = _record_question(tmp_path)
    node = q.node("n_de")
    bad = {"axis_effects": ["GENE_A", "GENE_B"]}
    good = {"axis_effects": [{"feature": "GENE_A", "effect": -1.7,
                              "direction": "down", "p_value": 0.01}]}

    class Run:
        final_answer = None

        def __init__(self, payload):
            self.payload = payload
            self._sent = False

        def step(self, observation=None):
            if self._sent:
                return AgentStep("final", answer="done", task_state=None)
            self._sent = True
            return AgentStep("action", actions=({"name": "write_file", "args": {
                "path": "/workspace/answers/markers.json",
                "content": json.dumps(self.payload)}},), task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts: list[str] = []

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            return Run(bad if len(self.prompts) == 1 else good)

    agent = Agent()
    adapter = OmicsBenchAdapter(q, MockEnvironment())
    sub = adapter.drive_node(agent, render_first_node_prompt(q, node), node,
                             recovery_attempts=1)

    correction = [p for p in agent.prompts if "JSON array of record objects" in p]
    assert correction, agent.prompts
    assert "markers.json" in correction[0]
    assert "feature" in correction[0] and "p_value" in correction[0]
    assert sub.outcome == "valid"
    assert sub.answer == good  # the rewrite is the graded artifact


def _numeric_question(tmp_path) -> "object":
    raw = json.loads(json.dumps(SYNTH_QUESTION))
    raw["nodes"] = [raw["nodes"][0]]
    grading = raw["nodes"][0]["grading"]
    grading["output_filename"] = "deg_counts.json"
    grading["graders"] = [
        {"name": "up_regulated_count", "type": "numeric_tolerance",
         "output_key": "up_regulated",
         "parameters": {"match": "absolute_tolerance", "full_credit_within": 300.0,
                        "no_credit_beyond": 900.0},
         "ground_truth": 1105},
        {"name": "down_regulated_count", "type": "numeric_tolerance",
         "output_key": "down_regulated",
         "parameters": {"match": "absolute_tolerance", "full_credit_within": 300.0,
                        "no_credit_beyond": 900.0},
         "ground_truth": 857},
    ]
    path = tmp_path / "numeric.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return load_question(path)


def test_scalar_strings_probe_writes_numbers_as_strings(tmp_path):
    from benchmarks.omicsbench.experiment import build_scalar_strings_command

    target = build_scalar_strings_command(_numeric_question(tmp_path))
    assert target is not None
    node_id, command = target
    assert node_id == "n_de"
    assert "deg_counts.json" in command
    assert "up_regulated" in command and "down_regulated" in command
    assert "probe number 1" in command
    assert "1105" not in command  # ground truth is never injected


def test_numeric_answers_written_as_strings_get_a_targeted_rewrite(tmp_path):
    """expD13: "1105" is a scalar for the official top-level check."""
    from pico.stepwise import AgentStep

    q = _numeric_question(tmp_path)
    node = q.node("n_de")
    bad = {"up_regulated": "probe number 1", "down_regulated": "probe number 2"}
    good = {"up_regulated": 1105, "down_regulated": 857}

    class Run:
        final_answer = None

        def __init__(self, payload):
            self.payload = payload
            self._sent = False

        def step(self, observation=None):
            if self._sent:
                return AgentStep("final", answer="done", task_state=None)
            self._sent = True
            return AgentStep("action", actions=({"name": "write_file", "args": {
                "path": "/workspace/answers/deg_counts.json",
                "content": json.dumps(self.payload)}},), task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts: list[str] = []

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            return Run(bad if len(self.prompts) == 1 else good)

    agent = Agent()
    adapter = OmicsBenchAdapter(q, MockEnvironment())
    sub = adapter.drive_node(agent, render_first_node_prompt(q, node), node,
                             recovery_attempts=1)

    correction = [p for p in agent.prompts if "must be a JSON number" in p]
    assert correction, agent.prompts
    assert "deg_counts.json" in correction[0]
    assert sub.outcome == "valid"
    assert sub.answer == good


def test_numeric_tolerance_dev_uses_the_full_credit_window(tmp_path):
    """The local dev scorer must not call a within-tolerance count wrong."""
    from benchmarks.omicsbench.experiment import dev_score_node

    node = _numeric_question(tmp_path).node("n_de")
    exact = {"up_regulated": 1105, "down_regulated": 857}
    within = {"up_regulated": 1168, "down_regulated": 726}   # |Δ| ≤ 300
    far = {"up_regulated": 173, "down_regulated": 110}
    as_text = {"up_regulated": "1105", "down_regulated": "857"}

    assert dev_score_node(node, exact)[0] == 1.0
    assert dev_score_node(node, within)[0] == 1.0
    assert dev_score_node(node, far)[0] == 0.0
    assert dev_score_node(node, as_text)[0] == 0.0  # strings are not numeric answers


def test_record_numbers_written_as_strings_are_flagged(tmp_path):
    """Field types come from the schema; "−1.65" is not a number (#47 family)."""
    from benchmarks.omicsbench.adapter import _feature_record_violation

    node = _record_question(tmp_path).node("n_de")
    bad = {"axis_effects": [{"feature": "GENE_A", "effect": "-1.65",
                             "direction": "down", "p_value": "0.02"}]}
    good = {"axis_effects": [{"feature": "GENE_A", "effect": -1.65,
                              "direction": "down", "p_value": 0.02}]}

    message = _feature_record_violation(node, bad)
    assert message is not None
    assert "as strings instead of JSON numbers" in message
    assert _feature_record_violation(node, good) is None


def test_record_answers_get_parameter_level_diagnostics(tmp_path):
    """dev=0 alone cannot separate a wrong answer from a formatting difference."""
    from benchmarks.omicsbench.experiment import content_diagnostics

    node = _record_question(tmp_path).node("n_de")
    answer = {"axis_effects": [
        {"feature": "GENE_A", "effect": -1.65, "direction": "down", "p_value": 0.02},
        {"feature": "probe feature A", "effect": 1.0, "direction": "up", "p_value": 0.5},
    ]}

    diag = content_diagnostics(node, answer)["axis_effects"]
    assert diag["expected"] == 2 and diag["reported"] == 2
    assert diag["matched"] == 1                 # only GENE_A is an expected feature
    assert diag["effect_within_full"] == 1      # |−1.65 − (−1.7)| = 0.05 ≤ 0.5
    assert diag["direction_match"] == 1
    assert diag["max_effect_delta"] == 0.05
    assert diag["extra_features"] == 1
    # It is diagnostics, not a score: a missing key returns no diagnostic block.
    assert content_diagnostics(node, {"other": []}) == {}


def test_gene_set_answers_get_overlap_diagnostics(tmp_path):
    """expD16 n5: an extra gene makes dev 0, but the overlap is the real evidence."""
    from benchmarks.omicsbench.experiment import content_diagnostics

    raw = json.loads(json.dumps(SYNTH_QUESTION))
    raw["nodes"] = [raw["nodes"][0]]
    grading = raw["nodes"][0]["grading"]
    grading["output_filename"] = "genes.json"
    grading["graders"] = [{"name": "effectors", "type": "gene_set",
                           "parameters": {"beta": 2.0},
                           "ground_truth": ["ITGA3", "ITGB1", "EPHA2"]}]
    path = tmp_path / "gene_set.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    node = load_question(path).node("n_de")
    diag = content_diagnostics(
        node, {"effectors": ["ITGA1", "itga3", "ITGB1", "EPHA2"]})["effectors"]
    assert diag == {"expected": 3, "reported": 4, "matched": 3,
                    "missing": 0, "extra": 1}


def test_probe_refuses_a_file_no_driven_node_grades(tmp_path, monkeypatch, capsys):
    """A probe that no driven node grades must fail, not silently no-op."""
    from benchmarks.omicsbench import experiment as exp_mod

    qpath = tmp_path / "synth_demo.json"
    qpath.write_text(json.dumps(SYNTH_QUESTION), encoding="utf-8")
    monkeypatch.setenv("BIGMODEL_API_KEY", "test-key")

    rc = exp_mod.main([
        "--question", str(qpath),
        "--work-root", str(tmp_path / "work"),
        "--out", str(tmp_path / "report"),
        "--env-file", str(tmp_path / "missing.env"),
        "--groups", "A", "--k", "1",
        # The malformed-items probe writes marker_divergence.json, which this
        # synthetic question never grades.
        "--inject-malformed-items",
    ])

    assert rc == 2
    assert "no driven node grades" in capsys.readouterr().out


def test_observed_execution_includes_executed_script_body(synth_question):
    """A script's body is evidence once run; a written-only script is not."""
    env = MockEnvironment()
    env.tool_calls = [
        {"name": "write_file", "args": {"path": "/workspace/scratch/a.py",
                                        "content": "sel = df[df.padj < 0.05]"}, "status": "ok"},
        {"name": "write_file", "args": {"path": "/workspace/scratch/b.py",
                                        "content": "sel = df[df.padj < 0.01]"}, "status": "ok"},
        {"name": "run_shell", "args": {"command": "python scratch/a.py"}, "status": "ok"},
    ]
    adapter = OmicsBenchAdapter(synth_question, env)
    text = adapter._observed_execution_text()
    assert "df.padj < 0.05" in text      # executed script body
    assert "df.padj < 0.01" not in text  # written but never run


def test_failed_commands_are_not_scientific_evidence(synth_question):
    """A failed command mentioning padj must not look like method drift."""
    env = MockEnvironment()
    env.tool_calls = [
        {"name": "run_shell", "args": {"command": "import pandas; sel = df.padj < 0.05"},
         "status": "error"},
        {"name": "run_shell", "args": {"command": "python scratch/markers.py"}, "status": "ok"},
    ]
    adapter = OmicsBenchAdapter(synth_question, env)
    text = adapter._observed_execution_text()
    assert "padj" not in text
    assert "python scratch/markers.py" in text


def test_host_status_marks_failed_and_successful_commands(tmp_path):
    from benchmarks.omicsbench.env import HostEnvironment

    env = HostEnvironment(tmp_path / "data", tmp_path / "work")
    env.execute("run_shell", {"command": "python -c \"raise SystemExit(3)\"", "timeout": 30})
    assert env.last_status == "error"
    assert env.tool_calls[-1].get("status") is None  # adapter tags it, not the env
    env.execute("run_shell", {"command": "python -c \"print(1)\"", "timeout": 30})
    assert env.last_status == "ok"


def test_padj_injection_command_writes_drifted_answer(tmp_path):
    """The controlled-drift command must actually execute (not a shell syntax error)."""
    from benchmarks.omicsbench.env import HostEnvironment
    from benchmarks.omicsbench.experiment import PADJ_SELECTION_COMMAND

    data_root = Path("D:/1Bio_Soft/omicsbench/data")
    if not (data_root / "gene_expression_profile.csv.gz").exists():
        pytest.skip("bovine data not available on this host")
    env = HostEnvironment(data_root, tmp_path / "work")
    env.execute("run_shell", {"command": PADJ_SELECTION_COMMAND, "timeout": 120})
    assert env.last_status == "ok"
    answer = json.loads(env.read_answer("marker_divergence.json"))
    sf = [g for row in answer["marker_direction"] if row["group"] == "higher in SF"
          for g in row["items"]]
    assert "ITGB1" in sf  # padj keeps ITGB1; the instruction-compliant test drops it


def test_host_environment_translates_script_paths(tmp_path):
    """A script written via write_file must resolve /data and /workspace on host.

    Regression: node1 wrote a correct analysis script, but `python scratch/x.py`
    crashed on /data and /workspace/answers (container paths inside the file were
    never translated), so the node never produced an answer.
    """
    import sys

    from benchmarks.omicsbench.env import HostEnvironment

    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    env = HostEnvironment(data_root, tmp_path / "work")

    script = (
        "import json\n"
        "rows = open('/data/x.csv').read().strip().splitlines()\n"
        "json.dump({'rows': len(rows)}, open('/workspace/answers/a.json', 'w'))\n"
    )
    env.execute("write_file", {"path": "/workspace/scratch/s.py", "content": script})
    written = (tmp_path / "work" / "scratch" / "s.py").read_text(encoding="utf-8")
    assert "'/data/" not in written and "'/workspace" not in written  # translated

    env.execute("run_shell", {"command": f'"{sys.executable}" scratch/s.py', "timeout": 60})
    assert env.read_answer("a.json") == '{"rows": 2}'


def test_host_environment_does_not_rewrite_answer_json(tmp_path):
    """Answer content is data, not a script: leave container-looking strings alone."""
    from benchmarks.omicsbench.env import HostEnvironment

    env = HostEnvironment(tmp_path / "data", tmp_path / "work")
    payload = json.dumps({"path": "/data/keep/this"})
    env.execute("write_file", {"path": "/workspace/answers/a.json", "content": payload})
    assert env.read_answer("a.json") == payload


def test_run_shell_timeout_clamped_to_remaining_wall_budget(synth_question, monkeypatch):
    """A model-supplied 600s command timeout must not outlive the node budget."""
    from pico.stepwise import AgentStep

    import benchmarks.omicsbench.adapter as adapter_mod

    node = synth_question.node("n_de")
    clock = {"t": 0.0}
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock["t"])

    class RecordingEnv(MockEnvironment):
        def __init__(self):
            super().__init__()
            self.seen = []

        def execute(self, name, args):
            self.seen.append((name, dict(args)))
            return super().execute(name, args)

    class Run:
        final_answer = None

        def __init__(self):
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                clock["t"] += 3.0  # 7s of the 10s budget remain
                action = {"name": "run_shell",
                          "args": {"command": "python x.py", "timeout": 600}}
                return AgentStep("action", actions=(action,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def start_run(self, prompt, execute_actions=False):
            return Run()

    env = RecordingEnv()
    adapter = OmicsBenchAdapter(synth_question, env)
    adapter.drive_node(Agent(), render_first_node_prompt(synth_question, node),
                       node, wall_timeout=10.0, recovery_attempts=0)

    assert env.seen[0][0] == "run_shell"
    assert env.seen[0][1]["timeout"] == 7


def test_drive_node_rewrites_off_menu_choice(tmp_path, synth_question):
    """A valid-shaped but off-menu choice answer gets one targeted rewrite."""
    from pico.stepwise import AgentStep

    node = synth_question.node("n_origin")
    wrong = {"origin": "progenitor"}
    right = {"origin": "progenitor Z"}

    class Run:
        final_answer = None

        def __init__(self, payload):
            self.payload = payload
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                action = {"name": "write_file", "args": {
                    "path": "/workspace/answers/origin.json",
                    "content": json.dumps(self.payload)}}
                return AgentStep("action", actions=(action,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts = []
            self.n = 0

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            self.n += 1
            return Run(wrong if self.n == 1 else right)

    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    agent = Agent()
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node),
                             node, recovery_attempts=1)

    assert sub.outcome == "valid"
    assert sub.answer == right
    assert len(agent.prompts) == 2
    assert "not one of the offered choices" in agent.prompts[1]


def test_multi_select_off_menu_items_get_a_targeted_rewrite():
    """expR30: a retried answer added genes outside the offered candidate list."""
    from benchmarks.omicsbench.adapter import _choice_violation

    class Grader:
        type = "multi_select_choice"
        parameters = {"choices": ["MYOD1", "MYOG", "MYF5"]}

        def resolved_output_key(self):
            return "regulators_up"

    class Grading:
        output_filename = "lineup.json"
        graders = [Grader()]

    class Node:
        grading = Grading()

    msg = _choice_violation(Node(), {"regulators_up": ["MYOD1", "MYOG", "MYF5", "MEOX2"]})
    assert msg and "MEOX2" in msg and "not among the offered candidates" in msg
    assert _choice_violation(Node(), {"regulators_up": ["MYOD1", "MYOG"]}) is None


def test_file_missing_recovery_quotes_the_answer_from_the_model_reply(tmp_path, synth_question):
    """When the reply already holds the JSON, the correction must name it.

    expR30/R31/R33: the model ended turns with the answer as text and no file;
    the generic "write the file" prompt made it redo the analysis (and sometimes
    widen the answer).
    """
    from pico.stepwise import AgentStep

    node = synth_question.node("n_origin")
    payload = {"origin": "progenitor Z"}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/origin.json", "content": json.dumps(payload)}}

    class Run:
        final_answer = None

        def __init__(self, writes):
            self._writes = writes
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._writes and self._n == 1:
                return AgentStep("action", actions=(write,), task_state=None)
            if not self._writes and self._n == 1:
                return AgentStep("final", answer=json.dumps(payload), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts = []
            self.n = 0

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            self.n += 1
            return Run(writes=self.n > 1)

    agent = Agent()
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                            recovery_attempts=1)

    assert sub.outcome == "valid"
    assert sub.answer == payload
    recovery = agent.prompts[1]
    assert "was not written" in recovery
    assert json.dumps(payload) in recovery          # its own JSON, verbatim
    assert "do not recompute" in recovery


def test_recovery_prompt_includes_json_parse_error(synth_question):
    """invalid_json recovery must quote the parser message (e.g. non-finite token)."""
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    node = synth_question.node("n_de")
    msg = adapter._recovery_prompt(
        node, outcome="invalid_json",
        json_error="ValueError: answer contains the non-finite JSON token 'NaN'")
    assert "Parser said:" in msg
    assert "NaN" in msg


def test_norm_items_rejects_non_sequences():
    """A string/mapping must not be iterated as an item collection (dev metric)."""
    from benchmarks.omicsbench.experiment import _norm_items

    assert _norm_items([" A ", "b"]) == frozenset({"a", "b"})
    for bad in ("GENE_ALPHA", {"GENE_ALPHA": True}):
        with pytest.raises(TypeError):
            _norm_items(bad)


def test_recovery_gets_reserved_budget_after_first_attempt_exhausts_phase(synth_question, monkeypatch):
    """The first attempt must not be allowed to consume the recovery window."""
    from pico.stepwise import AgentStep

    import benchmarks.omicsbench.adapter as adapter_mod

    node = synth_question.node("n_de")
    clock = {"t": 0.0}
    monkeypatch.setattr(adapter_mod.time, "monotonic", lambda: clock["t"])

    good = {"marker_direction": [{"group": "higher in A", "items": ["GENE_ALPHA"]}]}
    write = {"name": "write_file", "args": {
        "path": "/workspace/answers/markers.json", "content": json.dumps(good)}}

    class ExploreRun:
        final_answer = None

        def step(self, observation=None):
            clock["t"] += 1.0
            return AgentStep("action", actions=({"name": "run_shell",
                                                 "args": {"command": "echo hi"}},),
                             task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class WriteRun:
        final_answer = None

        def __init__(self):
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            clock["t"] += 1.0
            if self._n == 1:
                return AgentStep("action", actions=(write,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.n = 0

        def start_run(self, prompt, execute_actions=False):
            self.n += 1
            return ExploreRun() if self.n == 1 else WriteRun()

    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(Agent(), render_first_node_prompt(synth_question, node), node,
                             wall_timeout=10.0, recovery_attempts=1)

    # Exploration used 70% of the clock; recovery still ran and wrote the answer.
    assert sub.outcome == "valid"
    assert sub.answer == good


def test_experiment_persists_completed_runs_before_crash(tmp_path, monkeypatch):
    """An interrupt mid-sweep must leave completed runs in report.json."""
    from benchmarks.omicsbench import experiment as exp_mod

    qpath = tmp_path / "synth_demo.json"
    qpath.write_text(json.dumps(SYNTH_QUESTION), encoding="utf-8")
    monkeypatch.setenv("BIGMODEL_API_KEY", "test-key")

    calls = {"n": 0}

    def run_then_interrupt(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return []  # completed run with no node results, still recorded
        raise KeyboardInterrupt

    monkeypatch.setattr(exp_mod.OmicsBenchAdapter, "run_question", run_then_interrupt)

    out = tmp_path / "report"
    with pytest.raises(KeyboardInterrupt):
        exp_mod.main([
            "--question", str(qpath),
            "--work-root", str(tmp_path / "work"),
            "--out", str(out),
            "--env-file", str(tmp_path / "missing.env"),
            "--groups", "A", "--k", "2",
        ])

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    cfg = report["configs"]["A"]
    assert cfg["partial"] is True
    assert cfg["summary"] is None
    assert len(cfg["runs"]) == 1


def test_experiment_marks_a_run_whose_nodes_all_failed(tmp_path, monkeypatch):
    """expD24/25: a shared transport failure (HTTP 429) must not look like a 0.0."""
    from benchmarks.omicsbench import experiment as exp_mod
    from benchmarks.omicsbench.adapter import NodeSubmission

    qpath = tmp_path / "synth_demo.json"
    qpath.write_text(json.dumps(SYNTH_QUESTION), encoding="utf-8")
    monkeypatch.setenv("BIGMODEL_API_KEY", "test-key")

    def all_failed(self, factory, **kwargs):
        error = "RuntimeError: OpenAI-compatible request failed with HTTP 429: balance"
        return [NodeSubmission(node_id="n_de", outcome="error", error=error),
                NodeSubmission(node_id="n_origin", outcome="error", error=error)]

    monkeypatch.setattr(exp_mod.OmicsBenchAdapter, "run_question", all_failed)

    out = tmp_path / "report"
    rc = exp_mod.main([
        "--question", str(qpath),
        "--work-root", str(tmp_path / "work"),
        "--out", str(out),
        "--env-file", str(tmp_path / "missing.env"),
        "--groups", "A", "--k", "1",
    ])

    assert rc == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    run = report["configs"]["A"]["runs"][0]
    assert run["error"].startswith("all nodes failed")
    assert "429" in run["error"]
    assert report["configs"]["A"]["summary"]["failed_runs"] == 1


def test_experiment_records_failed_run_and_keeps_sweeping(tmp_path, monkeypatch):
    """A run-level setup failure is recorded and the next run still executes."""
    from benchmarks.omicsbench import experiment as exp_mod

    qpath = tmp_path / "synth_demo.json"
    qpath.write_text(json.dumps(SYNTH_QUESTION), encoding="utf-8")
    monkeypatch.setenv("BIGMODEL_API_KEY", "test-key")

    def boom(self, *args, **kwargs):
        raise RuntimeError("synthetic factory failure")

    monkeypatch.setattr(exp_mod.OmicsBenchAdapter, "run_question", boom)

    out = tmp_path / "report"
    rc = exp_mod.main([
        "--question", str(qpath),
        "--work-root", str(tmp_path / "work"),
        "--out", str(out),
        "--env-file", str(tmp_path / "missing.env"),
        "--groups", "A", "--k", "2",
    ])

    assert rc == 0
    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    runs = report["configs"]["A"]["runs"]
    assert len(runs) == 2  # both failures survived into the report
    assert all(r["error"] and "synthetic factory failure" in r["error"] for r in runs)
    assert report["configs"]["A"]["summary"]["failed_runs"] == 2
    assert report["wall_timeout_scope"] == "per_node"


def test_grouped_items_written_as_string_get_a_targeted_rewrite(tmp_path, synth_question):
    """expD4: `items` as a space-joined string passes official shape validation.

    We keep ``valid`` as the official-parity verdict and add one targeted rewrite.
    """
    from pico.stepwise import AgentStep

    node = synth_question.node("n_de")
    bad = {"marker_direction": [{"group": "higher in A", "items": "GENE_ALPHA GENE_BETA"}]}
    good = {"marker_direction": [{"group": "higher in A",
                                  "items": ["GENE_ALPHA", "GENE_BETA"]}]}

    class Run:
        final_answer = None

        def __init__(self, payload):
            self.payload = payload
            self._n = 0

        def step(self, observation=None):
            self._n += 1
            if self._n == 1:
                action = {"name": "write_file", "args": {
                    "path": "/workspace/answers/markers.json",
                    "content": json.dumps(self.payload)}}
                return AgentStep("action", actions=(action,), task_state=None)
            return AgentStep("final", answer="done", task_state=None)

        def close(self):
            pass

        def inject_event(self, event):
            pass

    class Agent:
        def __init__(self):
            self.prompts = []
            self.n = 0

        def start_run(self, prompt, execute_actions=False):
            self.prompts.append(prompt)
            self.n += 1
            return Run(bad if self.n == 1 else good)

    agent = Agent()
    adapter = OmicsBenchAdapter(synth_question, MockEnvironment())
    sub = adapter.drive_node(agent, render_first_node_prompt(synth_question, node), node,
                            recovery_attempts=1)

    assert sub.outcome == "valid"
    assert sub.answer == good
    assert len(agent.prompts) == 2
    assert "JSON array of item strings" in agent.prompts[1]


def test_docker_environment_builds_official_layout_command(tmp_path):
    """run_shell must mount /data read-only and /workspace read-write.

    A fake ``docker`` executable records its argv so the test needs no daemon.
    """
    from benchmarks.omicsbench.env import DockerEnvironment

    data = tmp_path / "data"
    work = tmp_path / "work"
    data.mkdir()
    fake = tmp_path / "fake-docker.bat"
    fake.write_text("@echo off\r\necho FAKE %*\r\n", encoding="utf-8")

    env = DockerEnvironment(data, work, image="img:test", docker=str(fake))
    out = env.execute("run_shell", {"command": "python -c \"print(1)\"", "timeout": 5})

    assert "FAKE run --rm --name ob_" in out
    assert f"{data.as_posix()}:/data:ro" in out.replace("\\", "/")
    assert f"{work.as_posix()}:/workspace" in out.replace("\\", "/")
    assert "-w /workspace img:test sh -lc" in out
    assert "print(1)" in out
    assert env.last_status == "ok"


def test_docker_environment_file_tools_stay_on_host(tmp_path):
    """File tools must not pay a container round-trip and must keep bytes exact."""
    from benchmarks.omicsbench.env import DockerEnvironment

    data = tmp_path / "data"
    work = tmp_path / "work"
    data.mkdir()
    (data / "expr.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    fake = tmp_path / "fake-docker.bat"
    fake.write_text("@echo off\r\nexit /b 9\r\n", encoding="utf-8")
    env = DockerEnvironment(data, work, docker=str(fake), network=False)

    env.execute("write_file", {"path": "/workspace/answers/x.json",
                               "content": "{\"k\": \"/data/untouched\"}"})
    assert env.read_answer("x.json") == "{\"k\": \"/data/untouched\"}"
    assert env.execute("read_file", {"path": "/data/expr.csv"}).startswith("a,b")
    assert "expr.csv" in env.execute("list_files", {"path": "/data"})
    env.clear_answer("x.json")
    assert env.read_answer("x.json") is None