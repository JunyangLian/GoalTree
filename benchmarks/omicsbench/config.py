"""Frozen ablation configuration for OmicsBench (development / public only).

These flags select which scientific-constraint layers are active in a run.
Nothing here starts a high-cost experiment; real runs require an authorized
model + container environment and must record their actual budget.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AblationGroup:
    name: str
    constraints: bool
    verification: bool
    recovery: bool
    purpose: str


ABLATION_GROUPS: dict[str, AblationGroup] = {
    "A": AblationGroup("A", False, False, False, "GoalTree baseline + official adapter"),
    "B": AblationGroup("B", True, False, False, "A + Constraint Compiler (record only)"),
    "C": AblationGroup("C", True, True, False, "B + Evidence Verifier"),
    "D": AblationGroup("D", True, True, True, "C + Targeted Recovery"),
}


@dataclass(frozen=True)
class RunSpec:
    """Frozen run metadata. ``gated``/``budget`` mirror the official runner's
    execution config; they must be set from the actual invocation, never guessed."""

    group: str
    model: str = ""
    model_protocol: str = ""
    temperature: float = 0.0
    gated: bool = False
    gated_threshold: float = 0.7
    gated_max_attempts: int = 3
    timeout_seconds: int = 1800
    environment_provider: str = "none"  # no Docker available in this environment
    notes: list[str] = field(default_factory=list)


def group_flags(group: str) -> dict[str, bool]:
    g = ABLATION_GROUPS[group]
    return {"constraints": g.constraints, "verification": g.verification, "recovery": g.recovery}