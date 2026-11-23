"""Shared environment semantics for text and native tool protocols.

The profile contains no wire-format instructions. Text and native renderers add
those independently, so an external environment cannot accidentally inherit XML
examples or a local coding-agent identity.
"""
from __future__ import annotations

import ast
import copy
import json
import math
import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class EnvironmentProfile:
    """Host-selected role and task instructions for one tool environment."""

    name: str
    role: str
    instructions: tuple[str, ...] = field(default_factory=tuple)
    native_instructions: tuple[str, ...] | None = None
    budget_instruction: str = (
        "Converge on the current environment task and reserve enough budget "
        "for required actions, verification and the final response."
    )
    clock: object = field(default=None, compare=False, hash=False)

    def __post_init__(self):
        if not self.name.strip() or not self.role.strip():
            raise ValueError("Environment name and role must be non-empty")
        object.__setattr__(self, "instructions", tuple(self.instructions))
        if self.native_instructions is not None:
            object.__setattr__(self, "native_instructions", tuple(self.native_instructions))
        if self.clock is not None and not (hasattr(self.clock, "now") and hasattr(self.clock, "iso")):
            raise ValueError("Environment clock must expose now() and iso()")

    def time_line(self) -> str:
        return "" if self.clock is None else f"Current simulated time: {self.clock.iso()}"

    def text_instructions(self) -> str:
        lines = ["- " + item for item in self.instructions if item.strip()]
        if self.time_line():
            lines.insert(0, "- " + self.time_line())
        return "\n".join(lines)

    def native_prompt(self) -> str:
        instructions = (self.native_instructions if self.native_instructions is not None
                        else self.instructions)
        parts = [self.role, *instructions]
        if self.time_line():
            parts.append(self.time_line())
        return "\n".join(part for part in parts if part.strip())


_DECLARATION = re.compile(r"^(?P<kind>[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?)(?:=(?P<default>.*))?$")
_TYPE_MAP = {
    "str": "string", "string": "string",
    "int": "integer", "integer": "integer",
    "float": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean",
    "dict": "object", "mapping": "object", "object": "object",
    "list": "array", "tuple": "array", "set": "array", "array": "array",
    "none": "null", "null": "null",
}
_ITEM_TYPE_MAP = {
    "str": "string", "string": "string",
    "int": "integer", "integer": "integer",
    "float": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean",
    "dict": "object", "object": "object",
    "list": "array", "array": "array",
}


def _declaration_schema(declaration: str) -> tuple[dict, bool, object]:
    match = _DECLARATION.match(declaration)
    if not match:
        raise ValueError("Unsupported tool argument declaration: " + repr(declaration))
    kind = match.group("kind")
    has_default = match.group("default") is not None
    default = None
    if has_default:
        raw = match.group("default")
        try:
            default = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            default = raw
    if kind == "any":
        schema = {}
    else:
        container, _, item = kind.partition("[")
        json_type = _TYPE_MAP.get(container)
        if json_type is None:
            raise ValueError("Unsupported tool argument type: " + container)
        schema = {"type": json_type}
        if item:
            item_kind = _ITEM_TYPE_MAP.get(item.rstrip("]"))
            if item_kind is None:
                raise ValueError("Unsupported array item type: " + item)
            schema["items"] = {"type": item_kind}
    if has_default:
        schema["default"] = default
    return schema, not has_default, default


def tool_json_schema(tool: dict) -> dict:
    """Return an object JSON Schema without losing structured argument types."""

    if "json_schema" in tool:
        schema = copy.deepcopy(tool["json_schema"])
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError("Tool json_schema must be an object schema")
        schema.setdefault("properties", {})
        schema.setdefault("required", [])
        schema.setdefault("additionalProperties", False)
        return schema
    properties, required = {}, []
    for name, declaration in tool.get("schema", {}).items():
        if not isinstance(declaration, str):
            raise ValueError("Legacy tool declarations must be strings")
        value, needed, _ = _declaration_schema(declaration)
        properties[name] = value
        if needed:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _matches_type(value, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return type(value) is int
    if expected == "number":
        return type(value) in {int, float} and math.isfinite(value)
    if expected == "boolean":
        return type(value) is bool
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    raise ValueError("Unsupported JSON Schema type: " + expected)


def _validate(value, schema: dict, location: str):
    if not isinstance(schema, dict):
        raise ValueError("Invalid JSON Schema at " + location)
    expected = schema.get("type")
    if expected is not None:
        choices = [expected] if isinstance(expected, str) else expected
        if not isinstance(choices, list) or not choices:
            raise ValueError("Invalid JSON Schema type at " + location)
        if not any(_matches_type(value, choice) for choice in choices):
            raise ValueError(f"{location} must be {' or '.join(choices)}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{location} is not an allowed value")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ValueError(f"{location} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{location} is too long")
    if type(value) in {int, float} and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"{location} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValueError(f"{location} is above maximum")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ValueError(f"{location} has too few items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ValueError(f"{location} has too many items")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], f"{location}[{index}]")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        missing = [name for name in required if name not in value]
        if missing:
            raise ValueError(f"{location} missing required argument: {missing[0]}")
        additional = schema.get("additionalProperties", True)
        for name, item in value.items():
            if name in properties:
                _validate(item, properties[name], f"{location}.{name}")
            elif additional is False:
                raise ValueError(f"{location} unknown argument: {name}")
            elif isinstance(additional, dict):
                _validate(item, additional, f"{location}.{name}")


def validate_tool_arguments(tool: dict, args) -> None:
    """Validate text or native arguments against the same object schema."""

    if not isinstance(args, dict):
        raise ValueError("Tool parameters must be an object")
    _validate(args, tool_json_schema(tool), "arguments")


def example_arguments(tool: dict) -> dict:
    """Build a small syntactically valid example from required arguments only."""

    schema = tool_json_schema(tool)

    def example(spec):
        if "default" in spec:
            return copy.deepcopy(spec["default"])
        if "enum" in spec and spec["enum"]:
            return copy.deepcopy(spec["enum"][0])
        expected = spec.get("type", "string")
        if isinstance(expected, list):
            expected = next((item for item in expected if item != "null"), expected[0])
        return {
            "string": "value", "integer": 1, "number": 1.0,
            "boolean": True, "array": [], "object": {}, "null": None,
        }.get(expected, "value")

    return {
        name: example(schema.get("properties", {}).get(name, {}))
        for name in schema.get("required", [])
    }


def example_tool_call(name: str, tool: dict) -> str:
    return "<tool>" + json.dumps(
        {"name": name, "args": example_arguments(tool)}, ensure_ascii=False,
    ) + "</tool>"
