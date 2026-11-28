"""Minimal dataclass mirror of the official OmicsBench question schema.

Only the fields the adapter drives with are modelled here. This is not the
grader; it only loads the *official* question JSON so the DAG, per-round
instruction, data references, and answer-file contract can be reproduced
faithfully. See official `_data_models/schemas/question_schema.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any
import json


@dataclass(frozen=True)
class DataFile:
    name: str
    description: str
    format: str
    data_modalities: list[str]
    organisms: list[str]
    platforms: list[str]
    uri: str
    checksum: str | None = None


@dataclass(frozen=True)
class GraderEntry:
    name: str
    type: str
    parameters: dict[str, Any]
    ground_truth: Any
    output_key: str | None = None

    def resolved_output_key(self) -> str:
        return self.output_key if self.output_key is not None else self.name


@dataclass(frozen=True)
class NodeGrading:
    output_filename: str
    aggregation: str
    graders: list[GraderEntry]


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    depends_on: list[str]
    instruction: str
    grading: NodeGrading


@dataclass(frozen=True)
class Question:
    id: str
    version: str
    description: str
    difficulty: float | None
    data: list[DataFile]
    nodes: list[Node]

    def node(self, node_id: str) -> Node:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    def topological_order(self) -> list[str]:
        graph = {n.id: set(n.depends_on) for n in self.nodes}
        return list(TopologicalSorter(graph).static_order())


def _load_data(item: dict) -> DataFile:
    return DataFile(
        name=item["name"],
        description=item.get("description", ""),
        format=item["format"],
        data_modalities=list(item.get("data_modalities", [])),
        organisms=list(item.get("organisms", [])),
        platforms=list(item.get("platforms", [])),
        uri=item["uri"],
        checksum=item.get("checksum"),
    )


def _load_grading(item: dict) -> NodeGrading:
    graders = [
        GraderEntry(
            name=g["name"],
            type=g["type"],
            parameters=dict(g.get("parameters", {})),
            ground_truth=g["ground_truth"],
            output_key=g.get("output_key"),
        )
        for g in item["graders"]
    ]
    return NodeGrading(
        output_filename=item["output_filename"],
        aggregation=item.get("aggregation", "mean"),
        graders=graders,
    )


def load_question(path: str | Path) -> Question:
    """Load an official OmicsBench question JSON into the adapter model."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    nodes = [
        Node(
            id=n["id"],
            type=n["type"],
            depends_on=list(n.get("depends_on", [])),
            instruction=n["instruction"],
            grading=_load_grading(n["grading"]),
        )
        for n in raw["nodes"]
    ]
    return Question(
        id=raw["id"],
        version=raw.get("version", ""),
        description=raw.get("description", ""),
        difficulty=raw.get("difficulty"),
        data=[_load_data(d) for d in raw.get("data", [])],
        nodes=nodes,
    )