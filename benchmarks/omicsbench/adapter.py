"""Thin adapter: drives GoalTree through an official OmicsBench question.

This mirrors the official runner's DAG traversal and per-round prompt contract,
delegating tool execution to an ``Environment`` and collecting the answer file.
It does *not* score answers: the official grader runs outside the agent path.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .env import Environment
from .prompt_renderer import derive_output_schema, render_first_node_prompt, render_node_prompt
from .question import GraderEntry, Node, Question


# Mirrors the official `_GRADER_ANSWER_SHAPE` primitive categories (dict/list/
# scalar) inferred from the serialized ground truth shape, which is what the
# official schema deriver also keys on. This classifies submission *shape* only;
# it never produces a score.
def _expected_category(entry: GraderEntry) -> str:
    gt = entry.ground_truth
    if isinstance(gt, dict):
        return "dict"
    if isinstance(gt, list):
        return "list"
    return "scalar"


def _matches_category(value: Any, category: str) -> bool:
    if value is None:
        return False
    if category == "dict":
        return isinstance(value, dict)
    if category == "list":
        return isinstance(value, list)
    return not isinstance(value, (dict, list))


def _reject_non_finite(token: str) -> float:
    """Reject NaN/Infinity tokens, mirroring the official answer parser.

    ``json.loads`` accepts these tokens by default, while the official grading
    loop raises ``InvalidJSONError`` for them (its serializer is strict). A
    parser that is looser than the grader would mark an answer "valid" and skip
    recovery for a file the official run cannot grade.
    """
    raise ValueError(f"answer contains the non-finite JSON token {token!r}")


def _parse_answer(text: str) -> tuple[Any, list[str]]:
    """Parse like the official loop: strict constants + top-level duplicate keys.

    Returns ``(parsed, duplicate_top_level_keys)``. Duplicates keep the last
    value (matching ``json.loads``) but are reported because the official grader
    treats them as a schema mismatch.
    """
    duplicates: list[str] = []

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        nonlocal duplicates
        seen: dict[str, Any] = {}
        dups: list[str] = []
        for key, value in pairs:
            if key in seen and key not in dups:
                dups.append(key)
            seen[key] = value
        duplicates = dups
        return seen

    parsed = json.loads(text, object_pairs_hook=hook, parse_constant=_reject_non_finite)
    return parsed, duplicates if isinstance(parsed, dict) else []


@dataclass
class NodeSubmission:
    """One node's collected result. ``outcome`` uses the official attempt
    vocabulary plus ``valid`` (shape-correct but not yet scored)."""

    node_id: str
    outcome: str  # file_missing | invalid_json | schema_mismatch | valid | timeout | error
    answer: Any = None
    answer_text: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    final_answer: str | None = None
    contract: Any = None
    verification: list[Any] = field(default_factory=list)
    usage: dict[str, int] | None = None  # per-node token delta, see _usage_delta
    error: str | None = None  # transport/setup exception that ended the node, if any
    # The node consumed its whole wall budget. Kept separate from ``outcome``:
    # a valid file written before the clock ran out is still gradable (official
    # semantics), but the report must not present it as a completed correction
    # (expD9 D run1: 370.6s budget overrun left an off-menu answer "valid").
    timed_out: bool = False


def classify_answer(node: Node, answer_text: str | None) -> tuple[str, Any]:
    """Classify the answer-file shape the way the official loop validates it.

    Returns ``(outcome, parsed)``. ``valid`` means the file would reach the
    official grader; it is not a score.
    """
    if answer_text is None:
        return "file_missing", None
    try:
        parsed, duplicates = _parse_answer(answer_text)
    except (json.JSONDecodeError, ValueError):
        return "invalid_json", None
    if not isinstance(parsed, dict):
        return "schema_mismatch", parsed
    expected = {entry.resolved_output_key() for entry in node.grading.graders}
    if duplicates or set(parsed) != expected:
        return "schema_mismatch", parsed
    for entry in node.grading.graders:
        value = parsed[entry.resolved_output_key()]
        if not _matches_category(value, _expected_category(entry)):
            return "schema_mismatch", parsed
    return "valid", parsed


def _choice_violation(node: Node, parsed: Any) -> str | None:
    """Return a correction message when a choice answer is not an offered option.

    The question JSON lists the allowed ``choices`` and the prompt tells the model
    to answer with one of them. This does not change ``valid`` (official shape)
    semantics; it only adds a targeted rewrite on top of it.

    Also covers ``multi_select_choice``: expR30 showed a retried answer that added
    genes outside the offered candidate list, which the grader cannot credit.
    """
    if not isinstance(parsed, dict):
        return None
    for entry in node.grading.graders:
        if entry.type not in {"multiple_choice", "multi_select_choice"}:
            continue
        choices = (entry.parameters or {}).get("choices")
        if (not isinstance(choices, list) or not choices
                or not all(isinstance(c, str) for c in choices)):
            continue  # only the plain string-choice form is validated here
        key = entry.resolved_output_key()
        value = parsed.get(key)
        offered = [str(c) for c in choices]
        allowed = {c.strip().lower() for c in offered}
        if entry.type == "multi_select_choice":
            if (not isinstance(value, list) or not value
                    or not all(isinstance(v, str) for v in value)):
                continue  # shape problems are handled by classify_answer
            outside = [v for v in value if v.strip().lower() not in allowed]
            if not outside:
                continue
            return (
                f"Validation: \"{key}\" includes {outside}, which are not among the offered "
                f"candidates {offered}. Select only from the offered candidates (do not add "
                f"genes of your own) and rewrite /workspace/answers/"
                f"{node.grading.output_filename} with write_file, then reply <final>done</final>."
            )
        if not isinstance(value, str):
            continue  # shape problems are handled by classify_answer
        if value.strip().lower() in allowed:
            continue
        return (
            f"Validation: the value you wrote for \"{key}\" is {value!r}, which is not one of the "
            f"offered choices {offered}. Choose exactly one of the offered options and rewrite "
            f"/workspace/answers/{node.grading.output_filename} with write_file, then reply "
            f"<final>done</final>."
        )
    return None


def _grouped_items_violation(node: Node, parsed: Any) -> str | None:
    """A grouped answer must carry ``items`` as an array of strings.

    Official validation only checks the top-level slice category, so a
    space-joined string reaches the grader and scores zero (expD4). ``valid``
    stays as-is (official parity); this only adds one targeted rewrite.
    """
    if not isinstance(parsed, dict):
        return None
    for entry in node.grading.graders:
        if entry.type != "grouped_entity_set":
            continue  # ranked-list shapes may legitimately use other item forms
        key = entry.resolved_output_key()
        groups = parsed.get(key)
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            items = group.get("items")
            if items is None:
                continue
            if isinstance(items, list) and all(isinstance(i, str) for i in items):
                continue
            shown = repr(items) if isinstance(items, str) else type(items).__name__
            return (
                f"Validation: \"{key}\" has \"items\" = {shown} for group "
                f"{group.get('group')!r}, but each group's \"items\" must be a JSON array of "
                f"item strings, one entry per element (e.g. [\"GENE1\", \"GENE2\"]). Split the "
                f"items into a list and rewrite /workspace/answers/"
                f"{node.grading.output_filename} with write_file, then reply <final>done</final>."
            )
    return None


def _feature_record_violation(node: Node, parsed: Any) -> str | None:
    """A ``differential_feature_set`` answer must be a list of record objects.

    Official top-level validation only asks for a list, so ``["SOX10", "MITF"]``
    reaches the grader and scores zero (expD11). ``valid`` keeps official
    semantics; this adds one targeted rewrite. Required fields come from the
    author-visible answer schema (the first ground-truth record's keys), never
    from its values.
    """
    if not isinstance(parsed, dict):
        return None
    for entry in node.grading.graders:
        if entry.type != "differential_feature_set":
            continue
        key = entry.resolved_output_key()
        records = parsed.get(key)
        if not isinstance(records, list):
            continue  # top-level shape problems belong to classify_answer
        required: list[str] = []
        numeric_fields: list[str] = []
        gt = entry.ground_truth
        if isinstance(gt, list) and gt and isinstance(gt[0], dict):
            required = list(gt[0].keys())
            # Field *types* are author-visible in the answer schema; values are not.
            numeric_fields = [field for field, value in gt[0].items()
                              if isinstance(value, (int, float)) and not isinstance(value, bool)]
        for index, record in enumerate(records):
            if isinstance(record, dict):
                missing = [field for field in required if field not in record]
                if missing:
                    problem = f"element {index} is missing {missing}"
                else:
                    quoted = [field for field in numeric_fields
                              if field in record and isinstance(record[field], str)]
                    if not quoted:
                        continue
                    problem = (f"element {index} has {quoted} as strings instead of JSON "
                               f"numbers")
            elif isinstance(record, str):
                problem = f"element {index} is the bare string {record!r}"
            else:
                problem = f"element {index} is a {type(record).__name__}"
            fields = ", ".join(required) if required else "the fields shown in the answer schema"
            return (
                f"Validation: \"{key}\" must be a JSON array of record objects, one per feature, "
                f"but {problem}. Each record must carry {fields}. Write the corrected answer to "
                f"/workspace/answers/{node.grading.output_filename} with write_file, then reply "
                f"<final>done</final>."
            )
    return None


def _numeric_type_violation(node: Node, parsed: Any) -> str | None:
    """A ``numeric_tolerance`` answer must be a JSON number, not a numeric string.

    Official top-level validation classifies every non-container value as a
    scalar, so ``"1105"`` reaches the grader and scores zero (expD13). ``valid``
    keeps official semantics; this only adds one targeted rewrite.
    """
    if not isinstance(parsed, dict):
        return None
    for entry in node.grading.graders:
        if entry.type != "numeric_tolerance":
            continue
        key = entry.resolved_output_key()
        value = parsed.get(key)
        if not isinstance(value, str):
            continue  # non-numeric shapes are handled by classify_answer
        return (
            f"Validation: \"{key}\" must be a JSON number (for example 1105), but you wrote the "
            f"string {value!r}. Write the numeric value without quotes and rewrite "
            f"/workspace/answers/{node.grading.output_filename} with write_file, then reply "
            f"<final>done</final>."
        )
    return None


def _json_object_in(text: str | None) -> dict | None:
    """First JSON object embedded in a free-form reply, or None.

    Used only to hand the model's *own* answer back when it described it in text
    but never wrote the graded file. Never reads an answer key.
    """
    if not text:
        return None
    for start in [i for i, ch in enumerate(text) if ch == "{"][:8]:
        depth = 0
        for end in range(start, len(text)):
            ch = text[end]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:end + 1])
                    except (json.JSONDecodeError, ValueError):
                        break
                    if isinstance(value, dict):
                        return value
                    break
    return None


def _json_error(text: str | None) -> str | None:
    """The parser's message for a malformed answer, or None when it parses."""
    if text is None:
        return None
    try:
        _parse_answer(text)
    except (json.JSONDecodeError, ValueError) as exc:
        return f"{type(exc).__name__}: {exc}"
    return None


def _usage_snapshot(agent) -> dict[str, int]:
    """Read the model client's optional cumulative token counters.

    The experiment harness instruments ``client.complete`` to accumulate
    ``_usage_totals``; when that hook is absent (e.g. offline FakeModelClient),
    this returns an empty dict and the adapter records no usage (``None``).
    """
    client = getattr(agent, "model_client", None)
    totals = getattr(client, "_usage_totals", None)
    if not isinstance(totals, dict):
        return {}
    out: dict[str, int] = {}
    for key in ("input_tokens", "output_tokens", "cached_tokens", "calls"):
        value = totals.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = int(value)
    return out


def _usage_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int] | None:
    """Per-node token delta from two cumulative snapshots; None when unmeasured."""
    if not before and not after:
        return None
    keys = ("input_tokens", "output_tokens", "cached_tokens", "calls")
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in keys}


def _deadline_passed(deadline: float | None) -> bool:
    """Whether the node's wall budget is exhausted (no deadline => never)."""
    return deadline is not None and time.monotonic() >= deadline


@dataclass(frozen=True)
class ScientificStages:
    """Independent scientific-pipeline switches for the A/B/C/D ablation.

    - constraints: compile the P1 contract (record only).
    - verification: run the P2 evidence verifier (record only).
    - recovery: on a blocking VIOLATION, inject the targeted correction (P3).
    """

    constraints: bool = False
    verification: bool = False
    recovery: bool = False


# Share of the wall budget kept for recovery. Observed on node1: the first
# attempt explored until the clock ran out, so every recovery (the step that
# actually writes the answer) started with ~0s and timed out immediately.
_RECOVERY_RESERVE_RATIO = 0.3


class OmicsBenchAdapter:
    """Drives one question. ``scientific`` (optional) injects the GoalTree
    science-constraint pipeline without changing the baseline path."""

    def __init__(self, question: Question, environment: Environment, scientific=None):
        self.question = question
        self.environment = environment
        self.scientific = scientific

    def _execute(self, action: dict[str, Any], node: Node,
                 deadline: float | None = None, drift_probe: bool = False) -> dict[str, Any]:
        name = action.get("name")
        args = action.get("args") or {}
        if name == "run_shell" and deadline is not None and isinstance(args, dict):
            # A model-supplied command timeout (e.g. 600s) would otherwise blow
            # far past the node's wall budget: the deadline check can only stop
            # *between* actions, not mid-command. Clamp it to the time left.
            remaining = max(1, int(deadline - time.monotonic()))
            requested = args.get("timeout")
            if (not isinstance(requested, (int, float)) or isinstance(requested, bool)
                    or requested > remaining):
                args = {**args, "timeout": remaining}
        try:
            content = self.environment.execute(name, args)
            status = "ok" if getattr(self.environment, "last_status", "ok") == "ok" else "error"
            result = {"content": content, "metadata": {"tool_status": status}}
        except Exception as exc:  # keep Observation honest; never fake success
            status = "error"
            result = {"content": f"error: {type(exc).__name__}: {exc}",
                      "metadata": {"tool_status": "error"}}
        # Tag the environment's own log entry: failed commands must not become
        # scientific evidence (a typo'd `padj < 0.05` is not method drift).
        calls = getattr(self.environment, "tool_calls", None)
        if isinstance(calls, list) and calls and isinstance(calls[-1], dict):
            calls[-1].setdefault("status", status)
            if drift_probe:
                # The harness, not the model, produced this command. Keep the tag so
                # evidence reconstruction can drop it once the graded artifact has
                # been rewritten by the model (otherwise the probe would keep
                # reporting drift forever and the correction could never clear).
                calls[-1]["drift_probe"] = True
        return result

    def _answer_is_valid(self, node: Node) -> bool:
        text = self.environment.read_answer(node.grading.output_filename)
        outcome, _ = classify_answer(node, text)
        return outcome == "valid"

    def _answer_is_fresh(self, node: Node, baseline: str | None) -> bool:
        """Valid answer that differs from ``baseline`` (or any valid one if None)."""
        text = self.environment.read_answer(node.grading.output_filename)
        outcome, _ = classify_answer(node, text)
        if outcome != "valid":
            return False
        return baseline is None or text != baseline

    def _wrapup_prompt(self, node: Node) -> str:
        return (
            "You have inspected the data enough. Stop exploring and write your final answer now: "
            "call write_file to write the strict JSON object to "
            f'"/workspace/answers/{node.grading.output_filename}" matching the requested schema, '
            "then reply <final>done</final>."
        )

    def _step_loop(self, agent, prompt_text: str, node: Node, *, max_rounds: int,
                   deadline: float | None, wrapup_threshold: int | None = None,
                   require_new_answer: bool = False):
        """Run up to ``max_rounds`` agent steps for this node.

        Returns ``(run, timed_out, error, final_text)``. Provider/setup exceptions
        are caught here — not propagated — so one failing node cannot abort an
        unattended multi-config sweep. ``error`` is the exception summary, or None.
        ``timed_out`` means the wall budget is exhausted; a transient model error
        before the deadline reports ``timed_out=False`` so the node can retry.
        ``final_text`` is the text of the model's own final reply (used to hand a
        described-but-unwritten answer back to it).

        ``require_new_answer`` is for correction turns that start from an already
        schema-valid file: a stale valid answer must not end the turn after the
        model's first action (usually the recompute), or the correction can never
        replace it. Completion then means "the model rewrote the graded file".
        """
        baseline = (self.environment.read_answer(node.grading.output_filename)
                    if require_new_answer else None)
        final_text = ""
        try:
            run = agent.start_run(prompt_text, execute_actions=False)
        except Exception as exc:  # setup/transport error before the first step
            return None, _deadline_passed(deadline), f"{type(exc).__name__}: {exc}", final_text
        awaiting = False
        observation = None
        action_count = 0
        wrapup_sent = False
        for _ in range(max_rounds):
            if deadline is not None and time.monotonic() >= deadline:
                run.close()
                return run, True, None, final_text
            try:
                step = run.step(observation) if awaiting else run.step()
            except Exception as exc:  # LLM transport/timeout mid-step: stop this node cleanly
                run.close()
                return run, _deadline_passed(deadline), f"{type(exc).__name__}: {exc}", final_text
            awaiting = False
            if step.kind == "action":
                observations = []
                for action in step.actions:
                    # A single model step can outlast the node's wall deadline
                    # (the client timeout is independent). Check before each
                    # action so a late batch cannot start a long command past
                    # the budget; overshoot is bounded to one in-flight action.
                    if deadline is not None and time.monotonic() >= deadline:
                        run.close()
                        return run, True, None, final_text
                    action_count += 1
                    observations.append(self._execute(action, node, deadline=deadline))
                    # Check after *each* action: a later action in the same step
                    # must not run (and overwrite) once a schema-valid answer
                    # exists. The graded artifact is the file, not <final> text.
                    if self._answer_is_fresh(node, baseline):
                        run.close()
                        return run, False, None, final_text
                # Wrap-up nudge: if the model keeps shelling without writing the
                # answer, prompt it to commit before the wall clock runs out.
                if (wrapup_threshold is not None and action_count >= wrapup_threshold
                        and not wrapup_sent):
                    wrapup_sent = True
                    run.inject_event({"type": "user_message", "content": self._wrapup_prompt(node)})
                observation = observations[0] if len(observations) == 1 else observations
                awaiting = True
            elif step.kind in ("observation", "retry"):
                continue
            else:
                final_text = (step.answer or step.message or "").strip() or final_text
                break
        # Reaching the round budget without a valid answer must still release the
        # run (generator + task state); otherwise the next node starts a new run
        # while the old one is still marked "running".
        run.close()
        return run, False, None, final_text

    def _observed_execution_text(self, calls: list[dict[str, Any]] | None = None) -> str:
        """Reconstruct the commands/signals the agent executed for the current node.

        ``calls`` is the per-node slice of the environment's tool-call log; when
        omitted it falls back to the full log (single-node tests/back-compat).
        """
        if calls is None:
            calls = getattr(self.environment, "tool_calls", [])
        lines: list[str] = []
        scripts: dict[str, str] = {}
        executed: set[str] = set()
        for call in calls:
            # A custom environment may log a malformed entry; a reconstruction
            # helper must never abort the node (and the already-written answer)
            # because of it.
            if not isinstance(call, dict):
                continue
            if call.get("status") == "error":
                # Failed executions (bad syntax, missing tool, timeout) are not
                # evidence of what the analysis did.
                continue
            name = str(call.get("name", ""))
            args = call.get("args")
            if not isinstance(args, dict):
                args = {}
            if name == "run_shell":
                command = str(args.get("command", ""))
                lines.append(command)
                # Script bodies count once the script is actually executed: the
                # analysis usually lives in a file, and the command alone
                # (`python x.py`) hides the statistical method it used.
                for path, source in scripts.items():
                    if path in executed:
                        continue
                    basename = path.replace("\\", "/").rsplit("/", 1)[-1]
                    if basename and basename in command:
                        executed.add(path)
                        lines.append(source)
            else:
                path = str(args.get("path", ""))
                lines.append(f"{name} {path}".strip())
                body = args.get("content")
                if isinstance(body, str) and path.lower().endswith((".py", ".sh", ".r")):
                    scripts[path] = body
        return "\n".join(lines)

    def _scientific_report(self, node: Node, prompt_text: str, answer: Any, stages: ScientificStages,
                           calls: list[dict[str, Any]] | None = None):
        """Compile constraints (P1) and run evidence verification (P2) per stages.

        ``verification`` is empty when ``stages.verification`` is off; the contract is
        compiled only when any scientific stage is on. ``calls`` scopes the observed
        execution to this node so earlier nodes in a shared session do not bleed in.
        """
        contract = None
        verification: list[Any] = []
        if stages.constraints or stages.verification or stages.recovery:
            from goaltree.scientific import compile_contract

            contract = compile_contract(node.id, prompt_text)
        if stages.verification or stages.recovery:
            from goaltree.scientific import run_verifier

            observed = self._observed_execution_text(calls)
            answer_text = self.environment.read_answer(node.grading.output_filename)
            verification = run_verifier(
                contract,
                observed_execution=observed,
                sample_usage=observed,  # executed commands are our only usage record
                answer=answer,
                answer_text=answer_text,
                provenance=observed,
            )
        return contract, verification

    def _scientific_correction(self, contract, results) -> str | None:
        if contract is None or not results:
            return None
        blockers = [r for r in results if getattr(r, "status", "") == "VIOLATION"
                    and getattr(r, "severity", "") == "blocking"]
        if not blockers:
            return None
        from goaltree.scientific import plan_recovery

        plan = plan_recovery(results, contract, attempt=0, max_attempts=1)
        return plan.message

    def _answer_template_note(self, node: Node, labels: list[str] | None = None,
                              include_schema: bool = True) -> str:
        """Compact reminder of the exact artifact a correction turn must write.

        The shape comes from the same renderer the task prompt uses (placeholders,
        never ground-truth items), and the labels are the ones the prompt already
        shows. expR27/expR28: corrections regenerated the answer with an invented
        filename, schema or labels and silently scored zero.
        """
        filename = node.grading.output_filename
        parts: list[str] = []
        if include_schema:
            parts.append(f'Write the corrected answer to "/workspace/answers/{filename}" with '
                         f"write_file, in exactly this shape:")
            parts.append(derive_output_schema(node.grading.graders))
        else:
            parts.append(f'Rewrite "/workspace/answers/{filename}" with write_file.')
        if labels:
            quoted = ", ".join(f"'{label}'" for label in labels)
            parts.append(f"Use exactly these group labels: {quoted}. "
                         f"Do not rename, translate, or invent labels.")
        parts.append("Then reply <final>done</final>.")
        return "\n".join(parts)

    def _recovery_prompt(self, node: Node, scientific_message: str | None = None,
                         outcome: str = "file_missing", parsed: Any = None,
                         json_error: str | None = None,
                         final_text: str | None = None) -> str:
        parts: list[str] = []
        if scientific_message:
            parts.append(scientific_message)
        filename = node.grading.output_filename
        keys = sorted(e.resolved_output_key() for e in node.grading.graders)
        if outcome == "schema_mismatch":
            # Surface the actual structure the model produced so it can see the
            # precise diff (wrong keys, wrong top-level type, or wrong value
            # shape) instead of guessing from a generic "rewrite" instruction.
            if isinstance(parsed, dict):
                actual = sorted(parsed.keys())
                if actual != keys:
                    parts.append(
                        f"Validation: /workspace/answers/{filename} has the wrong top-level keys. "
                        f"You wrote keys {actual}, but the required keys are exactly {keys}. "
                        f"Rewrite it with write_file (non-empty path and content) to "
                        f'"/workspace/answers/{filename}" using only the keys {keys}, then reply '
                        f"<final>done</final>."
                    )
                else:
                    # Show the expected *structure* (placeholders only, never the
                    # ground-truth values). A real run answered with a gene->label
                    # dict where a list of groups was required; the schema makes
                    # that shape explicit.
                    schema = derive_output_schema(node.grading.graders)
                    parts.append(
                        f"Validation: /workspace/answers/{filename} has the right top-level keys "
                        f"{keys}, but at least one value is the wrong type or shape, or a "
                        f"top-level key appears more than once. The required structure is:\n{schema}\n"
                        f"Check each value against it (and write each key exactly once), then rewrite "
                        f'it with write_file to "/workspace/answers/{filename}", then reply '
                        f"<final>done</final>."
                    )
            else:
                parts.append(
                    f"Validation: /workspace/answers/{filename} must be a strict JSON object, but "
                    f"you wrote {type(parsed).__name__ if parsed is not None else 'nothing usable'}. "
                    f"Rewrite it with write_file (non-empty path and content) to "
                    f'"/workspace/answers/{filename}" as an object with top-level keys exactly '
                    f"{keys}, then reply <final>done</final>."
                )
        elif outcome == "invalid_json":
            detail = f" Parser said: {json_error}." if json_error else ""
            parts.append(
                f"Validation: /workspace/answers/{filename} is not valid JSON.{detail} Rewrite it as "
                f"strict JSON with top-level keys {keys}, then reply <final>done</final>."
            )
        else:
            parts.append(
                f"Validation: the required answer file /workspace/answers/{filename} was not written. "
                "Call write_file with a non-empty 'path' and 'content': "
                f'path "/workspace/answers/{filename}", content the strict JSON object with keys {keys}. '
                "Then reply <final>done</final>."
            )
            # expR30/R31/R33: the model often ends a turn with the answer in its
            # reply text and no file. Handing its own JSON back removes the
            # ambiguity that made it redo (and sometimes widen) the analysis.
            described = _json_object_in(final_text)
            if isinstance(described, dict):
                body = json.dumps(described, ensure_ascii=False)
                if len(body) <= 800:
                    parts.append(
                        "Your last reply already contained this answer object as text:\n"
                        f"{body}\nWrite exactly this JSON to \"/workspace/answers/{filename}\" "
                        "with write_file — do not recompute, reformat, or add keys."
                    )
                else:
                    parts.append(
                        "Your last reply already contained an answer object as text. Write that "
                        f"same object to \"/workspace/answers/{filename}\" with write_file — do not "
                        "recompute it or change its values."
                    )
        return "\n\n".join(parts)

    @staticmethod
    def _stages(scientific) -> ScientificStages:
        if scientific is None:
            return ScientificStages()
        if isinstance(scientific, ScientificStages):
            return scientific
        # Backward compatibility: a truthy module means the full pipeline.
        return ScientificStages(constraints=True, verification=True, recovery=True)

    def drive_node(self, agent, prompt_text: str, node: Node, *,
                   max_rounds: int = 40, wall_timeout: float | None = None,
                   recovery_attempts: int = 1, scientific=None,
                   wrapup_after_tool_calls: int | None = None,
                   inject_command: str | None = None) -> NodeSubmission:
        stages = self._stages(scientific)
        deadline = time.monotonic() + wall_timeout if wall_timeout else None
        # Hold back part of the wall budget for recovery: the first attempt must
        # not consume the whole clock, or the step that writes the answer starts
        # with zero seconds left.
        reserve = (wall_timeout * _RECOVERY_RESERVE_RATIO
                   if wall_timeout and recovery_attempts > 0 else 0.0)
        first_deadline = deadline - reserve if deadline is not None else None
        usage_before = _usage_snapshot(agent)
        call_start = len(getattr(self.environment, "tool_calls", []))
        # A node's answer must be produced *during* this node. Clearing any stale
        # graded file prevents a leftover answer (from a prior run reusing the same
        # work_root) from being scored as this run's output.
        self.environment.clear_answer(node.grading.output_filename)
        run, _, error, final_text = self._step_loop(
            agent, prompt_text, node, max_rounds=max_rounds,
            deadline=first_deadline, wrapup_threshold=wrapup_after_tool_calls)
        probe_answer: str | None = None
        if inject_command:
            # Controlled drift probe: the harness itself performs one padj-based
            # selection and overwrites the graded file, so the configured stages
            # face genuinely drifted evidence and artifact. This is an injected
            # condition, not model behaviour.
            self._execute({"name": "run_shell",
                           "args": {"command": inject_command, "timeout": 60}}, node,
                          drift_probe=True)
            probe_answer = self.environment.read_answer(node.grading.output_filename)

        def evidence_calls() -> list[dict[str, Any]]:
            """This node's tool calls, minus the probe once its artifact is gone.

            While the graded file still holds the probe's answer, its command is
            the honest evidence of how that artifact was produced. After the model
            rewrites the answer, the probe no longer describes what is being
            reviewed; keeping it would report drift forever, so a correction could
            never clear.
            """
            calls = list(getattr(self.environment, "tool_calls", [])[call_start:])
            if probe_answer is not None and (
                    self.environment.read_answer(node.grading.output_filename) != probe_answer):
                calls = [c for c in calls
                         if not (isinstance(c, dict) and c.get("drift_probe"))]
            return calls
        # "timed out" means the node's *whole* budget is gone; exhausting only the
        # first-attempt phase still leaves room for recovery.
        timed_out = _deadline_passed(deadline)
        answer_text = self.environment.read_answer(node.grading.output_filename)
        outcome, parsed = classify_answer(node, answer_text)
        choice_message = _choice_violation(node, parsed)
        delivery_message = (_grouped_items_violation(node, parsed)
                            or _feature_record_violation(node, parsed)
                            or _numeric_type_violation(node, parsed))

        contract = None
        verification: list[Any] = []
        if stages.constraints or stages.verification or stages.recovery:
            contract, verification = self._scientific_report(
                node, prompt_text, parsed, stages, evidence_calls())

        attempts = 0
        while not timed_out and attempts < recovery_attempts:
            # Targeted correction is only computed when the recovery stage is on.
            correction = self._scientific_correction(contract, verification) if stages.recovery else None
            need_protocol = outcome != "valid"
            need_scientific = correction is not None
            need_choice = choice_message is not None
            need_delivery = delivery_message is not None
            if not need_protocol and not need_scientific and not need_choice and not need_delivery:
                break
            if deadline is not None and time.monotonic() >= deadline:
                timed_out = True
                break
            attempts += 1
            labels = list(getattr(contract, "required_labels", []) or [])
            # A schema-valid but method-drifted answer only needs the scientific
            # correction; a missing/malformed file also needs the write instruction;
            # a choice answer outside the option list gets the choice correction.
            if need_scientific and not need_protocol and not need_choice and not need_delivery:
                # A method-only correction must still name the graded artifact and its
                # shape: expR26 recomputed correctly but wrote a self-invented
                # `answers/answer.json`; expR27 renamed the required group labels.
                prompt = correction + "\n\n" + self._answer_template_note(node, labels)
            elif need_protocol:
                prompt = self._recovery_prompt(
                    node, correction, outcome, parsed, json_error=_json_error(answer_text),
                    final_text=final_text)
                # The schema_mismatch branch already carries the full structure.
                prompt += "\n\n" + self._answer_template_note(
                    node, labels, include_schema=outcome != "schema_mismatch")
            elif need_delivery and not need_choice:
                prompt = delivery_message
            else:
                prompt = choice_message
            run2, _timed_out, _err, _final = self._step_loop(
                agent, prompt, node, max_rounds=max_rounds, deadline=deadline,
                wrapup_threshold=wrapup_after_tool_calls,
                # A correction that starts from a valid file must end with a
                # *rewritten* one.
                require_new_answer=(outcome == "valid"))
            if run2 is not None:
                run = run2
            if _final:
                final_text = _final
            timed_out = timed_out or _timed_out
            if _err:
                error = _err
            answer_text = self.environment.read_answer(node.grading.output_filename)
            outcome, parsed = classify_answer(node, answer_text)
            choice_message = _choice_violation(node, parsed)
            delivery_message = (_grouped_items_violation(node, parsed)
                                or _feature_record_violation(node, parsed)
                                or _numeric_type_violation(node, parsed))
            if stages.constraints or stages.verification or stages.recovery:
                contract, verification = self._scientific_report(
                    node, prompt_text, parsed, stages, evidence_calls())

        if timed_out and outcome != "valid":
            outcome = "timeout"
        if error and outcome != "valid":
            outcome = "error"
        usage_after = _usage_snapshot(agent)
        node_calls = list(getattr(self.environment, "tool_calls", [])[call_start:])
        return NodeSubmission(
            node_id=node.id,
            outcome=outcome,
            answer=parsed,
            answer_text=answer_text,
            tool_calls=node_calls,
            final_answer=run.final_answer if run is not None else None,
            contract=contract,
            verification=verification,
            usage=_usage_delta(usage_before, usage_after),
            error=error,
            timed_out=timed_out,
        )

    def run_question(self, agent_factory, *, scientific=None, wall_timeout=None,
                     recovery_attempts: int = 1,
                     wrapup_after_tool_calls: int | None = None,
                     nodes: list[str] | None = None,
                     inject_command: str | None = None) -> list[NodeSubmission]:
        """Run every node in topological order, one persistent agent session.

        ``nodes`` (optional) restricts which node IDs run, preserving their
        topological order; independent nodes like node2 can run without node1.
        """
        agent = agent_factory()
        submissions: list[NodeSubmission] = []
        order = self.question.topological_order()
        if nodes is not None:
            wanted = set(nodes)
            order = [nid for nid in order if nid in wanted]
        prev: NodeSubmission | None = None
        node_map = {n.id: n for n in self.question.nodes}
        for i, node_id in enumerate(order):
            node = node_map[node_id]
            call_start = len(getattr(self.environment, "tool_calls", []))
            usage_before = _usage_snapshot(agent)
            try:
                if i == 0:
                    prompt = render_first_node_prompt(self.question, node)
                else:
                    # Only the previous node's graded-result note flows forward; no
                    # future node instruction is present in this prompt. Emit the note
                    # only when the previous node failed, because the workspace may be
                    # inconsistent and the next node must verify files it relies on.
                    prev_failed = prev is not None and prev.outcome != "valid"
                    prompt = render_node_prompt(
                        node,
                        previous_retried=prev_failed,
                        previous_all_failed=prev_failed,
                        best_attempt=1 if prev and prev.outcome == "valid" else None,
                        previous_node_id=prev.node_id if prev else None,
                        show_score=False,
                    )
                prev = self.drive_node(agent, prompt, node, wall_timeout=wall_timeout,
                                       recovery_attempts=recovery_attempts, scientific=scientific,
                                       wrapup_after_tool_calls=wrapup_after_tool_calls,
                                       inject_command=inject_command if i == 0 else None)
            except Exception as exc:
                # Environment/render/verification exceptions that escape
                # ``drive_node`` must not abort an unattended multi-config
                # sweep. Record the failure as this node's outcome and keep
                # going; partial tool calls and token usage are still reported.
                prev = NodeSubmission(
                    node_id=node_id,
                    outcome="error",
                    tool_calls=list(getattr(self.environment, "tool_calls", [])[call_start:]),
                    usage=_usage_delta(usage_before, _usage_snapshot(agent)),
                    error=f"{type(exc).__name__}: {exc}",
                )
            submissions.append(prev)
        return submissions