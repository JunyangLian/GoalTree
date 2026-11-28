"""Live evidence gate: applies the science-constraint pipeline inside a real run.

It is the loop-level counterpart of the OmicsBench adapter wiring: the gate
observes the commands the agent actually executed, compiles the explicit
requirements of *this turn's* instruction, and returns one targeted correction
when the execution contradicts a blocking requirement.

It is inert unless the instruction states a requirement the compiler understands
(p / logFC / top-N / multiple-testing correction), so ordinary coding runs are
unaffected. It never reads answers, grader labels, or future instructions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .compiler import compile_contract
from .contracts import Contract
from .recovery import plan_recovery
from .verifier import VerificationResult, run_verifier


@dataclass
class ScientificGate:
    instruction: str
    max_corrections: int = 1
    commands: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    scripts: dict[str, str] = field(default_factory=dict)
    executed_scripts: set[str] = field(default_factory=set)
    corrections: int = 0
    state: dict[str, Any] = field(default_factory=dict)
    contract: Contract = field(init=False)

    def __post_init__(self) -> None:
        self.contract = compile_contract("current_turn", self.instruction or "")

    @property
    def enabled(self) -> bool:
        """Only instructions with at least one explicit requirement are policed."""
        return bool(self.contract.constraints)

    def observe(self, name: str, args: dict[str, Any] | None, content: str | None = None) -> None:
        """Record executed evidence.

        A script's body counts as evidence only once the agent actually runs that
        script: analyses are usually written to a file first, and treating every
        written file as executed would flag code that never ran.
        """
        if not self.enabled:
            return
        args = args or {}
        if name == "run_shell":
            command = str(args.get("command", ""))
            self.commands.append(command)
            for path, source in self.scripts.items():
                if path in self.executed_scripts:
                    continue
                basename = path.replace("\\", "/").rsplit("/", 1)[-1]
                if basename and basename in command:
                    self.executed_scripts.add(path)
                    self.commands.append(source)
        elif name in ("write_file", "apply_patch", "replace_in_file"):
            path = str(args.get("path", ""))
            self.artifacts.append(path)
            body = args.get("content") or args.get("patch") or ""
            if isinstance(body, str) and path.lower().endswith((".py", ".sh", ".r")):
                self.scripts[path] = body

    def verify(self, final_text: str | None = None) -> list[VerificationResult]:
        observed = "\n".join(self.commands)
        return run_verifier(
            self.contract,
            observed_execution=observed,
            sample_usage=observed,
            answer=None,  # the loop has no graded artifact; only the final text
            answer_text=(final_text or None),
            provenance=(observed or None),
        )

    def problem(self, final_text: str | None = None) -> str | None:
        """A correction message when *positively observed* drift blocks completion.

        Absence-of-evidence violations (e.g. "no correction term found") are not
        acted on here: an analysis may live inside a script file, so silence is
        not proof of drift. The gate only rejects what the executed commands show.
        """
        if not self.enabled:
            return None
        results = self.verify(final_text)
        blockers = [r for r in results
                    if r.status == "VIOLATION" and r.severity == "blocking"
                    and getattr(r, "basis", "positive") == "positive"]
        self.state = {
            "contract_id": self.contract.subtask_id,
            "constraints": [c.id for c in self.contract.constraints],
            "verification": [r.to_dict() for r in results],
            "corrections": self.corrections,
        }
        if not blockers or self.corrections >= self.max_corrections:
            return None
        plan = plan_recovery(results, self.contract,
                             attempt=self.corrections, max_attempts=self.max_corrections)
        if not plan.should_recover or not plan.message:
            return None
        self.corrections += 1
        self.state["corrections"] = self.corrections
        return plan.message
