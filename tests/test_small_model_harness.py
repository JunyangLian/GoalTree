"""Tests for the small-model profile and the P0/P1 evaluation harness."""
from __future__ import annotations

import json
from unittest.mock import patch

from pico.prompt_prefix import build_prompt_prefix
from pico.providers.clients import OllamaModelClient
from pico.tools import build_tool_registry
from pico.workspace import WorkspaceContext

ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
SUITE = ROOT / "benchmarks" / "small_model_v1"


class _Agent:
    depth = 0
    max_depth = 1

    def __init__(self, root):
        self.root = root


def test_compact_prefix_is_shorter_and_keeps_the_protocol(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    tools = build_tool_registry(_Agent(tmp_path))

    classic = build_prompt_prefix(workspace=workspace, tools=tools)
    compact = build_prompt_prefix(workspace=workspace, tools=tools, compact=True)

    assert len(compact.text) < len(classic.text)
    for marker in ("<tool>", "<final>", "read_file", "write_file", "run_shell"):
        assert marker in compact.text
    # A rule that only the long profile carries must not leak into the short one.
    assert "New files should be complete" not in compact.text
    assert "New files should be complete" in classic.text


def test_ollama_client_records_its_own_usage_counters():
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps({"response": "ok", "prompt_eval_count": 123,
                               "eval_count": 45}).encode("utf-8")

    client = OllamaModelClient(model="qwen2.5-coder:1.5b",
                               host="http://127.0.0.1:11434",
                               temperature=0.2, top_p=0.9, timeout=30)
    with patch("urllib.request.urlopen", lambda request, timeout: FakeResponse()):
        assert client.complete("hello", 8) == "ok"

    assert client.last_completion_metadata == {"input_tokens": 123,
                                               "output_tokens": 45, "calls": 1}


def test_extract_code_prefers_the_longest_python_block():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "run_small_model_eval", ROOT / "scripts" / "run_small_model_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    text = ("Here is the fix:\n```python\ndef a():\n    return 1\n```\n"
            "and the full file:\n```python\ndef a():\n    return 1\n\ndef b():\n    return 2\n```\n")
    code = module.extract_code(text)
    assert code is not None and "def b()" in code
    assert module.extract_code("no code here") is None

    # Small models echo both files in one block; the test section must be dropped.
    echoed = ("```python\ndef clamp(value, low, high):\n    return low\n\n"
              "import unittest\nclass ClampTests(unittest.TestCase):\n    def test_x(self):\n"
              "        pass\n```")
    cleaned = module.extract_code(echoed)
    assert cleaned is not None
    assert "def clamp" in cleaned
    assert "unittest" not in cleaned and "ClampTests" not in cleaned


def test_compact_completion_rejection_includes_the_literal_call():
    from pico.completion import CompletionGate

    class Agent:
        verification_command = "python -B -m unittest discover -v"
        read_only = False
        compact_prompt = True

        def capture_workspace_snapshot(self):
            return {}

    message = CompletionGate(Agent()).problem()
    assert '"name": "run_shell"' in message
    assert "python -B -m unittest discover -v" in message
    assert message.count("<tool>") == 1

    class ClassicAgent(Agent):
        compact_prompt = False

    assert "<tool>" not in CompletionGate(ClassicAgent()).problem()


def test_auto_verify_runs_the_real_command_after_a_write(tmp_path):
    from pico import FakeModelClient, Pico, SessionStore
    from pico.workspace import WorkspaceContext
    from pico.evaluation.repair_suite import VERIFY_COMMAND

    (tmp_path / "test_demo.py").write_text(
        "import unittest\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n        self.assertEqual(1, 1)\n", encoding="utf-8")
    model = FakeModelClient([
        '<tool name="write_file" path="demo.py"><content>x = 1\n</content></tool>',
        "<final>done</final>",
    ])
    agent = Pico(model, WorkspaceContext.build(tmp_path, lightweight=True),
                 SessionStore(tmp_path / ".pico/sessions"), approval_policy="auto",
                 max_steps=3, verification_command=VERIFY_COMMAND,
                 auto_verify_after_write=True)
    agent.ask("create demo.py")

    tool_messages = [m for m in agent.session["history"] if m.get("role") == "tool"]
    assert any("[AUTO-VERIFY exit=0]" in str(m.get("content")) for m in tool_messages)
    assert any("Ran 1 test" in str(m.get("content")) for m in tool_messages)


def test_plain_code_reply_becomes_a_write_in_structured_mode(tmp_path):
    from pico import FakeModelClient, Pico, SessionStore
    from pico.workspace import WorkspaceContext
    from pico.evaluation.repair_suite import VERIFY_COMMAND

    (tmp_path / "test_demo.py").write_text(
        "import unittest\nfrom demo import value\nclass T(unittest.TestCase):\n"
        "    def test_x(self):\n        self.assertEqual(value(), 1)\n", encoding="utf-8")
    model = FakeModelClient([
        "```python\ndef value():\n    return 1\n```",
        "<final>done</final>",
    ])
    agent = Pico(model, WorkspaceContext.build(tmp_path, lightweight=True),
                 SessionStore(tmp_path / ".pico/sessions"), approval_policy="auto",
                 max_steps=3, verification_command=VERIFY_COMMAND,
                 compact_prompt=True, auto_verify_after_write=True,
                 structured_repair_path="demo.py")
    agent.ask("fix demo.py")

    assert (tmp_path / "demo.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"
    tool_messages = [m for m in agent.session["history"] if m.get("role") == "tool"]
    assert any("[AUTO-VERIFY exit=0]" in str(m.get("content")) for m in tool_messages)


def test_unfenced_code_reply_is_accepted_in_structured_mode(tmp_path):
    from pico import FakeModelClient, Pico, SessionStore
    from pico.workspace import WorkspaceContext

    model = FakeModelClient([
        "Here is the corrected file:\ndef value():\n    return 1\n",
        "<final>done</final>",
    ])
    agent = Pico(model, WorkspaceContext.build(tmp_path, lightweight=True),
                 SessionStore(tmp_path / ".pico/sessions"), approval_policy="auto",
                 max_steps=3, compact_prompt=True,
                 structured_repair_path="demo.py")
    agent.ask("fix demo.py")
    assert (tmp_path / "demo.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"


def test_structured_prefix_has_no_tool_syntax(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    prefix = build_prompt_prefix(workspace=WorkspaceContext.build(tmp_path),
                                 tools=build_tool_registry(_Agent(tmp_path)),
                                 structured_path="demo.py")
    assert "demo.py" in prefix.text
    assert "COMPLETE corrected file" in prefix.text
    assert "<tool>" not in prefix.text
    assert "write_file" not in prefix.text


def test_small_model_assets_fail_before_the_fix_and_pass_after(tmp_path):
    from pico.evaluation.repair_suite import verify_pristine

    tasks = {t["id"]: t for t in json.loads(
        (SUITE / "tasks.json").read_text(encoding="utf-8"))["tasks"]}
    references = json.loads((SUITE / "reference_solutions.json").read_text(encoding="utf-8"))
    for task_id in ("clamp_value", "top_k"):
        task = tasks[task_id]
        buggy = verify_pristine(task, {}, tmp_path / (task_id + "-buggy"))
        fixed = verify_pristine(
            task,
            {name: content.encode() for name, content in references[task_id].items()},
            tmp_path / (task_id + "-fixed"))
        assert buggy["accepted"] is False
        assert buggy["tests_run"] == task["expected_tests"]
        assert fixed["accepted"] is True
        assert fixed["tests_run"] == task["expected_tests"]
