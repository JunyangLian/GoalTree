"""Archive outcomes, prompts, and run metadata. No score is invented: a missing
official grader score is recorded as ``null``, never zero-filled."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import RunSpec
from .adapter import NodeSubmission


def outcome_summary(submissions: list[NodeSubmission]) -> dict[str, Any]:
    """Counts by outcome. ``scored`` is never claimed here; only official grader
    output can produce a score."""
    counts: dict[str, int] = {}
    for s in submissions:
        counts[s.outcome] = counts.get(s.outcome, 0) + 1
    return {
        "nodes": {
            s.node_id: {
                "outcome": s.outcome,
                "tool_calls": len(s.tool_calls),
                "final_answer_length": len(s.final_answer or ""),
            }
            for s in submissions
        },
        "counts": counts,
        "official_scores": None,  # only set after the real grader runs
    }


def write_run_artifacts(artifact_dir: Path, *, spec: RunSpec, prompts: list[dict[str, Any]],
                        submissions: list[NodeSubmission]) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "manifest.json").write_text(
        json.dumps({
            "group": spec.group,
            "model": spec.model,
            "model_protocol": spec.model_protocol,
            "temperature": spec.temperature,
            "gated": spec.gated,
            "gated_threshold": spec.gated_threshold,
            "gated_max_attempts": spec.gated_max_attempts,
            "timeout_seconds": spec.timeout_seconds,
            "environment_provider": spec.environment_provider,
            "notes": spec.notes,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # visible_prompts.jsonl records only this run's worksheet, not future turns.
    with (artifact_dir / "visible_prompts.jsonl").open("w", encoding="utf-8") as fh:
        for p in prompts:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    subs_dir = artifact_dir / "submissions"
    subs_dir.mkdir(exist_ok=True)
    for s in submissions:
        (subs_dir / f"{s.node_id}.json").write_text(
            json.dumps({
                "node_id": s.node_id,
                "outcome": s.outcome,
                "answer": s.answer,
                "answer_text": s.answer_text,
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    (artifact_dir / "summary.json").write_text(
        json.dumps(outcome_summary(submissions), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return artifact_dir