"""Turning a model's ask_user call into an elicitation form."""

from __future__ import annotations

import json

import pytest

from strawberry_orm.contrib.mcp import Clarification
from strawberry_orm.contrib.mcp.checks import truncate


def _parse(**arguments):
    return Clarification.from_tool_arguments(json.dumps(arguments))


def test_fields_become_a_form_schema():
    clarification, errors = _parse(
        message="Which post?",
        fields={
            "title": {
                "type": "string",
                "description": "Its title",
                "choices": ["A", "B"],
            },
            "limit": {"type": "integer"},
            "drafts": {"type": "boolean", "description": 3},
        },
    )

    assert errors == []
    assert clarification.kind == "question"
    assert clarification.schema == {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Its title", "enum": ["A", "B"]},
            "limit": {"type": "integer"},
            "drafts": {"type": "boolean"},
        },
        "required": ["title", "limit", "drafts"],
    }
    assert clarification.as_dict() == {
        "kind": "question",
        "message": "Which post?",
        "schema": clarification.schema,
    }


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("not json", "not valid JSON"),
        (json.dumps({"message": " ", "fields": {"a": {"type": "string"}}}), "message"),
        (json.dumps({"message": "Hm?", "fields": {}}), "at least one field"),
        (json.dumps({"message": "Hm?", "fields": []}), "at least one field"),
        (json.dumps({"message": "Hm?", "fields": {"a": "string"}}), "needs a type"),
        (
            json.dumps(
                {
                    "message": "Hm?",
                    "fields": {"a": {"type": "integer", "choices": ["1"]}},
                }
            ),
            "choices must be strings",
        ),
        (
            json.dumps(
                {"message": "Hm?", "fields": {"a": {"type": "string", "choices": []}}}
            ),
            "choices must be strings",
        ),
        (
            json.dumps(
                {"message": "Hm?", "fields": {"a": {"type": "string", "choices": [1]}}}
            ),
            "choices must be strings",
        ),
    ],
)
def test_unusable_questions_say_why(raw, error):
    clarification, errors = Clarification.from_tool_arguments(raw)

    assert clarification is None
    assert error in errors[0]


def test_empty_arguments_are_unusable():
    clarification, errors = Clarification.from_tool_arguments("")

    assert clarification is None
    assert errors == ["Pass message as a non-empty string."]


def test_a_confirmation_shows_the_change():
    plain = Clarification.confirm("mutation { a }", {})
    with_variables = Clarification.confirm("mutation($x: Int) { a(x: $x) }", {"x": 1})

    assert plain.message == "Run this change?\n\nmutation { a }"
    assert plain.fields == ["confirm"]
    assert plain.as_dict()["variables"] == {}
    assert with_variables.message.endswith('with {"x": 1}')


def test_truncate_cuts_every_list():
    value = {"a": [1, 2, 3], "b": {"c": [[1, 2, 3]]}, "d": "x"}

    assert truncate(value, 2) == ({"a": [1, 2], "b": {"c": [[1, 2]]}, "d": "x"}, True)
    assert truncate({"a": [1], "b": None}, 2) == ({"a": [1], "b": None}, False)
