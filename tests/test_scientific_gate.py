"""Tests for the live science-constraint gate wired into the agent loop.

The gate is inert for ordinary instructions; it only rejects completion when the
*executed* commands positively show a method substitution that contradicts an
explicit requirement in the current instruction.
"""
from __future__ import annotations

import pytest

from goaltree.scientific.gate import ScientificGate
from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def _gate(instruction: str) -> ScientificGate:
    return ScientificGate(instruction)


def test_disabled_for_instruction_without_statistical_requirement():
    gate = _gate("Refactor the parser and make the tests pass.")
    assert gate.enabled is False
    gate.observe("run_shell", {"command": "pytest -q"})
    assert gate.commands == []
    assert gate.problem("done") is None


def test_blocks_positively_observed_padj_selection():
    gate = _gate("Report markers with unadjusted p < 0.05.")
    gate.observe("run_shell", {"command": "sel = df[df.padj < 0.05]"})
    msg = gate.problem("All 12 markers reported.")
    assert msg and "unadjusted" in msg.lower()
    # The correction budget is one: a second completion is not rejected again.
    assert gate.problem("All 12 markers reported.") is None


def test_does_not_block_raw_p_selection():
    gate = _gate("Report markers with unadjusted p < 0.05.")
    gate.observe("run_shell", {"command": "sel = df[df.pvalue < 0.05]"})
    assert gate.problem("Markers reported.") is None


def test_does_not_block_on_absence_of_evidence():
    """Drift inside a script file is invisible; silence must not reject a run."""
    gate = _gate("Select genes with adjusted p < 0.05.")
    gate.observe("run_shell", {"command": "python analysis.py"})
    assert gate.problem("Selected 40 genes.") is None


def test_drift_inside_an_executed_script_is_detected():
    """Analyses usually live in a file; the body must count once it actually runs."""
    gate = _gate("Report markers with unadjusted p < 0.05.")
    gate.observe("write_file", {"path": "/workspace/scratch/a.py",
                                "content": "sel = df[df.padj < 0.05]\n"})
    assert gate.problem("Markers reported.") is None  # written but never executed
    gate.observe("run_shell", {"command": "python scratch/a.py"})
    msg = gate.problem("Markers reported.")
    assert msg and "unadjusted" in msg.lower()


def test_written_but_unused_script_is_not_evidence():
    gate = _gate("Report markers with unadjusted p < 0.05.")
    gate.observe("write_file", {"path": "/workspace/scratch/old.py",
                                "content": "sel = df[df.padj < 0.05]\n"})
    gate.observe("run_shell", {"command": "python scratch/other.py"})
    assert gate.problem("Reported.") is None


def test_agent_loop_rejects_once_then_accepts(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"run_shell","args":{"command":"python -c \\"print(\'padj < 0.05\')\\""}}</tool>',
        "<final>All markers reported.</final>",
        "<final>Recomputed with unadjusted p.</final>",
    ]
    agent = Pico(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        max_steps=8,
    )

    answer = agent.ask("Report depot-biased markers with unadjusted p < 0.05.")

    assert answer == "Recomputed with unadjusted p."
    assert agent.current_task_state.status == "completed"
    corrections = [m for m in agent.session["history"]
                   if m.get("role") == "assistant" and "Scientific validation" in str(m.get("content", ""))]
    assert corrections, "the correction must be shown to the model"


def test_agent_loop_accepts_when_scientific_verification_disabled(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    outputs = [
        '<tool>{"name":"run_shell","args":{"command":"python -c \\"print(\'padj < 0.05\')\\""}}</tool>',
        "<final>Reported using adjusted p.</final>",
    ]
    agent = Pico(
        model_client=FakeModelClient(outputs),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        max_steps=8,
        scientific_verification=False,
    )

    assert agent.ask("Report markers with unadjusted p < 0.05.") == "Reported using adjusted p."
    assert not any("Scientific validation" in str(m.get("content", ""))
                   for m in agent.session["history"] if m.get("role") == "assistant")
