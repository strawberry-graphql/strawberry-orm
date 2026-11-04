"""Questions the agent puts to the person asking, and mutation confirmations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

_FIELD_TYPES = ("string", "number", "integer", "boolean")

ASK_USER_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": (
            "Ask the person who asked the question for details the query needs. "
            "Your turn ends until they answer."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "One short question, shown to the person.",
                },
                "fields": {
                    "type": "object",
                    "description": "The values to collect, keyed by name.",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "type": {"enum": list(_FIELD_TYPES)},
                            "description": {"type": "string"},
                            "choices": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Allowed values, for a string.",
                            },
                        },
                        "required": ["type"],
                    },
                },
            },
            "required": ["message", "fields"],
        },
    },
}


@dataclass
class Clarification:
    """Something the agent needs from the person asking before it can go on.

    ``kind`` is ``"question"`` when the model asks for missing details, and
    ``"confirm"`` before a mutation runs, where ``query`` and ``variables``
    hold the mutation. ``schema`` is the JSON Schema of the answer, in the
    restricted form MCP elicitation accepts: an object of primitive fields.
    """

    kind: Literal["question", "confirm"]
    message: str
    schema: dict[str, Any]
    query: str | None = None
    variables: dict[str, Any] = field(default_factory=dict)

    @property
    def fields(self) -> list[str]:
        return list(self.schema["properties"])

    def as_dict(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "kind": self.kind,
            "message": self.message,
            "schema": self.schema,
        }
        if self.query is not None:
            detail["query"] = self.query
            detail["variables"] = self.variables
        return detail

    @classmethod
    def confirm(cls, query: str, variables: dict[str, Any]) -> Clarification:
        message = f"Run this change?\n\n{query}"
        if variables:
            message += f"\n\nwith {json.dumps(variables)}"
        schema = {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "boolean",
                    "title": "Run it",
                    "description": "Tick to run this change.",
                }
            },
            "required": ["confirm"],
        }
        return cls("confirm", message, schema, query=query, variables=variables)

    @classmethod
    def from_tool_arguments(cls, raw: str) -> tuple[Clarification | None, list[str]]:
        """The question a model asked through ``ask_user``, or why it is unusable."""
        try:
            arguments = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None, ["The tool arguments were not valid JSON."]
        message = arguments.get("message")
        fields = arguments.get("fields")
        if not isinstance(message, str) or not message.strip():
            return None, ["Pass message as a non-empty string."]
        if not isinstance(fields, dict) or not fields:
            return None, ["Pass fields as an object with at least one field."]
        properties: dict[str, Any] = {}
        errors: list[str] = []
        for name, spec in fields.items():
            spec = spec if isinstance(spec, dict) else {}
            kind = spec.get("type")
            if kind not in _FIELD_TYPES:
                errors.append(
                    f"Field {name!r} needs a type: {', '.join(_FIELD_TYPES)}."
                )
                continue
            prop: dict[str, Any] = {"type": kind}
            if isinstance(spec.get("description"), str):
                prop["description"] = spec["description"]
            choices = spec.get("choices")
            if choices is not None:
                if kind != "string" or not _strings(choices):
                    errors.append(f"Field {name!r}: choices must be strings.")
                    continue
                prop["enum"] = choices
            properties[name] = prop
        if errors:
            return None, errors
        schema = {
            "type": "object",
            "properties": properties,
            "required": list(properties),
        }
        return cls("question", message, schema), []


def _strings(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(item, str) for item in value)
    )
