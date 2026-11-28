"""Real-model adapter smoke (wiring only).

Runs ONE node of an official OmicsBench question with a real model and a
``MockEnvironment``. The mock environment does NOT execute bioinformatics
commands, so this exercises the adapter wiring (prompt -> real model tool calls
-> observation -> answer collection) without producing a benchmark score or a
scientifically valid answer. Credentials are read from environment variables and
are never printed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from .env import HostEnvironment, MockEnvironment
from .adapter import OmicsBenchAdapter
from .prompt_renderer import render_first_node_prompt
from .question import load_question


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _safe_host(base_url: str) -> str:
    # Keep the host (no path, no credentials) for the manifest; never log keys.
    return base_url.split("//")[-1].split("/")[0]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Real-model OmicsBench adapter smoke (1 node).")
    parser.add_argument("--question", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--model", default=None, help="defaults to PICO_OPENAI_MODEL")
    parser.add_argument("--base-url", default=None, help="defaults to PICO_OPENAI_API_BASE")
    parser.add_argument("--api-format", default="chat_completions")
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-rounds", type=int, default=40)
    parser.add_argument("--recovery-attempts", type=int, default=3,
                        help="targeted re-prompt turns when the answer file is missing/invalid "
                             "(default 3 for parity with the experiment harness)")
    parser.add_argument("--scientific", action="store_true",
                        help="enable P1/P2/P3 constraint verification + targeted recovery")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--wall-timeout", type=float, default=120.0, help="seconds; 0 disables")
    parser.add_argument("--reasoning", default="low", choices=["low", "high", "max", ""],
                        help="GLM reasoning_effort; glm-5.3-flash is always-thinking")
    parser.add_argument("--env", choices=["mock", "host"], default="mock",
                        help="mock = inert; host = real local shell (Windows dev)")
    parser.add_argument("--data-root", default="D:/1Bio_Soft/omicsbench/data")
    parser.add_argument("--work-root", default="D:/1Bio_Soft/omicsbench/work")
    parser.add_argument("--workspace", default=None)
    args = parser.parse_args(argv)

    _load_dotenv(Path(args.env_file))

    model = args.model or os.environ.get("PICO_OPENAI_MODEL") or os.environ.get("PICO_DEEPSEEK_MODEL") or "deepseek-flash"
    base_url = args.base_url or os.environ.get("PICO_OPENAI_API_BASE") or "https://api.deepseek.com/v1"
    # Provider-specific key selection: BigModel uses its own credential, not the
    # PICO_OPENAI (DeepSeek/SiliconFlow) key. Selecting by host avoids sending a
    # cross-provider key.
    if "bigmodel" in _safe_host(base_url):
        api_key = os.environ.get("BIGMODEL_API_KEY", "").strip()
    else:
        api_key = (
            os.environ.get("PICO_OPENAI_API_KEY") or os.environ.get("PICO_DEEPSEEK_API_KEY") or ""
        ).strip()
    if not api_key:
        print(json.dumps({"error": "no API key found in environment"}, ensure_ascii=False))
        return 2

    from pico.providers.clients import OpenAICompatibleModelClient
    client = OpenAICompatibleModelClient(
        model, base_url, api_key, args.temperature, timeout=300, api_format=args.api_format,
    )
    if args.reasoning:
        # glm-5.3-flash is always-thinking; reduce cost/latency via reasoning_effort.
        client.request_options = {"reasoning_effort": args.reasoning}

    workspace_root = Path(args.workspace) if args.workspace else Path("tmp/omicsbench_smoke_ws")
    workspace_root.mkdir(parents=True, exist_ok=True)

    from pico import EnvironmentProfile, Pico, SessionStore, WorkspaceContext
    profile = EnvironmentProfile(
        name="omicsbench",
        role="You are a computational biologist operating in a containerized bioinformatics environment.",
        instructions=("Write graded answers to /workspace/answers/ as strict JSON.",),
    )
    agent = Pico(
        client,
        WorkspaceContext.build(workspace_root),
        SessionStore(workspace_root / ".pico/sessions"),
        environment=profile,
        wire_protocol="text",
        approval_policy="auto",
        max_depth=0,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        scientific_verification=False,  # the adapter exercises the science stages explicitly
    )

    question = load_question(args.question)
    node = question.nodes[0]  # first node only (wiring smoke)
    prompt = render_first_node_prompt(question, node)

    env = HostEnvironment(args.data_root, args.work_root) if args.env == "host" else MockEnvironment()
    adapter = OmicsBenchAdapter(question, env)

    scientific = None
    if args.scientific:
        import goaltree.scientific as scientific

    started = time.perf_counter()
    try:
        sub = adapter.drive_node(agent, prompt, node, max_rounds=args.max_rounds,
                                     wall_timeout=args.wall_timeout or None,
                                     recovery_attempts=args.recovery_attempts,
                                     scientific=scientific)
        error = None
    except Exception as exc:  # capture and report; the wiring smoke must not crash silently
        sub = None
        error = f"{type(exc).__name__}: {exc}"
    wall = round(time.perf_counter() - started, 3)

    result = {
        "scope": "real-model wiring smoke; mock environment; NO benchmark score",
        "question_id": question.id,
        "node_id": node.id,
        "model": model,
        "base_host": _safe_host(base_url),
        "api_format": args.api_format,
        "wire_protocol": "text",
        "temperature": args.temperature,
        "reasoning_effort": args.reasoning or None,
        "wall_timeout_seconds": args.wall_timeout,
        "wall_seconds": wall,
        "outcome": sub.outcome if sub else None,
        "tool_calls": getattr(env, "tool_calls", []),
        "final_answer": sub.final_answer if sub else None,
        "answer": sub.answer if sub else None,
        "scientific_enabled": bool(scientific),
        "constraint_count": len(getattr(sub.contract, "constraints", [])) if sub and sub.contract else 0,
        "verification": [r.to_dict() if hasattr(r, "to_dict") else vars(r) for r in (sub.verification if sub else [])],
        "error": error,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())