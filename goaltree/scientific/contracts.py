"""Serializable science-constraint contracts.

These are internal, versioned objects — *not* the OmicsBench data schema. They
capture what this turn's instruction explicitly requires, plus every requirement
the compiler could not reliably parse. Nothing here is graded or read back into
the agent as an answer hint.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class Constraint:
    id: str
    category: str  # selection_criterion | data_scope | output_contract | method
    operator: str | None
    value: str | None
    unit: str | None
    source_span: str          # the matched text in the current-round prompt
    confidence: str = "explicit"   # explicit | inferred
    verification_strategy: str = "unknown"
    raw_kind: str = "statistical"


@dataclass
class Contract:
    contract_version: int = 1
    subtask_id: str = ""
    source_prompt_hash: str = ""
    constraints: list[Constraint] = field(default_factory=list)
    unresolved_requirements: list[str] = field(default_factory=list)
    # The task's own sentence describing *how* to compute the statistic, when it
    # states one (e.g. "compare on log2(raw read count + 1) without library-size
    # normalization"). Recovery quotes it so a correction cannot be satisfied by
    # swapping to a precomputed column.
    computation_hint: str = ""
    # Group labels the instruction names explicitly (e.g. "Use the group labels
    # 'higher in IMF' and 'higher in SF'"). Quoted from the task, never from a
    # grader/answer key; a renamed group scores zero under a label-matching grader.
    required_labels: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contract":
        return cls(
            contract_version=data.get("contract_version", 1),
            subtask_id=data.get("subtask_id", ""),
            source_prompt_hash=data.get("source_prompt_hash", ""),
            constraints=[Constraint(**c) for c in data.get("constraints", [])],
            unresolved_requirements=list(data.get("unresolved_requirements", [])),
            computation_hint=data.get("computation_hint", ""),
            required_labels=list(data.get("required_labels", [])),
        )