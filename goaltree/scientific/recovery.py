"""Targeted Recovery (P3, minimal loop).

Produces a short, precise correction message only when the verifier found a
VIOLATION backed by deterministic evidence. It never reads answers, grader
labels, or future prompts; recovery is grounded solely in the current turn's
explicit requirement and the observed execution.
"""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import Contract
from .verifier import VerificationResult


@dataclass
class RecoveryPlan:
    should_recover: bool
    message: str | None
    violation_ids: list[str]


def build_recovery_message(violations: list[VerificationResult], contract: Contract) -> str | None:
    """Return a targeted correction message for confirmed violations, else None."""
    if not violations:
        return None
    lines: list[str] = ["Scientific validation detected a mismatch in this attempt."]
    for v in violations:
        constraint = next((c for c in contract.constraints if c.id == v.constraint_id), None)
        requirement = constraint.source_span if constraint else v.constraint_id
        lines.append("")
        lines.append(f"Requirement: {requirement}")
        lines.append(f"Observed execution: {v.reason}")
        if v.evidence:
            ev = v.evidence[0]
            lines.append(f"Evidence: {ev.path or '-'} (kind={ev.kind}, hash={(ev.hash or '')[:12]})")
    lines.append("")
    lines.append("Please correct the analysis and regenerate the answer from the data.")
    lines.append("Do not change the specified criteria to fit an expected score.")
    lines.append(
        "Do not substitute precomputed statistics from the input table for the required "
        "computation: recompute from the raw values exactly as the instruction specifies."
    )
    hint = getattr(contract, "computation_hint", "")
    if hint:
        lines.append(f"Required computation (from the task): {hint}")
    return "\n".join(lines)


def plan_recovery(
    results: list[VerificationResult],
    contract: Contract,
    *,
    attempt: int,
    max_attempts: int,
) -> RecoveryPlan:
    """Decide whether to recover, respecting a fixed retry cap."""
    blockers = [r for r in results if r.status == "VIOLATION" and r.severity == "blocking"]
    if not blockers:
        return RecoveryPlan(should_recover=False, message=None, violation_ids=[])
    if attempt >= max_attempts:
        return RecoveryPlan(
            should_recover=False,
            message=None,
            violation_ids=[r.constraint_id for r in blockers],
        )
    return RecoveryPlan(
        should_recover=True,
        message=build_recovery_message(blockers, contract),
        violation_ids=[r.constraint_id for r in blockers],
    )