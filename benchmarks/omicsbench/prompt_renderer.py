"""Faithful re-implementation of the official OmicsBench prompt rendering.

Reproduces `system_prompt.j2`, `data_context_prompt.j2`, `node_task_prompt.j2`
and `output_schema_deriver.py` behaviour without Jinja2. The no-leak invariants
match the official renderer exactly:

- The answer schema shows the *shape* of the ground truth (leaf values replaced
  by ``<integer>``/``<string>``/``<number>``/``<boolean>``/``<any>``), never the
  values themselves.
- Choice lists come from grader ``parameters.choices`` (author-provided), not
  from ground truth.
- Grouped/labeled-matrix options expose only group labels / row / column labels,
  never the items or numeric cells.
"""
from __future__ import annotations

import json
from typing import Any

from .question import GraderEntry, Node, Question


_SYSTEM_PROMPT = """You are a computational biologist. You have access to a computational
environment with standard bioinformatics tools.

Your workspace is organized as follows:
- /data/                -- input data files (read-only)
- /workspace/answers/   -- write ONLY your graded answer files here (JSON)
- /workspace/scratch/   -- working area for intermediate files, scripts, etc.

You will receive a series of analysis subtasks. For each subtask, you will
be told what to do and where to write your answer. Complete each subtask
before moving on to the next.

How to succeed:

- Work autonomously. Do not ask for clarification. If an instruction is
  ambiguous, note your interpretation in your reasoning, pick the most
  reasonable one, and proceed.

- The graded artifact is the file you write at
  /workspace/answers/<requested_filename>, not anything you print in your
  response. If you describe the answer but do not write it to the correct
  file, you score zero. Writing the file is the job.

- Write exactly to the path and filename the subtask specifies. Do not
  rename, pluralize, change the extension, or route the file elsewhere. If a
  subtask asks for foo.json, the answer lives at /workspace/answers/foo.json.

- Answer files are strict JSON. No comments, no trailing commas, no markdown
  fences, no wrapping the answer in extra keys. The file's top-level
  structure must match the schema in the subtask literally.

- For multiple-choice answers, write the selected answer exactly as one of
  the choices shown in the subtask. Letter case is ignored by the grader, but
  spelling, punctuation, and qualifiers must match the listed choice.

- Before writing code that processes an input file, inspect it (shape,
  columns, dtypes, first few rows). Do not assume structure from memory.

- Commit to your best answer. Do not write placeholder values like "TBD",
  "uncertain", or null when the subtask asks for a concrete value.

- Do not delete files in /workspace/scratch/ between subtasks; later
  subtasks may depend on them."""


def _scalar_placeholder(value: Any) -> str:
    if isinstance(value, bool):
        return "<boolean>"
    if isinstance(value, int):
        return "<integer>"
    if isinstance(value, float):
        return "<number>"
    if isinstance(value, str):
        return "<string>"
    return "<any>"


def _derive_shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _derive_shape(v) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return ["<any>"]
        return [_derive_shape(value[0])]
    return _scalar_placeholder(value)


def derive_output_schema(entries: list[GraderEntry]) -> str:
    schema: dict[str, Any] = {}
    for entry in entries:
        key = entry.resolved_output_key()
        if isinstance(entry.ground_truth, (dict, list)):
            schema[key] = _derive_shape(entry.ground_truth)
        else:
            schema[key] = _scalar_placeholder(entry.ground_truth)
    return json.dumps(schema, indent=2)


def derive_choice_options(entries: list[GraderEntry]) -> list[dict[str, Any]]:
    options = []
    for entry in entries:
        if entry.type not in {"multiple_choice", "multi_select_choice"}:
            continue
        choices = entry.parameters.get("choices")
        if isinstance(choices, list):
            options.append({"key": entry.resolved_output_key(), "choices_json": json.dumps(choices)})
    return options


def derive_group_label_options(entries: list[GraderEntry]) -> list[dict[str, Any]]:
    options = []
    for entry in entries:
        if entry.type not in {"grouped_entity_set", "grouped_ranked_list"}:
            continue
        gt = entry.ground_truth
        if not isinstance(gt, list):
            continue
        labels: list[str] = []
        for item in gt:
            if isinstance(item, dict) and isinstance(item.get("group"), str):
                label = item["group"]
                if label not in labels:
                    labels.append(label)
        if labels:
            options.append({"key": entry.resolved_output_key(), "labels_json": json.dumps(labels)})
    return options


def derive_labeled_numeric_matrix_options(entries: list[GraderEntry]) -> list[dict[str, Any]]:
    options = []
    for entry in entries:
        if entry.type != "labeled_numeric_matrix":
            continue
        gt = entry.ground_truth
        if not isinstance(gt, list):
            continue
        rows: list[str] = []
        columns: list[str] = []
        for item in gt:
            if not isinstance(item, dict):
                continue
            row = item.get("row")
            column = item.get("column")
            if isinstance(row, str) and row not in rows:
                rows.append(row)
            if isinstance(column, str) and column not in columns:
                columns.append(column)
        if rows or columns:
            options.append({
                "key": entry.resolved_output_key(),
                "rows_json": json.dumps(rows),
                "columns_json": json.dumps(columns),
            })
    return options


def render_system_prompt() -> str:
    return _SYSTEM_PROMPT


def render_data_prompt(question: Question) -> str:
    all_modalities = sorted({m for f in question.data for m in f.data_modalities})
    all_organisms = sorted({o for f in question.data for o in f.organisms})
    lines = [
        "Below, you will find a description of the data that you will be using to "
        "accomplish your subtasks. Read through it carefully before starting any analysis.",
        "",
        question.description,
        "",
        "Data modalities:",
    ]
    for modality in all_modalities:
        lines.append(f"- {modality}")
    lines.append("Organisms:")
    for organism in all_organisms:
        lines.append(f"- {organism}")
    lines.append("Files available in /data/:")
    for index, file in enumerate(question.data, start=1):
        lines.append(f"{index}. {file.name}")
        lines.append(f"   - Description: {file.description}")
        lines.append(f"   - Format: {file.format}")
        if file.data_modalities:
            lines.append(f"   - Data modalities: {', '.join(file.data_modalities)}")
        if file.organisms:
            lines.append(f"   - Organisms: {', '.join(file.organisms)}")
        if file.platforms:
            lines.append(f"   - Platforms: {', '.join(file.platforms)}")
        lines.append(f"   - Path: /data/{file.name}.{file.format}")
    return "\n".join(lines)


def render_node_prompt(
    node: Node,
    *,
    previous_retried: bool = False,
    previous_all_failed: bool = False,
    best_attempt: int | None = None,
    previous_node_id: str | None = None,
    best_score: float | None = None,
    show_score: bool = False,
) -> str:
    out: list[str] = [f"Subtask: {node.id}"]
    if previous_retried:
        if previous_all_failed:
            out.append(
                f"Note: Every attempt at the previous subtask ({previous_node_id}) failed. "
                "The workspace may be in an inconsistent state; verify file contents before "
                "relying on them."
            )
        else:
            score_part = (
                f"had the best score ({best_score})" if show_score else "was your best attempt"
            )
            out.append(
                f"Note: Your workspace has been restored to the state from attempt {best_attempt} "
                f"of the previous subtask ({previous_node_id}), which {score_part}. Files on disk "
                "may differ from your most recent attempt."
            )
    out.append(node.instruction)
    out.append("")
    out.append(
        f"Write your answer as JSON to /workspace/answers/{node.grading.output_filename}. "
        "The schema below uses placeholders like ``<integer>``, ``<string>``, ``<number>``, "
        "``<boolean>``, and ``<any>``. You MUST replace every placeholder with an actual value of "
        "that type. Do NOT output the literal placeholder string (``<integer>`` as a string value "
        "is never correct). The shape around the placeholder(s) (brackets, braces, keys, and list "
        "wrappers) is literal."
    )
    out.append("")
    out.append(derive_output_schema(node.grading.graders))

    choice_options = derive_choice_options(node.grading.graders)
    if choice_options:
        out.append("")
        out.append("Choice fields:")
        out.append(
            "Multiple-choice fields are scalar values. Multi-select fields are lists. For "
            "list-valued fields, include every correct choice and no unsupported choices. Answer "
            "exactly as one of the listed choices. Letter case is ignored by the grader for string "
            "choices, but spelling, punctuation, and qualifiers must match the listed choice."
        )
        for option in choice_options:
            out.append(f'- "{option["key"]}": {option["choices_json"]}')

    group_label_options = derive_group_label_options(node.grading.graders)
    if group_label_options:
        out.append("")
        out.append("Grouped fields:")
        out.append("Use the listed group labels exactly for each grouped answer field.")
        for option in group_label_options:
            out.append(f'- "{option["key"]}": {option["labels_json"]}')

    matrix_options = derive_labeled_numeric_matrix_options(node.grading.graders)
    if matrix_options:
        out.append("")
        out.append("Labeled matrix fields:")
        out.append("Use these row and column labels exactly for each labeled numeric matrix field.")
        for option in matrix_options:
            out.append(f'- "{option["key"]}" rows: {option["rows_json"]}')
            out.append(f'- "{option["key"]}" columns: {option["columns_json"]}')

    return "\n".join(out)


def render_first_node_prompt(question: Question, node: Node, **kwargs) -> str:
    """System + data context + node prompt, used only for the first DAG node."""
    return f"{render_system_prompt()}\n\n{render_data_prompt(question)}\n\n{render_node_prompt(node, **kwargs)}"