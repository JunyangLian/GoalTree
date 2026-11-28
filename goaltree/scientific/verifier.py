"""Evidence-aware verifier (P2). V1 = criteria drift, V2 = scope drift,
V3 = unsupported answer provenance. Deterministic evidence only; no scores,
no grader labels, no answer-key feedback.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import Constraint, Contract
from .evidence import Evidence, hash_text


@dataclass
class VerificationResult:
    constraint_id: str
    status: str            # PASS | VIOLATION | UNVERIFIED | NOT_APPLICABLE
    severity: str          # info | warning | blocking
    evidence: list[Evidence] = field(default_factory=list)
    reason: str = ""
    recommended_action: str | None = None
    # "positive" = the executed evidence shows the drift; "absence" = a required
    # signal was not found. A live gate may act on the former without risking a
    # false rejection when the analysis lives inside a script file.
    basis: str = "positive"

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "status": self.status,
            "severity": self.severity,
            "basis": self.basis,
            "evidence": [
                {"kind": e.kind, "path": e.path, "hash": e.hash} for e in self.evidence
            ],
            "reason": self.reason,
            "recommended_action": self.recommended_action,
        }


# Terms that indicate the analysis used multiple-testing correction. Detecting
# one of these when the contract demands unadjusted p is criteria drift.
_ADJUST_TERMS = (
    r"\bpadj\b", r"p\.adjust", r"p_adj", r"\bfdr\b",
    r"\bbh\b", r"benjamini", r"bonferroni", r"\bqvalue\b", r"q\.value",
)


def _has_adjust(text: str) -> bool:
    return any(re.search(t, text, flags=re.IGNORECASE) for t in _ADJUST_TERMS)


# Adjusted p-values only count as *drift* when they are used for selection (a
# comparison, or an explicit correction call). A real run selected on a freshly
# computed raw p and printed `'padj=', r.padj` for cross-reference; treating any
# mention as drift produced a false blocking violation and a wasted recovery.
_ADJUST_SELECTION = (
    r"\bpadj\b['\"\]]*\s*(?:<=|<|≤|>=|>|≥)",
    r"(?:<=|<|≤)\s*[\w\.\[\]\"']*\bpadj\b",
    r"p\.adjust\s*\(",
    r"p_adjust\s*\(",
    r"multipletests\s*\(",
    r"\bfdr\b\s*(?:<=|<|≤)",
    r"q\.value\s*\(",
)


def _adjust_selection_hits(text: str) -> list[str]:
    """Readable snippets proving adjusted p-values were used to select."""
    hits: list[str] = []
    for pattern in _ADJUST_SELECTION:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            hits.append(m.group(0).strip())
    return hits


def _has_any(text: str, *words: str) -> bool:
    return any(re.search(w, text, flags=re.IGNORECASE) for w in words)


def _answer_items(answer: Any) -> list[Any] | None:
    """Flatten the answer into its listed items, or None if not list-shaped.

    Accepts a flat list directly, or a dict whose list-valued leaf values are
    treated as item lists (top-N style answers).
    """
    if isinstance(answer, list):
        return answer
    if isinstance(answer, dict):
        items: list[Any] = []
        for value in answer.values():
            if isinstance(value, list):
                items.extend(value)
        return items if items else None
    return None


def verify_criteria_drift(contract: Contract, observed_text: str, answer: Any = None) -> list[VerificationResult]:
    """V1: detect replacement of an explicit statistical method/threshold.

    ``observed_text`` is the concatenated content of the scripts/commands the
    agent actually executed (from real tool observations), never the model's
    prose claim. ``answer`` (parsed answer file) is optional extra evidence for
    checks that need the produced items (e.g. Top-N), not just the command text.

    ``data_scope`` constraints are skipped here; ``verify_scope_drift`` covers
    them.
    """
    results: list[VerificationResult] = []
    hint = getattr(contract, "computation_hint", "")
    for c in contract.constraints:
        if c.category == "method":
            results.append(_verify_method(c, observed_text))
        elif c.category == "selection_criterion" and c.raw_kind == "statistical":
            results.append(_verify_criterion(c, observed_text, answer, hint))
        # data_scope and anything else are handled by their dedicated verifiers.
    return results


# A statistic recomputed from raw values shows up as a test function call. A
# bare column comparison (`df.pvalue < 0.05`) only reuses the input table's
# precomputed numbers, which is not the computation the task asked for.
_RECOMPUTE_TERMS = (
    r"\bttest\w*\s*\(",
    r"\bwilcoxon\b",
    r"\bmannwhitneyu\b",
    r"\bf_oneway\b",
    r"\bstat_compare\w*",
    r"\bscipy\.stats\b",
    r"\bstats\.\w+\s*\(",
    r"\blm\s*\(",
)

# Computation requirements that imply a statistic must be derived from the raw
# values rather than read off a column.
_RECOMPUTE_HINT = (r"log2", r"normali[sz]", r"recompute", r"raw (?:read )?count")


def _requires_recompute(computation_hint: str) -> bool:
    return bool(computation_hint) and _has_any(computation_hint, *_RECOMPUTE_HINT)


def _has_recompute_evidence(observed: str) -> bool:
    return _has_any(observed, *_RECOMPUTE_TERMS)


def _verify_method(c: Constraint, observed: str) -> VerificationResult:
    evidence = [Evidence.from_text("script_or_command", observed)]
    if not observed.strip():
        return VerificationResult(c.id, "UNVERIFIED", "info", evidence,
                                  "no executed command/script evidence available")

    value = (c.value or "").lower()
    if value == "unadjusted":
        sites = _adjust_selection_hits(observed)
        if sites:
            return VerificationResult(
                c.id, "VIOLATION", "blocking", evidence,
                f"required unadjusted p, but observed multiple-testing correction "
                f"used for selection ({', '.join(sites)}).",
                "use unadjusted p-values for the selection; do not apply FDR/BH/Bonferroni.",
            )
        if _has_adjust(observed):
            return VerificationResult(
                c.id, "UNVERIFIED", "warning", evidence,
                "adjusted-p terms appear in the executed commands, but not in a selection "
                "comparison; they may only be printed for cross-reference. Not treated as drift.",
            )
        if _has_any(observed, r"\bpvalue\b", r"\bp\.value", r"\bp\b\s*[<≤]"):
            return VerificationResult(c.id, "PASS", "info", evidence,
                                      "observed raw p-value selection, consistent with unadjusted requirement.")
        return VerificationResult(c.id, "UNVERIFIED", "warning", evidence,
                                  "no p-value selection term found in executed evidence.")

    if value == "bonferroni":
        if _has_any(observed, r"\bbonferroni\b"):
            return VerificationResult(c.id, "PASS", "info", evidence, "observed Bonferroni correction.")
        drifted = _adjust_selection_hits(observed)
        if drifted:
            return VerificationResult(
                c.id, "VIOLATION", "blocking", evidence,
                f"required Bonferroni, but observed {', '.join(drifted)} instead.",
                "replace the correction with Bonferroni.",
            )
        return VerificationResult(c.id, "UNVERIFIED", "warning", evidence,
                                  "no correction method found in executed evidence.")

    if value in ("benjamini_hochberg", "fdr"):
        if _has_any(observed, r"benjamini", r"\bbh\b", r"\bfdr\b"):
            return VerificationResult(c.id, "PASS", "info", evidence, "observed BH/FDR correction.")
        # A passing mention of Bonferroni in a comment is not evidence of using it;
        # require an explicit method assignment or correction call.
        if _has_any(observed, r"method\s*=\s*['\"]?bonferroni", r"bonferroni\s*\("):
            return VerificationResult(
                c.id, "VIOLATION", "blocking", evidence,
                "required BH/FDR, but observed Bonferroni.",
                "replace the correction with Benjamini-Hochberg/FDR.",
            )
        return VerificationResult(c.id, "UNVERIFIED", "warning", evidence,
                                  "no correction method found in executed evidence.")

    return VerificationResult(c.id, "NOT_APPLICABLE", "info", evidence,
                              f"unknown method requirement value={value!r}")


def _verify_criterion(c: Constraint, observed: str, answer: Any = None,
                      computation_hint: str = "") -> VerificationResult:
    evidence = [Evidence.from_text("script_or_command", observed)]
    if answer is not None:
        evidence.append(Evidence.from_text("answer", json.dumps(answer, ensure_ascii=False)))
    if not observed.strip():
        return VerificationResult(c.id, "UNVERIFIED", "info", evidence,
                                  "no executed command/script evidence available")

    # Top-N: the produced item count is directly checkable from the answer.
    if c.operator == "top_n":
        items = _answer_items(answer)
        if items is None:
            return VerificationResult(c.id, "UNVERIFIED", "warning", evidence,
                                      "Top-N check requires the produced answer list.")
        try:
            n = int(c.value or 0)
        except (TypeError, ValueError):
            return VerificationResult(c.id, "NOT_APPLICABLE", "info", evidence,
                                      f"bad Top-N value {c.value!r}")
        if len(items) == n:
            return VerificationResult(c.id, "PASS", "info", evidence,
                                      f"answer lists exactly top {n} items.")
        return VerificationResult(c.id, "VIOLATION", "blocking", evidence,
                                  f"required top {n} items, but the answer lists {len(items)}.",
                                  "include exactly the requested number of items.")

    # Direction needs per-gene fold-change evidence; the final list alone is not
    # enough to confirm it, so it is UNVERIFIED rather than PASS or VIOLATION.
    if c.operator == "direction":
        return VerificationResult(c.id, "UNVERIFIED", "warning", evidence,
                                  f"direction ({c.value}) needs per-gene fold-change evidence; "
                                  "not verifiable from command text alone.")

    unit = (c.unit or "").lower()
    if unit in ("p", "unadjusted_p"):
        sites = _adjust_selection_hits(observed)
        if sites:
            return VerificationResult(
                c.id, "VIOLATION", "blocking", evidence,
                f"selection criterion used adjusted p-values ({', '.join(sites)}) but the contract "
                "requires an unadjusted p threshold.",
                "recompute the selection using unadjusted p-values.",
            )
        if _has_adjust(observed):
            return VerificationResult(
                c.id, "UNVERIFIED", "warning", evidence,
                "adjusted-p terms appear only in inspection output, not in a selection comparison; "
                "cannot confirm the selection used raw p, but this is not evidence of drift.",
            )
        value = c.value
        # When the task states *how* to compute the statistic (e.g. from raw
        # counts), a bare column comparison only reuses precomputed numbers; that
        # is "no evidence of the required computation", not a pass. Observed in
        # expR22/expR24: the model switched from padj to the table's `pvalue`
        # column and the verifier still said PASS while the answer was wrong.
        if answer is not None and _requires_recompute(computation_hint) \
                and not _has_recompute_evidence(observed):
            return VerificationResult(
                c.id, "UNVERIFIED", "warning", evidence,
                "the task requires computing the statistic from the raw values, but no test "
                "call (ttest/wilcoxon/…) appears in the executed evidence; the answer may "
                "reuse precomputed columns from the input table.",
            )
        if answer is not None:
            return VerificationResult(
                c.id, "PASS", "info", evidence,
                "observed unadjusted p selection and a produced answer; exact cutoff "
                "value was not independently recomputed from the data.",
            )
        if value and value not in observed:
            return VerificationResult(
                c.id, "UNVERIFIED", "warning", evidence,
                f"required p threshold ({value}) not found in executed evidence; "
                "cannot confirm the threshold was preserved.",
            )
        return VerificationResult(c.id, "PASS", "info", evidence,
                                  "observed unadjusted p selection at the required threshold.")

    if unit == "adjusted_p":
        if not _has_adjust(observed):
            return VerificationResult(
                c.id, "VIOLATION", "blocking", evidence,
                "contract requires adjusted p, but no correction term found in executed evidence.",
                "apply the specified multiple-testing correction before selecting.",
                basis="absence",
            )
        return VerificationResult(c.id, "PASS", "info", evidence,
                                  "observed adjusted (corrected) p selection.")

    if unit == "logfc":
        value = c.value
        if answer is not None:
            return VerificationResult(c.id, "PASS", "info", evidence,
                                      "observed logFC selection and a produced answer.")
        if value and value not in observed:
            return VerificationResult(c.id, "UNVERIFIED", "warning", evidence,
                                      f"logFC threshold ({value}) not found in executed evidence.")
        return VerificationResult(c.id, "PASS", "info", evidence,
                                  "observed logFC threshold consistent with contract.")

    return VerificationResult(c.id, "NOT_APPLICABLE", "info", evidence,
                              f"unhandled criterion unit={unit!r}")


def verify_scope_drift(contract: Contract, sample_usage: str | None) -> list[VerificationResult]:
    """V2: light provenance check. Cannot infer 'did not analyze full data' from
    a small output table alone; only explicit sample/feature evidence counts."""
    results: list[VerificationResult] = []
    for c in contract.constraints:
        if c.category != "data_scope":
            continue  # covered by criteria-drift or unsupported-answer verifiers
        if not sample_usage:
            results.append(VerificationResult(
                c.id, "UNVERIFIED", "warning",
                reason="no verifiable sample/feature usage list available; cannot confirm scope.",
            ))
            continue
        ev = [Evidence.from_text("sample_list", sample_usage)]
        # If the required scope value names a cohort/samples, treat presence as pass,
        # absence as unverified (not violation: absence of a list is weak evidence).
        span_value = (c.value or "").lower()
        if span_value and span_value in sample_usage.lower():
            results.append(VerificationResult(c.id, "PASS", "info", ev,
                                              "observed the required samples/cohort in the usage record."))
        else:
            results.append(VerificationResult(
                c.id, "UNVERIFIED", "warning", ev,
                "cannot confirm the required scope from the usage record; "
                "a small final table does not by itself prove under-analysis.",
            ))
    return results


def verify_output_labels(contract: Contract, answer: Any = None) -> list[VerificationResult]:
    """V5: the answer must use the group labels the instruction names.

    A grouped-entity grader matches labels (case-insensitively), so a correct
    membership under an invented label ("up_in_IMF") still scores zero. expR27
    showed exactly that after a correction turn, so this is checked directly
    against the task's own quoted labels — never against an answer key.

    Only *invented* labels are a defect: a group that legitimately has no items
    may be omitted, so an unused required label is not a violation.
    """
    required = list(getattr(contract, "required_labels", []) or [])
    if not required or answer is None:
        return []
    items = _answer_items(answer)
    groups = [it["group"] for it in (items or [])
              if isinstance(it, dict) and isinstance(it.get("group"), str)]
    ev = [Evidence.from_text("answer", json.dumps(answer, ensure_ascii=False))]
    if not groups:
        return [VerificationResult("output_labels", "UNVERIFIED", "info", ev,
                                  "no grouped items in the answer; label check not applicable.")]
    want = [r.strip().lower() for r in required]
    got = [g.strip().lower() for g in groups]
    extra = [g for g in groups if g.strip().lower() not in want]
    if extra:
        return [VerificationResult(
            "output_labels", "VIOLATION", "blocking", ev,
            f"the instruction only defines the group labels {required}, but the answer uses "
            f"invented labels {extra}. Labels are matched, so an invented group scores zero.",
            f"rewrite the answer using only the labels {required}.",
        )]
    missing = [r for r in required if r.strip().lower() not in got]
    if missing:
        # A group with no qualifying items may legitimately be omitted; only an
        # invented label is a defect.
        return [VerificationResult(
            "output_labels", "PASS", "info", ev,
            f"answer uses only the required group labels; not used: {missing} "
            f"(may legitimately have no items).",
        )]
    return [VerificationResult("output_labels", "PASS", "info", ev,
                              f"answer uses the required group labels {required}.")]


def verify_unsupported_answer(answer_text: str | None, provenance: str | None) -> VerificationResult:
    """V3: whether the final answer traces to an actual produced artifact."""
    if not answer_text:
        return VerificationResult("answer_provenance", "VIOLATION", "blocking",
                                  reason="no answer file was produced.", basis="absence")
    if not provenance:
        return VerificationResult(
            "answer_provenance", "UNVERIFIED", "warning",
            reason="answer present but no script/output provenance available to trace it.",
        )
    ev = [Evidence.from_text("answer", answer_text), Evidence.from_text("provenance", provenance)]
    # Non-empty provenance is the minimum traceability bar; absence is UNVERIFIED,
    # not a claim of wrongness.
    return VerificationResult("answer_provenance", "PASS", "info", ev,
                              "answer has an attached provenance trace.")


def run_verifier(
    contract: Contract,
    observed_execution: str = "",
    sample_usage: str | None = None,
    answer: Any = None,
    answer_text: str | None = None,
    provenance: str | None = None,
) -> list[VerificationResult]:
    results = verify_criteria_drift(contract, observed_execution, answer)
    results.extend(verify_scope_drift(contract, sample_usage))
    results.extend(verify_output_labels(contract, answer))
    results.append(verify_unsupported_answer(answer_text, provenance))
    return results