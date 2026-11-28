"""Offline tests for the GoalTree science-constraint pipeline (P1/P2/P3)."""
from __future__ import annotations

import json

from goaltree.scientific import (
    compile_contract,
    plan_recovery,
    run_verifier,
    verify_criteria_drift,
)
from goaltree.scientific.verifier import VerificationResult


def _contract(prompt: str, subtask_id: str = "t0") -> "object":
    return compile_contract(subtask_id, prompt)


# --- P1: Constraint Compiler -------------------------------------------------

def test_bonferroni_is_preserved_not_defaulted():
    c = _contract("apply Bonferroni correction to adjusted p-values.")
    methods = [k for k in c.constraints if k.category == "method"]
    assert len(methods) == 1
    assert methods[0].value == "bonferroni"
    assert methods[0].source_span.lower() == "bonferroni"


def test_adjusted_p_threshold_extracted():
    c = _contract("keep genes with adjusted p < 0.05.")
    crit = [k for k in c.constraints if k.unit == "adjusted_p"]
    assert len(crit) == 1
    assert crit[0].operator == "less_than" and crit[0].value == "0.05"


def test_logfc_and_topn_extracted():
    c = _contract("select genes with |log2 fold change| > 1 and report the top 100 genes.")
    logfc = [k for k in c.constraints if k.unit == "logfc"]
    top = [k for k in c.constraints if k.operator == "top_n"]
    assert len(logfc) == 1 and logfc[0].value == "1"
    assert len(top) == 1 and top[0].value == "100"


def test_omitted_threshold_is_not_fabricated():
    c = _contract("describe the data, no statistical test required.")
    assert c.unresolved_requirements
    assert not any(k.raw_kind == "statistical" for k in c.constraints)


def test_prompt_change_gives_distinct_contract_hash():
    a = _contract("FDR < 0.05", "x")
    b = _contract("Bonferroni p < 0.01", "x")
    assert a.source_prompt_hash != b.source_prompt_hash


# --- P2: Evidence Verifier (criteria drift) ---------------------------------

def test_unadjusted_vs_adjusted_is_a_violation():
    c = _contract("report markers with unadjusted p < 0.05.")
    results = verify_criteria_drift(c, "y = results.padj < 0.05  # used adjusted")
    method_results = [r for r in results if r.constraint_id in {k.id for k in c.constraints if k.category == "method" or (k.unit in ("p", "unadjusted_p"))}]
    assert any(r.status == "VIOLATION" for r in method_results)


def test_correct_unadjusted_analysis_not_blocked():
    c = _contract("report markers with unadjusted p < 0.05.")
    results = verify_criteria_drift(c, "y = pvalue < 0.05")
    assert any(r.status == "PASS" for r in results)
    assert not any(r.status == "VIOLATION" for r in results)


def test_incidental_padj_print_is_not_blocking_drift():
    """Printing the padj column for cross-reference must not count as drift.

    Regression from a real node1 run: raw p was recomputed with a t-test, but the
    command also printed ``'padj=', r.padj``; the old check flagged a blocking
    VIOLATION and burned the single recovery attempt.
    """
    c = _contract("report markers with unadjusted p < 0.05.")
    observed = (
        "t,p=ttest_ind(im,sf)\n"
        "print(m, 'p=', p, 'padj=', r.padj, r.category)\n"
    )
    results = verify_criteria_drift(c, observed)
    assert not any(r.status == "VIOLATION" for r in results)
    assert any(r.status == "UNVERIFIED" and r.severity == "warning" for r in results)


def test_padj_used_in_selection_is_still_blocking():
    c = _contract("report markers with unadjusted p < 0.05.")
    for observed in (
        "up = df[df.padj < 0.05]",
        "sel = table[table['padj'] <= 0.05]",
        "p = multipletests(raw, method='fdr_bh')",
    ):
        results = verify_criteria_drift(c, observed)
        assert any(r.status == "VIOLATION" and r.severity == "blocking" for r in results), observed


def test_incidental_fdr_mention_does_not_violate_bonferroni_requirement():
    c = _contract("correct with Bonferroni.")
    results = verify_criteria_drift(c, "print(df.columns)  # includes padj, fdr columns")
    assert not any(r.status == "VIOLATION" for r in results)


def test_bonferroni_replaced_by_bh_is_violation():
    c = _contract("correct with Bonferroni.")
    results = verify_criteria_drift(c, "padj = p.adjust(p, method='BH')")
    assert any(r.status == "VIOLATION" for r in results)


def test_bonferroni_used_is_pass():
    c = _contract("correct with Bonferroni.")
    results = verify_criteria_drift(c, "padj = p.adjust(p, method='bonferroni')")
    assert any(r.status == "PASS" for r in results)


def test_required_bh_replaced_by_bonferroni_is_violation():
    c = _contract("correct with Benjamini-Hochberg.")
    results = verify_criteria_drift(c, "padj = p.adjust(p, method='bonferroni')")
    assert any(r.status == "VIOLATION" for r in results)


def test_no_method_constraint_does_not_false_reject_legal_choice():
    # A prompt that permits multiple methods yields no method constraint, so a
    # script using FDR must not be flagged as drift.
    c = _contract("use any reasonable multiple-testing correction you prefer.")
    results = verify_criteria_drift(c, "padj = p.adjust(p, method='BH')")
    assert not any(r.status == "VIOLATION" for r in results)


def test_missing_trace_is_unverified_not_violation():
    c = _contract("correct with Bonferroni.")
    results = verify_criteria_drift(c, "")
    method = [r for r in results if r.constraint_id in {k.id for k in c.constraints if k.category == "method"}]
    assert method and method[0].status == "UNVERIFIED"


def test_empty_answer_is_a_violation_not_pass():
    from goaltree.scientific.verifier import verify_unsupported_answer
    r = verify_unsupported_answer("", provenance="some_trace.txt")
    assert r.status == "VIOLATION"


def test_missing_provenance_is_unverified():
    from goaltree.scientific.verifier import verify_unsupported_answer
    r = verify_unsupported_answer('{"origin": "x"}', provenance=None)
    assert r.status == "UNVERIFIED"


def test_top_n_verified_against_answer():
    c = _contract("report the top 3 genes.")
    ok = verify_criteria_drift(c, "python -c 'compute'", answer={"genes": ["A", "B", "C"]})
    top = [r for r in ok if r.constraint_id in {k.id for k in c.constraints if k.operator == "top_n"}]
    assert top and top[0].status == "PASS"

    bad = verify_criteria_drift(c, "python -c 'compute'", answer={"genes": ["A", "B"]})
    top_bad = [r for r in bad if r.constraint_id in {k.id for k in c.constraints if k.operator == "top_n"}]
    assert top_bad and top_bad[0].status == "VIOLATION"


def test_direction_runs_but_unverified_without_result_table():
    c = _contract("report only upregulated genes.")
    results = verify_criteria_drift(c, "python -c 'compute'", answer={"genes": ["A"]})
    direction = [r for r in results if r.constraint_id in {k.id for k in c.constraints if k.operator == "direction"}]
    assert direction and direction[0].status == "UNVERIFIED"


def test_p_threshold_with_answer_converges_to_pass():
    c = _contract("report markers with unadjusted p < 0.05.")
    results = verify_criteria_drift(c, "y = pvalue < 0.05", answer={"marker_direction": [{"group": "up", "items": ["A"]}]})
    p_results = [r for r in results if r.constraint_id in {k.id for k in c.constraints if k.unit in ("p", "unadjusted_p")}]
    assert p_results and p_results[0].status == "PASS"


# --- P3: Targeted Recovery ---------------------------------------------------

def test_precomputed_column_use_is_unverified_not_pass():
    """Reusing the table's pvalue column is not the required computation.

    The task asks for a test on log2(raw counts); a bare `df.pvalue < 0.05`
    comparison only reuses precomputed numbers (ITGB1 stays in the answer), so
    the verifier must stop reporting PASS.
    """
    prompt = ("Report markers with unadjusted p < 0.05. Compute each marker on "
              "log2(raw read count + 1) without library-size normalization.")
    c = _contract(prompt)
    answer = {"marker_direction": [{"group": "higher in IMF", "items": ["PDGFRA"]}]}

    reuse = verify_criteria_drift(c, "sel = df[df.pvalue < 0.05]", answer)
    assert any(r.status == "UNVERIFIED" and "precomputed" in r.reason for r in reuse)
    assert not any(r.status == "PASS" and r.constraint_id == "C001" for r in reuse)

    recomputed = verify_criteria_drift(
        c, "t,p = stats.ttest_ind(np.log2(im+1), np.log2(sf+1))", answer)
    assert any(r.status == "PASS" for r in recomputed)


def test_no_computation_hint_keeps_previous_pass_behaviour():
    """Without a stated computation, a column comparison is still acceptable."""
    c = _contract("Report markers with unadjusted p < 0.05.")
    answer = {"marker_direction": [{"group": "higher in IMF", "items": ["PDGFRA"]}]}
    results = verify_criteria_drift(c, "sel = df[df.pvalue < 0.05]", answer)
    assert any(r.status == "PASS" for r in results)


def test_required_group_labels_are_extracted_from_the_instruction():
    c = _contract("Report depot-biased markers grouped by direction. "
                  "Use the group labels 'higher in IMF' and 'higher in SF'.")
    assert c.required_labels == ["higher in IMF", "higher in SF"]


def test_no_label_sentence_means_no_label_check():
    c = _contract("Report markers with unadjusted p < 0.05.")
    assert c.required_labels == []


def test_group_labels_are_read_from_the_rendered_grouped_fields_block():
    """The renderer lists labels under 'Use the listed group labels exactly ...'."""
    prompt = ('Grouped fields:\n'
              'Use the listed group labels exactly for each grouped answer field.\n'
              '- "marker_direction": ["higher in A", "higher in B"]\n')
    c = _contract(prompt)
    assert c.required_labels == ["higher in A", "higher in B"]


def test_using_a_subset_of_required_labels_is_not_a_violation():
    """A group with no qualifying items may legitimately be omitted."""
    c = _contract("Group the markers by direction. "
                  "Use the group labels 'higher in IMF' and 'higher in SF'.")
    answer = {"marker_direction": [{"group": "higher in IMF", "items": ["PDGFRA"]}]}
    results = run_verifier(c, observed_execution="ttest_ind(x, y)", answer=answer,
                           answer_text=json.dumps(answer), provenance="ttest_ind(x, y)")
    labels = [r for r in results if r.constraint_id == "output_labels"]
    assert labels and labels[0].status == "PASS"
    assert labels[0].severity == "info"


def test_renamed_group_labels_are_a_blocking_violation():
    """expR27: correct membership under an invented label still scores zero."""
    c = _contract("Group the markers by direction. "
                  "Use the group labels 'higher in IMF' and 'higher in SF'.")
    answer = {"marker_direction": [
        {"group": "up_in_IMF", "items": ["PDGFRA", "CD34"]},
        {"group": "up_in_SC", "items": ["PPARG"]},
    ]}
    results = run_verifier(c, observed_execution="ttest_ind(x, y)", answer=answer,
                           answer_text=json.dumps(answer), provenance="ttest_ind(x, y)")
    labels = [r for r in results if r.constraint_id == "output_labels"]
    assert labels and labels[0].status == "VIOLATION"
    assert labels[0].severity == "blocking"


def test_required_group_labels_pass_case_insensitively():
    c = _contract("Group the markers by direction. "
                  "Use the group labels 'higher in IMF' and 'higher in SF'.")
    answer = {"marker_direction": [
        {"group": "Higher in IMF", "items": ["PDGFRA", "CD34"]},
        {"group": "higher in sf", "items": ["PPARG"]},
    ]}
    results = run_verifier(c, observed_execution="ttest_ind(x, y)", answer=answer,
                           answer_text=json.dumps(answer), provenance="ttest_ind(x, y)")
    labels = [r for r in results if r.constraint_id == "output_labels"]
    assert labels and labels[0].status == "PASS"


def test_computation_hint_is_quoted_in_recovery():
    """Recovery must repeat the task's own computation sentence, not just the cutoff."""
    prompt = ("Report depot-biased markers with unadjusted p < 0.05. "
              "Compute each marker on log2(raw read count + 1) without library-size normalization.")
    c = _contract(prompt)
    assert "log2(raw read count + 1)" in c.computation_hint

    results = verify_criteria_drift(c, "sel = df[df.padj < 0.05]")
    plan = plan_recovery(results, c, attempt=0, max_attempts=1)
    assert "Required computation (from the task):" in plan.message
    assert "log2(raw read count + 1)" in plan.message


def test_recovery_message_forbids_substituting_precomputed_stats():
    """Recovery must not let the model swap padj for the table's pvalue column.

    Observed in a controlled drift run: after being told to stop using adjusted p,
    the model filtered the table's unadjusted `pvalue` column instead of
    recomputing from raw counts, which keeps a wrong marker.
    """
    c = _contract("report markers with unadjusted p < 0.05.")
    results = verify_criteria_drift(c, "sel = df[df.padj < 0.05]")
    plan = plan_recovery(results, c, attempt=0, max_attempts=1)
    assert plan.should_recover
    assert "precomputed statistics" in plan.message
    assert "recompute from the raw values" in plan.message


def test_recovery_plans_only_on_blocking_violation():
    c = _contract("correct with Bonferroni.")
    results = verify_criteria_drift(c, "padj = p.adjust(p, method='BH')")
    plan = plan_recovery(results, c, attempt=1, max_attempts=3)
    assert plan.should_recover is True
    assert plan.message and "Scientific validation" in plan.message


def test_recovery_stops_at_max_attempts():
    c = _contract("correct with Bonferroni.")
    results = verify_criteria_drift(c, "padj = p.adjust(p, method='BH')")
    plan = plan_recovery(results, c, attempt=3, max_attempts=3)
    assert plan.should_recover is False


def test_recovery_does_not_produce_message_without_violation():
    c = _contract("correct with Bonferroni.")
    results = verify_criteria_drift(c, "padj = p.adjust(p, method='bonferroni')")
    plan = plan_recovery(results, c, attempt=1, max_attempts=3)
    assert plan.should_recover is False
    assert plan.message is None