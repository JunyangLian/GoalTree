"""Small-model evaluation: one-shot vs GoalTree harness profiles.

Modes
-----
oneshot        Single model call, no tools. The model receives the buggy module
               and its tests and must return the complete fixed file.
agent          GoalTree/Pico with the classic prompt and full tool set.
agent-compact  GoalTree/Pico with the opt-in compact prompt and a minimal tool
               set, aimed at 1.5B-3B local models.
agent-repair   Pico plus host-side verification after every successful edit.
agent-structured Pico structured profile: a plain code reply is written as the
               target file, and the host runs the real tests.
small-executor Pico's host-driven executor: every round rebuilds the request
               from the current file, visible tests and the latest real failure
               output instead of replaying the conversation.

Success is judged by the pristine external verifier (frozen files + tests), so
editing tests inside the workspace cannot fake a pass. Nothing is scored from
the model's own claim.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pico.providers.clients import OllamaModelClient  # noqa: E402
from pico.runtime import Pico, SessionStore  # noqa: E402
from pico.workspace import WorkspaceContext  # noqa: E402
from pico.evaluation.repair_suite import (install, verify_pristine,  # noqa: E402
                                          VERIFY_COMMAND)

SUITE = ROOT / "benchmarks" / "small_model_v1"
MODES = ("oneshot", "agent", "agent-compact", "agent-repair", "agent-structured",
         "small-executor", "scaffold")


def load_tasks(cohort: str, task_ids: list[str] | None) -> list[dict]:
    tasks = json.loads((SUITE / "tasks.json").read_text(encoding="utf-8"))["tasks"]
    if cohort != "all":
        tasks = [t for t in tasks if t["cohort"] == cohort]
    if task_ids:
        wanted = set(task_ids)
        tasks = [t for t in tasks if t["id"] in wanted]
    return tasks


def extract_code(text: str) -> str | None:
    """Return the module code from a model reply, never the test file.

    Small models often echo both files in one block (the prompt shows both), so a
    block that contains unittest imports or a ``*Tests`` class is trimmed at that
    boundary before it is accepted.
    """
    blocks = re.findall(r"```(?:python)?\s*\n(.*?)```", text, flags=re.S)
    candidates = sorted(blocks, key=len, reverse=True) if blocks else [text]
    for block in candidates:
        code = _drop_test_section(block)
        if "def " in code and not re.search(r"^class \w*Tests?\b", code, re.M):
            return code.strip() + "\n"
    return None


def _drop_test_section(code: str) -> str:
    lines = code.splitlines()
    for index, line in enumerate(lines):
        if (re.match(r"\s*(import unittest|from unittest|class \w+Tests?\s*[(:])", line)
                or re.match(r"\s*#\s*(test|example|usage)", line, re.I)
                or re.match(r"\s*assert\s", line)):
            return "\n".join(lines[:index])
    return code


class CountingClient:
    """Record calls and tokens without changing the client's behaviour."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.raw_samples: list[str] = []

    def complete(self, *args, **kwargs):
        text = self.inner.complete(*args, **kwargs)
        meta = getattr(self.inner, "last_completion_metadata", {}) or {}
        self.calls += 1
        self.input_tokens += int(meta.get("input_tokens") or 0)
        self.output_tokens += int(meta.get("output_tokens") or 0)
        if len(self.raw_samples) < 5:
            self.raw_samples.append(str(text or "")[:400])
        return text

    def __getattr__(self, name):
        return getattr(self.inner, name)


def make_client(args) -> CountingClient:
    inner = OllamaModelClient(model=args.model, host=args.host,
                              temperature=args.temperature, top_p=args.top_p,
                              timeout=args.request_timeout,
                              repeat_penalty=args.repeat_penalty,
                              repeat_last_n=args.repeat_last_n)
    return CountingClient(inner)


def run_tests(workspace: Path) -> dict:
    started = time.monotonic()
    process = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover", "-v"],
                             cwd=workspace, capture_output=True, timeout=120)
    stdout = process.stdout.decode("utf-8", errors="replace")
    stderr = process.stderr.decode("utf-8", errors="replace")
    match = re.search(r"Ran (\d+) tests?", stderr + stdout)
    return {"exit_code": process.returncode, "tests_run": int(match.group(1)) if match else None,
            "elapsed_ms": round((time.monotonic() - started) * 1000),
            "output_tail": (stdout + stderr)[-1200:]}


def oneshot_prompt(task: dict) -> str:
    module = task["editable_files"][0]
    test_name = next(n for n in task["files"] if n != module)
    return (
        "You are repairing one Python file. Return ONLY the complete corrected content "
        f"of {module} inside one ```python fenced block. Do not explain.\n\n"
        f"Task: {task['prompt']}\n\n"
        f"{module}:\n```python\n{task['files'][module]}\n```\n\n"
        f"{test_name}:\n```python\n{task['files'][test_name]}\n```\n"
    )


def run_oneshot(task: dict, workspace: Path, args, client: CountingClient) -> dict:
    module = task["editable_files"][0]
    started = time.monotonic()
    outcome = {"mode": "oneshot", "raw_chars": 0, "code_found": False}
    try:
        text = client.complete(oneshot_prompt(task), args.max_new_tokens)
        outcome["raw_chars"] = len(text or "")
        code = extract_code(text or "")
        if code is None:
            outcome["error"] = "no complete file found in model output"
        else:
            (workspace / module).write_text(code, encoding="utf-8", newline="\n")
            outcome["code_found"] = True
    except Exception as exc:  # transport, timeout, parse
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    outcome["wall_ms"] = round((time.monotonic() - started) * 1000)
    return outcome


def run_scaffold(task: dict, workspace: Path, args, client: CountingClient) -> dict:
    """Harness-level repair loop: full-file output + real test feedback.

    This does not ask a 1.5B model to emit tool calls. The harness writes the
    returned file and runs the frozen tests itself, feeding the real failure
    output back for the next round. Recorded separately from the Pico agent
    arms so the two are never mixed.
    """
    module = task["editable_files"][0]
    test_name = next(n for n in task["files"] if n != module)
    base = oneshot_prompt(task)
    feedback = ""
    outcome = {"mode": "scaffold", "attempts": 0, "test_failures": []}
    started = time.monotonic()
    for _ in range(args.scaffold_rounds):
        outcome["attempts"] += 1
        try:
            text = client.complete(base + feedback, args.max_new_tokens)
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            break
        code = extract_code(text or "")
        if code is None:
            feedback = "\n\nReturn ONLY the complete corrected file in one ```python block."
            continue
        (workspace / module).write_text(code, encoding="utf-8", newline="\n")
        result = run_tests(workspace)
        outcome["test_failures"].append(result["exit_code"])
        if result["exit_code"] == 0:
            break
        feedback = ("\n\nThe tests still fail. Real output:\n"
                    f"```\n{result['output_tail'][-900:]}\n```\n"
                    f"Return the complete corrected {module} again, in one ```python block.")
    outcome["wall_ms"] = round((time.monotonic() - started) * 1000)
    return outcome


def run_agent(task: dict, workspace: Path, args, client: CountingClient,
              compact: bool, auto_verify: bool = False, structured: bool = False,
              executor: bool = False) -> dict:
    module = task["editable_files"][0]
    test_name = next(n for n in task["files"] if n != module)
    context = WorkspaceContext(str(workspace), str(workspace), "-", "-", "fresh fixture", [], {})
    allowed = (["read_file", "write_file", "run_shell"] if compact else
               ["list_files", "read_file", "write_file", "patch_file", "run_shell"])
    agent = Pico(client, context, SessionStore(workspace / ".pico/sessions"),
                 approval_policy="ask", max_steps=args.max_steps,
                 max_new_tokens=args.max_new_tokens, max_depth=0,
                 verification_command=VERIFY_COMMAND, allowed_tools=allowed,
                 scientific_verification=False, compact_prompt=compact,
                 auto_verify_after_write=auto_verify,
                 structured_repair_path=module if (structured or executor) else None,
                 small_model_executor=executor,
                 small_model_rounds=getattr(args, "executor_rounds", 3),
                 small_model_reference_paths=[test_name] if executor else None)
    def approve(name, tool_args):
        try:
            if name == "run_shell":
                return str(tool_args.get("command", "")).strip() == VERIFY_COMMAND
            if name in {"write_file", "patch_file"}:
                return agent.path(tool_args.get("path", "")).relative_to(workspace).as_posix() == module
        except (ValueError, TypeError):
            return False
        return name in {"read_file", "list_files"}

    agent.approve = approve
    original = agent.execute_tool
    observed = {"calls": 0, "verify": False}

    def counted(name, tool_args):
        observed["calls"] += 1
        if (name == "run_shell"
                and str(tool_args.get("command", "")).strip() == VERIFY_COMMAND):
            observed["verify"] = True
        return original(name, tool_args)

    agent.execute_tool = counted
    started = time.monotonic()
    outcome = {"mode": ("small-executor" if executor else
                        ("agent-structured" if structured else
                         ("agent-repair" if auto_verify else
                          ("agent-compact" if compact else "agent"))))}
    timer = threading.Timer(args.run_timeout, agent.cancel)
    timer.start()
    request = task["prompt"] + f"\n\nOnly edit {module}. Verify with the test command."
    if structured and not executor:
        # The structured profile forbids tool calls, so the one file it may
        # rewrite has to travel with the request; otherwise a tiny model
        # rewrites from memory and silently drops existing validation.
        # Showing the tests too was measured and made results worse (the model
        # echoed them), so the request carries the implementation only.
        request += (f"\n\nCurrent {module}:\n```python\n"
                    f"{(workspace / module).read_text(encoding='utf-8')}\n```")
    try:
        final = agent.ask(request)
        outcome["final_answer"] = (final or "")[:500]
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        timer.cancel()
        outcome["tool_calls"] = observed["calls"]
        outcome["agent_ran_verify"] = observed["verify"]
        outcome["wall_ms"] = round((time.monotonic() - started) * 1000)
    return outcome


def evaluate(task: dict, mode: str, workspace: Path, args) -> dict:
    install(workspace, task["files"])
    client = make_client(args)
    if mode == "oneshot":
        outcome = run_oneshot(task, workspace, args, client)
    elif mode == "scaffold":
        outcome = run_scaffold(task, workspace, args, client)
    else:
        outcome = run_agent(task, workspace, args, client,
                            compact=(mode in {"agent-compact", "agent-repair",
                                              "agent-structured", "small-executor"}),
                            auto_verify=(mode in {"agent-repair", "agent-structured", "small-executor"}),
                            structured=(mode == "agent-structured"),
                            executor=(mode == "small-executor"))
    implementation = {name: (workspace / name).read_bytes()
                      for name in task["editable_files"] if (workspace / name).is_file()}
    verifier = verify_pristine(task, implementation, workspace.parent / "pristine")
    outcome.update({
        "task_id": task["id"], "cohort": task["cohort"], "model": args.model,
        "passed": bool(verifier["accepted"]),
        "tests_run": verifier.get("tests_run"),
        "tests_expected": task["expected_tests"],
        "verifier_exit": verifier.get("exit_code"),
        "model_calls": client.calls,
        "input_tokens": client.input_tokens,
        "output_tokens": client.output_tokens,
        "raw_samples": client.raw_samples,
    })
    return outcome


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="qwen2.5-coder:1.5b")
    parser.add_argument("--host", default="http://127.0.0.1:11434")
    parser.add_argument("--modes", default="oneshot,agent,agent-compact")
    parser.add_argument("--cohort", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--tasks", default="", help="comma-separated task ids")
    parser.add_argument("--k", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=10)
    parser.add_argument("--max-new-tokens", type=int, default=1200)
    parser.add_argument("--request-timeout", type=float, default=300)
    parser.add_argument("--run-timeout", type=float, default=600)
    parser.add_argument("--scaffold-rounds", type=int, default=3)
    parser.add_argument("--executor-rounds", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=0.3)
    parser.add_argument("--top-p", type=float, default=0.9)
    parser.add_argument("--repeat-penalty", type=float, default=1.15)
    parser.add_argument("--repeat-last-n", type=int, default=128)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    args.top_p = args.top_p

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        parser.error(f"unknown modes: {unknown}")
    tasks = load_tasks(args.cohort, [t.strip() for t in args.tasks.split(",") if t.strip()])
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows = []
    started = time.time()
    for mode in modes:
        for task in tasks:
            for repeat in range(args.k):
                workspace = out / "runs" / f"{mode}-{task['id']}-r{repeat}" / "workspace"
                print(json.dumps({"start": mode, "task": task["id"], "repeat": repeat}), flush=True)
                try:
                    row = evaluate(task, mode, workspace, args)
                except Exception as exc:
                    row = {"mode": mode, "task_id": task["id"], "cohort": task["cohort"],
                           "repeat": repeat, "passed": False,
                           "error": f"{type(exc).__name__}: {exc}"}
                row["repeat"] = repeat
                rows.append(row)
                (out / "runs.jsonl").open("a", encoding="utf-8").write(
                    json.dumps(row, ensure_ascii=False) + "\n")
                print(json.dumps({"done": mode, "task": task["id"], "passed": row.get("passed"),
                                  "wall_ms": row.get("wall_ms")}), flush=True)

    summary = {"model": args.model, "cohort": args.cohort, "k": args.k,
               "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "modes": {}}
    for mode in modes:
        subset = [r for r in rows if r["mode"] == mode]
        passed = sum(1 for r in subset if r.get("passed"))
        summary["modes"][mode] = {
            "tasks": len(subset), "passed": passed,
            "success_rate": round(passed / len(subset), 3) if subset else None,
            "wall_ms_mean": round(sum(r.get("wall_ms", 0) for r in subset) / len(subset)) if subset else None,
        }
    summary["elapsed_seconds"] = round(time.time() - started, 1)
    (out / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                     encoding="utf-8", newline="\n")
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
