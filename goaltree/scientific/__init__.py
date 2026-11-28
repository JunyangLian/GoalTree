"""GoalTree scientific-constraint pipeline (default off).

``goaltree.scientific`` introduces the Science Constraint Compiler, Evidence
Verifier, and Targeted Recovery described in the OmicsBench ablation design.
None of it runs unless explicitly imported/enabled; the ordinary coding-agent
path is unchanged.
"""
from .contracts import Constraint, Contract
from .compiler import compile_contract
from .evidence import Evidence, hash_text
from .recovery import RecoveryPlan, plan_recovery
from .verifier import (
    VerificationResult,
    run_verifier,
    verify_criteria_drift,
    verify_output_labels,
)

__all__ = [
    "Constraint",
    "Contract",
    "Evidence",
    "RecoveryPlan",
    "VerificationResult",
    "compile_contract",
    "hash_text",
    "plan_recovery",
    "run_verifier",
    "verify_criteria_drift",
    "verify_output_labels",
]