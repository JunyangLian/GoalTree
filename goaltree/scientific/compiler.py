"""Deterministic Constraint Compiler (P1, minimum set).

Extracts *explicit* statistical / data-scope / method / output requirements from
the current-round prompt text. Only explicit conditions become constraints;
anything ambiguous or unsupported is recorded in ``unresolved_requirements`` and
is never treated as a hard requirement. No future-round prompt, answer key, or
grader detail is ever read here.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace

from .contracts import Constraint, Contract


def _first(pattern: str, text: str) -> str | None:
    m = re.search(pattern, text, flags=re.IGNORECASE)
    return m.group(0) if m else None


def _extract_adjusted_p(text: str) -> Constraint | None:
    # ``\badjusted\b`` will not match inside "unadjusted", so the unadjusted
    # clause falls through to the unadjusted extractor.
    m = re.search(r"\badjusted\s+p(?:-?\s*value)?\s*[<≤]\s*(0?\.\d+)", text, flags=re.IGNORECASE)
    if not m:
        return None
    return Constraint(
        id="", category="selection_criterion", operator="less_than",
        value=m.group(1), unit="adjusted_p", source_span=m.group(0),
        confidence="explicit", verification_strategy="table_recompute",
        raw_kind="statistical",
    )


def _extract_unadjusted_p(text: str) -> Constraint | None:
    m = re.search(r"(?<!adjusted\s)\b(?:unadjusted\s+)?p(?:-?\s*value)?\s*[<≤]\s*(0?\.\d+)",
                  text, flags=re.IGNORECASE)
    if not m:
        return None
    span = m.group(0)
    unit = "unadjusted_p" if "unadjusted" in span.lower() else "p"
    return Constraint(
        id="", category="selection_criterion", operator="less_than",
        value=m.group(1), unit=unit, source_span=span,
        confidence="explicit", verification_strategy="table_recompute",
        raw_kind="statistical",
    )


def _extract_logfc(text: str) -> Constraint | None:
    m = re.search(r"\|?log2?[ -]?fold[- ]change\|?\s*[>≥]\s*(\d+(?:\.\d+)?)",
                  text, flags=re.IGNORECASE)
    if not m:
        return None
    return Constraint(
        id="", category="selection_criterion", operator="greater_than",
        value=m.group(1), unit="logfc", source_span=m.group(0),
        confidence="explicit", verification_strategy="table_recompute",
        raw_kind="statistical",
    )


def _extract_correction_method(text: str) -> Constraint | None:
    methods = (
        ("bonferroni", "bonferroni"),
        ("benjamini[- ]?hochberg", "benjamini_hochberg"),
        (r"\bbh\b", "benjamini_hochberg"),
        (r"\bfdr\b", "fdr"),
        (r"false[- ]discovery[- ]rate", "fdr"),
    )
    for pattern, canonical in methods:
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            return Constraint(
                id="", category="method", operator="equals", value=canonical,
                unit=None, source_span=m.group(0), confidence="explicit",
                verification_strategy="method_check", raw_kind="method",
            )
    m = re.search(
        r"without\s+(?:multiple[-\s]+testing\s+)?correction|no\s+multiple\s+testing",
        text, flags=re.IGNORECASE,
    )
    if m:
        return Constraint(
            id="", category="method", operator="equals", value="unadjusted",
            unit=None, source_span=m.group(0), confidence="explicit",
            verification_strategy="method_check", raw_kind="method",
        )
    return None


def _extract_top_n(text: str) -> Constraint | None:
    span = _first(r"\btop[- ]?\s*\d+\b", text)
    if not span:
        return None
    value = re.search(r"\d+", span).group(0)
    return Constraint(
        id="", category="selection_criterion", operator="top_n",
        value=value, unit="genes", source_span=span,
        confidence="explicit", verification_strategy="list_count",
        raw_kind="statistical",
    )


def _extract_directions(text: str) -> list[Constraint]:
    out: list[Constraint] = []
    for word, canonical in (("upregulated", "up"), ("downregulated", "down")):
        m = re.search(rf"\b{word}\b", text, flags=re.IGNORECASE)
        if m:
            out.append(Constraint(
                id="", category="selection_criterion", operator="direction",
                value=canonical, unit=None, source_span=m.group(0),
                confidence="explicit", verification_strategy="direction_check",
                raw_kind="statistical",
            ))
    return out


def _extract_data_scope(text: str) -> Constraint | None:
    m = re.search(
        r"\b(all\s+(?:samples?|genes?|cells?|clones?)|"
        r"(?:both|the two|all)\s+[A-Za-z]+(?:\s+[A-Za-z]+){0,2}\s+cohorts?|"
        r"eight\s+clones?|four\s+(?:IMF|SF)\s+clones?)\b",
        text, flags=re.IGNORECASE,
    )
    if not m:
        return None
    return Constraint(
        id="", category="data_scope", operator="includes",
        value=m.group(0), unit=None, source_span=m.group(0),
        confidence="explicit", verification_strategy="provenance_check",
        raw_kind="scope",
    )


def _computation_hint(text: str, limit: int = 220) -> str:
    """The task's own sentence describing how to compute the statistic.

    Only sentences that mention a computation/normalisation are kept; this is a
    quotation of the current instruction, never a hint from an answer key.
    """
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if not sentence.strip():
            continue
        if re.search(r"log2|log2\(|normali[sz]|recompute|compute", sentence, flags=re.IGNORECASE):
            return sentence.strip()[:limit]
    return ""


def _required_group_labels(text: str) -> list[str]:
    """Quoted group labels the instruction explicitly mandates.

    Only sentences that name "group label(s)" are read, and only their quoted
    strings are kept; a task that does not state labels yields an empty list, so
    no check runs where it would be guessing.
    """
    labels: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text or ""):
        if not re.search(r"\bgroup\s+labels?\b", sentence, flags=re.IGNORECASE):
            continue
        # Only what follows the phrase describes labels: the same "sentence" may
        # also contain a schema block whose quoted keys/placeholders are not labels.
        tail = re.split(r"\bgroup\s+labels?\b", sentence, flags=re.IGNORECASE)[-1]
        for value in re.findall(r"['\"]([^'\"]{1,60})['\"]", tail):
            value = value.strip()
            if value and "<" not in value and value not in labels:
                labels.append(value)
    # The renderer also spells them out as a list under "Use the listed group
    # labels exactly ..." (the labels are prompt-visible, never the items).
    lines = (text or "").splitlines()
    for i, line in enumerate(lines):
        if not re.search(r"listed group labels", line, flags=re.IGNORECASE):
            continue
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                break
            m = re.match(r'\s*-\s*"[^"]+"\s*:\s*(\[.*\])\s*$', nxt)
            if not m:
                continue
            try:
                values = json.loads(m.group(1))
            except (ValueError, TypeError):
                continue
            for value in values:
                if isinstance(value, str) and value.strip() and value.strip() not in labels:
                    labels.append(value.strip())
    return labels


def compile_contract(subtask_id: str, prompt_text: str, contract_version: int = 1) -> Contract:
    """Compile explicit requirements from a single subtask turn's prompt."""
    candidates: list[Constraint] = []

    adjusted = _extract_adjusted_p(prompt_text)
    if adjusted:
        candidates.append(adjusted)
    else:
        unadjusted = _extract_unadjusted_p(prompt_text)
        if unadjusted:
            candidates.append(unadjusted)

    logfc = _extract_logfc(prompt_text)
    if logfc:
        candidates.append(logfc)

    method = _extract_correction_method(prompt_text)
    if method:
        candidates.append(method)

    top_n = _extract_top_n(prompt_text)
    if top_n:
        candidates.append(top_n)

    candidates.extend(_extract_directions(prompt_text))

    scope = _extract_data_scope(prompt_text)
    if scope:
        candidates.append(scope)

    constraints = [replace(c, id=f"C{i:03d}") for i, c in enumerate(candidates, start=1)]

    unresolved: list[str] = []
    has_stat = any(c.raw_kind == "statistical" for c in constraints)
    if not has_stat:
        unresolved.append("no explicit statistical threshold (p/logFC/top-N) detected")
    if method is None:
        unresolved.append("no explicit multiple-testing correction method detected")

    return Contract(
        contract_version=contract_version,
        subtask_id=subtask_id,
        source_prompt_hash=hashlib.sha256(prompt_text.encode("utf-8")).hexdigest(),
        constraints=constraints,
        unresolved_requirements=unresolved,
        computation_hint=_computation_hint(prompt_text),
        required_labels=_required_group_labels(prompt_text),
    )