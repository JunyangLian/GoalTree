"""Offline entry point for the OmicsBench adapter.

This is a planning/dry-run tool in the current (no-Docker, Python 3.10)
environment. It validates an official question, renders each round's prompt
(without leaking future rounds or ground-truth values), and archives the plan.
It never invokes a real model or fabricates a score.

Example:
    python -X utf8 -m benchmarks.omicsbench.run --question <path.json> --group A
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import ABLATION_GROUPS, RunSpec, group_flags
from .prompt_renderer import render_first_node_prompt, render_node_prompt
from .question import load_question


def _render_plan(question) -> list[dict]:
    prompts: list[dict] = []
    order = question.topological_order()
    node_map = {n.id: n for n in question.nodes}
    for i, node_id in enumerate(order):
        node = node_map[node_id]
        if i == 0:
            text = render_first_node_prompt(question, node)
        else:
            text = render_node_prompt(node, previous_retried=False)
        prompts.append({"order": i, "node_id": node_id, "prompt": text})
    return prompts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Plan an OmicsBench run (offline).")
    parser.add_argument("--question", required=True, help="path to an official question JSON")
    parser.add_argument("--group", choices=sorted(ABLATION_GROUPS), default="A")
    parser.add_argument("--out", default="artifacts/omicsbench/plan")
    parser.add_argument("--model", default="", help="label only; no real model is called")
    args = parser.parse_args(argv)

    question = load_question(args.question)
    flags = group_flags(args.group)
    prompts = _render_plan(question)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    spec = RunSpec(
        group=args.group,
        model=args.model or "not-run",
        environment_provider="none",
        notes=[
            "offline dry-run: no real model and no container were executed",
            "real evaluation requires Docker + Python 3.13 + an authorized model",
            f"flags: {flags}",
        ],
    )
    manifest = {
        "question_id": question.id,
        "node_count": len(question.nodes),
        "topological_order": question.topological_order(),
        "group": spec.group,
        "flags": flags,
        "prompts_written": len(prompts),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    with (out / "visible_prompts.jsonl").open("w", encoding="utf-8") as fh:
        for p in prompts:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")

    print(json.dumps({**manifest, "note": spec.notes[0]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())