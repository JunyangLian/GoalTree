"""A/B/C/D ablation runner on one public question (dev only).

Uses HostEnvironment (no Docker) + a real GLM model, and an independent ground-truth
comparison for correctness (NOT the official OmicsBench grader). Each config runs
the full DAG in topological order; every config gets a fresh answer directory.

Output is a JSON summary plus per-node submissions. All results are local-dev.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .config import group_flags
from .env import DockerEnvironment, HostEnvironment
from .adapter import OmicsBenchAdapter, ScientificStages
from .question import load_question


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _safe_host(base_url: str) -> str:
    return base_url.split("//")[-1].split("/")[0]


def _norm_items(items) -> frozenset:
    """Normalise a collection of item strings.

    A string or mapping must not be treated as a collection of items (iterating
    them would silently compare characters/keys and could fake a match); the
    callers turn TypeError into a "wrong" dev score.
    """
    if isinstance(items, (str, bytes, dict)) or not isinstance(items, (list, tuple, set, frozenset)):
        raise TypeError("items must be a sequence of item strings")
    return frozenset(str(i).strip().lower() for i in items)


def _match(entry, got) -> bool:
    gt = entry.ground_truth
    etype = entry.type
    if etype == "grouped_entity_set":
        def norm_group(items):
            return frozenset((str(g["group"]).strip().lower(), _norm_items(g["items"])) for g in items)
        try:
            return norm_group(got) == norm_group(gt)
        except (TypeError, KeyError, AttributeError):
            return False
    if etype == "multi_select_choice":
        try:
            return _norm_items(got) == _norm_items(gt)
        except (TypeError, AttributeError):
            return False
    if etype == "multiple_choice":
        return str(got).strip().lower() == str(gt).strip().lower()
    if etype == "numeric_tolerance":
        # Local approximation of the official tolerance grader: full credit only
        # inside ``full_credit_within`` (absolute). Partial credit in the ramp up
        # to ``no_credit_beyond`` is NOT modelled, so dev is a lower bound there.
        # Strings are rejected: "1105" is not a numeric answer (#47).
        if isinstance(got, bool) or not isinstance(got, (int, float)):
            return False
        params = entry.parameters or {}
        if params.get("match") != "absolute_tolerance":
            return float(got) == float(gt)
        within = params.get("full_credit_within")
        if isinstance(within, (int, float)) and not isinstance(within, bool):
            return abs(float(got) - float(gt)) <= float(within)
        return float(got) == float(gt)
    # Fallback: direct/case-insensitive equality for scalars.
    if isinstance(gt, str) and isinstance(got, str):
        return got.strip().lower() == gt.strip().lower()
    return got == gt


def dev_score_node(node, answer):
    """Independent dev correctness vs public ground truth. 1.0 == all graders match."""
    if answer is None:
        return 0.0, {"_": "no_answer"}
    total = len(node.grading.graders)
    correct = 0
    detail = {}
    for e in node.grading.graders:
        key = e.resolved_output_key()
        if key not in answer:
            detail[e.name] = "missing"
            continue
        ok = _match(e, answer[key])
        if ok:
            detail[e.name] = "correct"
        elif e.type == "multiple_choice":
            # Distinguish "picked a listed but wrong option" from "wrote a value
            # that was never on the menu" (seen in expR6); the latter is a prompt
            # -following failure, not a judgement call.
            choices = (e.parameters or {}).get("choices")
            offered = ({str(c).strip().lower() for c in choices}
                       if isinstance(choices, list) else set())
            got = str(answer[key]).strip().lower()
            detail[e.name] = "wrong_off_menu" if offered and got not in offered else "wrong"
        else:
            detail[e.name] = "wrong"
        if ok:
            correct += 1
    return (correct / total if total else 0.0), detail


def content_diagnostics(node, answer) -> dict[str, dict]:
    """Parameter-level evidence for answers a binary verdict cannot describe.

    This is **not** a score: it reports which expected records were found, how
    far their effect is from the expected value, and whether the direction
    matches. It exists because the local dev verdict for
    ``differential_feature_set`` is exact-match only (#46), so ``dev=0`` alone
    cannot tell "wrong answer" from "right answer, different formatting".
    """
    if not isinstance(answer, dict):
        return {}
    out: dict[str, dict] = {}
    for entry in node.grading.graders:
        if entry.type == "gene_set":
            key = entry.resolved_output_key()
            gt = entry.ground_truth
            got = answer.get(key)
            if not isinstance(gt, list) or not isinstance(got, list):
                continue
            gt_items = {str(item).strip().upper() for item in gt if isinstance(item, str)}
            got_items = {str(item).strip().upper() for item in got if isinstance(item, str)}
            out[entry.name] = {
                "expected": len(gt_items),
                "reported": len(got_items),
                "matched": len(gt_items & got_items),
                "missing": len(gt_items - got_items),
                "extra": len(got_items - gt_items),
            }
            continue
        if entry.type != "differential_feature_set":
            continue
        gt = entry.ground_truth
        got = answer.get(entry.resolved_output_key())
        if not isinstance(gt, list) or not isinstance(got, list):
            continue
        by_feature = {}
        for record in got:
            if isinstance(record, dict) and isinstance(record.get("feature"), str):
                by_feature[record["feature"].strip().lower()] = record
        expected = [r for r in gt
                    if isinstance(r, dict) and isinstance(r.get("feature"), str)]
        within = (entry.parameters or {}).get("effect_full_credit_within")
        within = (float(within)
                  if isinstance(within, (int, float)) and not isinstance(within, bool) else None)
        matched = effect_within = direction_match = 0
        deltas: list[float] = []
        for ref in expected:
            record = by_feature.get(ref["feature"].strip().lower())
            if record is None:
                continue
            matched += 1
            try:
                delta = abs(float(record.get("effect")) - float(ref.get("effect")))
            except (TypeError, ValueError):
                delta = None
            if delta is not None:
                deltas.append(delta)
                if within is not None and delta <= within:
                    effect_within += 1
            if (str(record.get("direction", "")).strip().lower()
                    == str(ref.get("direction", "")).strip().lower()):
                direction_match += 1
        out[entry.name] = {
            "expected": len(expected),
            "reported": len(got),
            "matched": matched,
            "effect_full_credit_within": within,
            "effect_within_full": effect_within if within is not None else None,
            "direction_match": direction_match,
            "max_effect_delta": round(max(deltas), 4) if deltas else None,
            "extra_features": max(0, len(got) - matched),
        }
    return out


def _make_usage_tracker(client):
    """Wrap the model client to accumulate token usage across every completion.

    The adapter reads ``client._usage_totals`` (a per-run cumulative dict) and
    records a per-node delta in each ``NodeSubmission.usage``. This is honest
    cost accounting: it only sums the ``last_completion_metadata`` the provider
    already reports; it never estimates or fabricates a token count.
    """
    totals = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "calls": 0}
    client._usage_totals = totals
    original = client.complete

    def tracked(*args, **kwargs):
        result = original(*args, **kwargs)
        metadata = getattr(client, "last_completion_metadata", {}) or {}
        for key in ("input_tokens", "output_tokens", "cached_tokens"):
            value = metadata.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                totals[key] += value
        totals["calls"] += 1
        return result

    client.complete = tracked


# A real padj-based selection for the bovine marker panel. It is executed by the
# harness as the agent's *first* action to create controlled drift evidence: the
# model never wrote it, so results are about the correction loop, not the model's
# own tendency. padj<0.05 keeps ITGB1, which the instruction-compliant raw-p
# test drops, so a drifted answer is also a wrong answer.
PADJ_SELECTION_SCRIPT = (
    "import pandas as pd, json; "
    "d=pd.read_csv('/data/gene_expression_profile.csv.gz'); "
    "m=['PDGFRA','DLK1','PPARG','ZNF423','CD34','PDGFRB','ITGB1']; "
    "s=d[d.Geneid.isin(m)]; sel=s[s.padj<0.05]; "
    "up=[g for g in m if g in set(sel[sel.log2FoldChange>0].Geneid)]; "
    "dn=[g for g in m if g in set(sel[sel.log2FoldChange<0].Geneid)]; "
    "json.dump({'marker_direction':[{'group':'higher in IMF','items':up},"
    "{'group':'higher in SF','items':dn}]}, "
    "open('/workspace/answers/marker_divergence.json','w')); "
    "print(up,dn)"
)
# The script must be run through `python -c "..."`; passing the bare source to
# the shell is a command-not-found error (and would still *look* like padj usage
# in the transcript, so the verifier must only count successful executions).
PADJ_SELECTION_COMMAND = 'python -c "%s"' % PADJ_SELECTION_SCRIPT

# Controlled probe for a *choice* node: the harness writes an answer that is
# schema-valid but not one of the offered options. Used to exercise the off-menu
# correction channel (#24). The value and target file are derived from the loaded
# question, so the probe follows whichever choice node actually runs; for
# `multi_select_choice` one offered option is kept and one outside item is added,
# which is the #41 situation (extra genes outside the candidate list).
OFFMENU_SENTINEL = "probe off-menu value"


def _string_choices(entry) -> list[str] | None:
    """The prompt-visible option list of a choice grader, or None."""
    choices = (entry.parameters or {}).get("choices")
    if (not isinstance(choices, list) or not choices
            or not all(isinstance(c, str) for c in choices)):
        return None
    return [str(c) for c in choices]


def pick_offmenu_target(question, node_ids=None):
    """First runnable node (topological order) that offers string choices.

    ``multi_select_choice`` is preferred because an injected extra item is the
    exact #41 failure. ``node_ids`` mirrors ``--nodes``: only the first driven
    node consumes ``inject_command``, so the probe must target a node that runs.
    """
    order = question.topological_order()
    if node_ids:
        wanted = set(node_ids)
        order = [nid for nid in order if nid in wanted]
    for prefer in ("multi_select_choice", "multiple_choice"):
        for node_id in order:
            node = question.node(node_id)
            for entry in node.grading.graders:
                if entry.type == prefer and _string_choices(entry):
                    return node, entry
    return None, None


def build_offmenu_probe_command(question, node_ids=None) -> tuple[str, str] | None:
    """``(node_id, shell command)`` that writes an off-menu answer, or None.

    The sentinel is guaranteed absent from the offered options, so only the
    delivery correction channel can remove it. Ground-truth values are never
    used here; for multi-select the kept option is ``choices[0]`` (author-provided
    and prompt-visible).
    """
    node, entry = pick_offmenu_target(question, node_ids)
    if node is None:
        return None
    choices = _string_choices(entry)
    sentinel = OFFMENU_SENTINEL
    while sentinel.strip().lower() in {c.strip().lower() for c in choices}:
        sentinel += " (alt)"
    value = [choices[0], sentinel] if entry.type == "multi_select_choice" else sentinel
    payload = json.dumps({entry.resolved_output_key(): value})
    script = (
        "import json; open('/workspace/answers/%s','w').write(%r); "
        "print('wrote off-menu probe')" % (node.grading.output_filename, payload)
    )
    # The script is already single-quoted for Python; escape the double quotes of
    # the embedded JSON so the surrounding `python -c "..."` stays intact.
    return node.id, 'python -c "%s"' % script.replace('"', '\\"')

# Deterministic probe for the nested-shape delivery channel (#45): the harness
# writes a schema-*valid* grouped answer whose `items` is a space-joined string.
# Official top-level validation accepts it; only the adapter's delivery check
# can notice. Used to exercise that path on demand instead of waiting for the
# model to produce it by accident.
MALFORMED_ITEMS_SCRIPT = (
    "import json; "
    "json.dump({'marker_direction':[{'group':'higher in IMF','items':'PDGFRA CD34'},"
    "{'group':'higher in SF','items':'PPARG ITGB1'}]}, "
    "open('/workspace/answers/marker_divergence.json','w')); "
    "print('wrote space-joined items')"
)
MALFORMED_ITEMS_COMMAND = 'python -c "%s"' % MALFORMED_ITEMS_SCRIPT

# The two probes above write a fixed bovine file. Keep the filenames so the CLI
# can refuse a sweep whose probe would never touch a driven answer (a silent
# no-op probe would look like "the channel works").
PADJ_TARGET_FILENAME = "marker_divergence.json"
MALFORMED_ITEMS_TARGET_FILENAME = "marker_divergence.json"

# Probe for the *record* shape of `differential_feature_set` (melanoma n2):
# official top-level validation only asks for a list, so a list of strings is
# "valid" while the grader needs one object per feature. The prompt already
# renders that object shape, so a missing nested check would be invisible.
MALFORMED_RECORDS_SENTINEL = "probe feature"


def pick_malformed_records_target(question, node_ids=None):
    """First runnable node with a ``differential_feature_set`` grader, or None."""
    order = question.topological_order()
    if node_ids:
        wanted = set(node_ids)
        order = [nid for nid in order if nid in wanted]
    for node_id in order:
        node = question.node(node_id)
        for entry in node.grading.graders:
            if entry.type == "differential_feature_set":
                return node, entry
    return None, None


def build_malformed_records_command(question, node_ids=None) -> tuple[str, str] | None:
    """``(node_id, command)`` writing feature records as bare strings, or None.

    The values are sentinels, never ground truth: the probe only asks whether the
    delivery layer notices that a record-shaped answer is a list of strings.
    """
    node, entry = pick_malformed_records_target(question, node_ids)
    if node is None:
        return None
    payload = json.dumps({entry.resolved_output_key():
                          [f"{MALFORMED_RECORDS_SENTINEL} A", f"{MALFORMED_RECORDS_SENTINEL} B"]})
    script = (
        "import json; open('/workspace/answers/%s','w').write(%r); "
        "print('wrote string records')" % (node.grading.output_filename, payload)
    )
    return node.id, 'python -c "%s"' % script.replace('"', '\\"')


def driven_output_filenames(question, node_ids=None) -> dict[str, str]:
    """``output_filename -> node_id`` for the nodes this sweep will drive."""
    order = question.topological_order()
    if node_ids:
        wanted = set(node_ids)
        order = [nid for nid in order if nid in wanted]
    return {question.node(nid).grading.output_filename: nid for nid in order}


# Probe for the *leaf type* of `numeric_tolerance`: a number written as a string
# still passes the official top-level scalar check, so only a type check can
# notice (melanoma n1: "1105" instead of 1105). Values are sentinels, never
# ground truth.
SCALAR_STRING_SENTINEL = "probe number"


def pick_scalar_strings_target(question, node_ids=None):
    """First runnable node whose numeric graders can be written as strings."""
    order = question.topological_order()
    if node_ids:
        wanted = set(node_ids)
        order = [nid for nid in order if nid in wanted]
    for node_id in order:
        node = question.node(node_id)
        entries = [e for e in node.grading.graders
                   if e.type == "numeric_tolerance" and not isinstance(e.ground_truth, bool)]
        if entries:
            return node, entries
    return None, None


def build_scalar_strings_command(question, node_ids=None) -> tuple[str, str] | None:
    """``(node_id, command)`` writing every numeric answer as a JSON string."""
    node, entries = pick_scalar_strings_target(question, node_ids)
    if node is None:
        return None
    payload = json.dumps({e.resolved_output_key(): f"{SCALAR_STRING_SENTINEL} {i + 1}"
                          for i, e in enumerate(entries)})
    script = (
        "import json; open('/workspace/answers/%s','w').write(%r); "
        "print('wrote string numbers')" % (node.grading.output_filename, payload)
    )
    return node.id, 'python -c "%s"' % script.replace('"', '\\"')



def _sum_usage(usages: list) -> dict[str, int] | None:
    """Sum per-node usage dicts into a per-run total; None when none measured."""
    if not usages or all(u is None for u in usages):
        return None
    keys = ("input_tokens", "output_tokens", "cached_tokens", "calls")
    total = {key: 0 for key in keys}
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        for key in keys:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total[key] += int(value)
    return total


def _write_report(out, report) -> None:
    """Persist the report; called after every run and group so a kill keeps work.

    Writes to a temp file then ``os.replace`` so a crash mid-write cannot leave
    a truncated report.json.
    """
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "report.json.tmp"
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    os.replace(tmp, out / "report.json")


def _agent_factory(args, data_root, work_root):
    from pico.providers.clients import OpenAICompatibleModelClient

    model = args.model
    base_url = args.base_url
    key = args.api_key
    temperature = args.temperature
    reasoning = args.reasoning

    def factory():
        client = OpenAICompatibleModelClient(model, base_url, key, temperature, timeout=300,
                                             api_format="chat_completions")
        _make_usage_tracker(client)
        if reasoning:
            client.request_options = {"reasoning_effort": reasoning}
        from pico import EnvironmentProfile, Pico, SessionStore, WorkspaceContext
        ws = Path(work_root) / ".pico"
        ws.mkdir(parents=True, exist_ok=True)
        profile = EnvironmentProfile(
            name="omicsbench",
            role="You are a computational biologist operating in a computational environment.",
            instructions=(
                "Write graded answers to /workspace/answers/ as strict JSON.",
                "Python 3 is available with pandas, scipy and numpy. Data files under /data/ are "
                "readable directly by Python (e.g. pandas.read_csv reads .csv.gz). Prefer Python "
                "for inspecting and analyzing files; common Linux shell utilities (zcat, ls, sed, "
                "mkdir -p, cat heredoc) may be unavailable on this host.",
            ),
        )
        return Pico(client, WorkspaceContext.build(Path(work_root)),
                    SessionStore(ws / "sessions"), environment=profile, wire_protocol="text",
                    approval_policy="auto", max_depth=0, max_steps=args.max_steps,
                    max_new_tokens=args.max_new_tokens,
                    # The A/B/C/D ablation owns the science stages; the core gate
                    # would silently add verification+recovery to the A baseline.
                    scientific_verification=False)
    return factory


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run A/B/C/D ablation on a public question (dev).")
    ap.add_argument("--question", required=True)
    ap.add_argument("--data-root", default="D:/1Bio_Soft/omicsbench/data")
    ap.add_argument("--work-root", default="D:/1Bio_Soft/omicsbench/exp")
    ap.add_argument("--out", default="D:/1Bio_Soft/omicsbench/exp/report")
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--model", default="glm-5.3-flash")
    ap.add_argument("--base-url", default="https://open.bigmodel.cn/api/paas/v4")
    ap.add_argument("--temperature", type=float, default=0.2)
    ap.add_argument("--reasoning", default="low")
    ap.add_argument("--wall-timeout", type=float, default=200.0)
    ap.add_argument("--recovery-attempts", type=int, default=3,
                    help="targeted re-prompt turns per failing node (default 3: expR34/expD6 "
                         "showed one attempt can be consumed by delivery+content in sequence)")
    ap.add_argument("--wrapup-after-tool-calls", type=int, default=None,
                    help="inject a commit-answer nudge after this many tool calls")
    ap.add_argument("--inject-padj-after-attempt", action="store_true",
                    help="controlled drift probe: after the agent's own attempt, run a "
                         "padj-based selection that overwrites the answer file, then let "
                         "the configured stages react (A keeps it, C/D must correct it)")
    ap.add_argument("--inject-offmenu-choice", action="store_true",
                    help="controlled drift probe for a multiple-choice node: after the "
                         "agent's own attempt, write an answer outside the offered options "
                         "and let the stages react (tests the off-menu correction channel)")
    ap.add_argument("--inject-malformed-items", action="store_true",
                    help="controlled probe: overwrite the grouped answer with `items` as a "
                         "space-joined string (schema-valid for the official loop) to exercise "
                         "the nested-shape delivery correction")
    ap.add_argument("--inject-malformed-records", action="store_true",
                    help="controlled probe: overwrite a differential_feature_set answer with a "
                         "list of bare strings (official top-level shape is still `list`) to "
                         "exercise the record-shape delivery correction")
    ap.add_argument("--inject-scalar-strings", action="store_true",
                    help="controlled probe: overwrite numeric_tolerance answers with JSON "
                         "strings (official top-level scalar check still passes) to exercise "
                         "the numeric leaf-type correction")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--max-new-tokens", type=int, default=4096)
    ap.add_argument("--k", type=int, default=3, help="repeat runs per config")
    ap.add_argument("--groups", default="A,B,C,D")
    ap.add_argument("--nodes", default="", help="comma-separated node IDs to run (default: all)")
    ap.add_argument("--env", choices=("host", "docker"), default="host",
                    help="run_shell backend: host shim (default) or a Linux container")
    ap.add_argument("--image", default="omicsbench-runtime:dev",
                    help="container image for --env docker")
    ap.add_argument("--no-network", action="store_true",
                    help="run containers with --network none (analysis runs offline)")
    args = ap.parse_args(argv)

    _load_dotenv(Path(args.env_file))
    args.api_key = None
    if "bigmodel" in _safe_host(args.base_url):
        args.api_key = os.environ.get("BIGMODEL_API_KEY", "").strip()
    else:
        args.api_key = (os.environ.get("PICO_OPENAI_API_KEY") or os.environ.get("PICO_DEEPSEEK_API_KEY") or "").strip()
    if not args.api_key:
        print(json.dumps({"error": "no api key"})); return 2

    question = load_question(args.question)
    requested_nodes = [n.strip() for n in args.nodes.split(",") if n.strip()] or None
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)

    # One controlled probe at a time, resolved against the question the sweep
    # actually drives. A probe that writes a file no driven node grades is a
    # silent no-op and would be misread as "the channel reacted".
    probe_command, probe_name, probe_filename = None, None, None
    if args.inject_padj_after_attempt:
        probe_command, probe_name, probe_filename = (
            PADJ_SELECTION_COMMAND, "padj", PADJ_TARGET_FILENAME)
    elif args.inject_offmenu_choice:
        target = build_offmenu_probe_command(question, requested_nodes)
        if target is None:
            print(json.dumps({"error": "no multiple_choice/multi_select_choice node to probe"}))
            return 2
        probe_node, probe_command = target
        probe_name = "offmenu_choice"
        probe_filename = question.node(probe_node).grading.output_filename
    elif args.inject_malformed_items:
        probe_command, probe_name, probe_filename = (
            MALFORMED_ITEMS_COMMAND, "malformed_items", MALFORMED_ITEMS_TARGET_FILENAME)
    elif args.inject_malformed_records:
        target = build_malformed_records_command(question, requested_nodes)
        if target is None:
            print(json.dumps({"error": "no differential_feature_set node to probe"}))
            return 2
        probe_node, probe_command = target
        probe_name = "malformed_records"
        probe_filename = question.node(probe_node).grading.output_filename
    elif args.inject_scalar_strings:
        target = build_scalar_strings_command(question, requested_nodes)
        if target is None:
            print(json.dumps({"error": "no numeric_tolerance node to probe"}))
            return 2
        probe_node, probe_command = target
        probe_name = "scalar_strings"
        probe_filename = question.node(probe_node).grading.output_filename

    driven = driven_output_filenames(question, requested_nodes)
    if probe_command is not None and probe_filename not in driven:
        print(json.dumps({"error": f"probe writes {probe_filename}, which no driven node "
                                   f"grades; driven files: {sorted(driven)}"}))
        return 2
    probe_node = driven.get(probe_filename) if probe_filename else None

    report = {"question_id": question.id, "model": args.model, "base_host": _safe_host(args.base_url),
              "temperature": args.temperature, "reasoning_effort": args.reasoning,
              "wall_timeout": args.wall_timeout, "wall_timeout_scope": "per_node",
              "recovery_attempts": args.recovery_attempts,
              "injected_probe": probe_name,
              "injected_probe_node": probe_node,
              "execution_env": args.env,
              "image": args.image if args.env == "docker" else None,
              "configs": {}}

    for group_name in [g.strip() for g in args.groups.split(",") if g.strip()]:
        flags = group_flags(group_name)
        stages = ScientificStages(
            constraints=bool(flags["constraints"]),
            verification=bool(flags["verification"]),
            recovery=bool(flags["recovery"]),
        )

        runs = []
        node_scores: dict[str, list[float]] = {}
        walls: list[float] = []
        for run_idx in range(args.k):
            work_root = Path(args.work_root) / group_name / f"run{run_idx}"
            for sub in ("answers", "scratch"):
                (work_root / sub).mkdir(parents=True, exist_ok=True)
            for f in (work_root / "answers").glob("*"):
                f.unlink()

            if args.env == "docker":
                env = DockerEnvironment(args.data_root, work_root, image=args.image,
                                        network=not args.no_network)
            else:
                env = HostEnvironment(args.data_root, work_root)
            adapter = OmicsBenchAdapter(question, env)
            t0 = time.perf_counter()
            try:
                subs = adapter.run_question(_agent_factory(args, args.data_root, work_root),
                                            scientific=stages, wall_timeout=args.wall_timeout,
                                            recovery_attempts=args.recovery_attempts,
                                            wrapup_after_tool_calls=args.wrapup_after_tool_calls,
                                            nodes=requested_nodes,
                                            inject_command=probe_command)
                run_error = None
            except Exception as exc:
                # Adapter already isolates per-node failures; this catches setup
                # failures (client construction, environment init) so one broken
                # run cannot discard the whole unattended sweep's report.
                subs = []
                run_error = f"{type(exc).__name__}: {exc}"
            wall = round(time.perf_counter() - t0, 2)
            walls.append(wall)

            nodes = []
            for s in subs:
                node = question.node(s.node_id)
                sc, detail = dev_score_node(node, s.answer)
                node_scores.setdefault(s.node_id, []).append(sc)
                nodes.append({
                    "node_id": s.node_id,
                    "outcome": s.outcome,
                    "dev_correct": sc,
                    "detail": detail,
                    "content_diagnostics": content_diagnostics(node, s.answer),
                    "tool_calls": len(s.tool_calls),
                    "final_answer_len": len(s.final_answer or ""),
                    "verification": [r.to_dict() if hasattr(r, "to_dict") else vars(r) for r in s.verification],
                    "usage": s.usage,
                    "error": s.error,
                    "timed_out": s.timed_out,
                    "answer": s.answer,
                })
            usage_total = _sum_usage([n["usage"] for n in nodes])
            if nodes and all(n.get("error") for n in nodes) and not run_error:
                # Every node died on the same transport/setup error (expD24/25: HTTP
                # 429 balance exhausted). Without this the run looks like a clean
                # 0.0 in the summary and ``failed_runs`` stays 0.
                run_error = "all nodes failed: " + str(nodes[0]["error"])[:180]
            runs.append({"run_index": run_idx, "wall_seconds": wall,
                         "usage_total": usage_total, "error": run_error, "nodes": nodes})
            print(f"[{group_name} run{run_idx}] wall={wall}s usage={usage_total} nodes="
                  f"{[(n['node_id'], n['outcome'], n['dev_correct']) for n in nodes]}"
                  f"{' node_errors=' + str(sum(1 for n in nodes if n.get('error'))) if any(n.get('error') for n in nodes) else ''}"
                  f"{' error=' + run_error if run_error else ''}", flush=True)
            # Persist after every run so a kill mid-sweep keeps completed runs
            # (marked partial until the group's summary exists).
            report["configs"][group_name] = {
                "flags": flags, "k": args.k, "partial": True, "summary": None, "runs": runs,
            }
            _write_report(out, report)

        failed_runs = sum(1 for r in runs
                          if r.get("error") or not r.get("nodes")
                          or all(n.get("error") for n in r["nodes"]))
        summary = {
            "node_correct_mean": {nid: round(sum(v) / len(v), 3) for nid, v in node_scores.items()},
            "task_correct_mean": round(
                sum(sum(v) for v in node_scores.values()) / (len(node_scores) * args.k), 3
            ) if node_scores and args.k else 0.0,
            "mean_wall_seconds": round(sum(walls) / len(walls), 2) if walls else 0.0,
            # A run that produced no node results is scored as zero above; expose
            # the count so an infrastructure failure is visible, not silently mixed
            # into the model's "correctness".
            "runs": args.k,
            "failed_runs": failed_runs,
        }
        report["configs"][group_name] = {
            "flags": flags, "k": args.k, "partial": False, "summary": summary, "runs": runs,
        }
        print(f"[{group_name}] SUMMARY {summary}", flush=True)
        _write_report(out, report)

    _write_report(out, report)
    print("WROTE", str(out / "report.json"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())