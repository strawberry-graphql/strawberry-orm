"""The JSON Schemas a tool publishes for its operation's variables and data."""

from __future__ import annotations

from strawberry_orm.contrib.mcp.operations import read_operations

from .fakes import OPERATIONS, schema


def _operation(source, name=None):
    operations = {op.name: op for op in read_operations(schema._schema, source)}
    return operations[name] if name else next(iter(operations.values()))


def _data(source, name=None):
    return _operation(source, name).output_schema["properties"]["data"]


# Variables


def test_variables_become_arguments_with_their_descriptions_and_defaults():
    assert _operation(OPERATIONS, "ListPosts").input_schema == {
        "type": "object",
        "properties": {
            "filter": {
                "anyOf": [{"$ref": "#/$defs/PostFilter"}, {"type": "null"}],
                "description": "Only posts matching this.",
            },
            "first": {"type": ["integer", "null"], "default": 10},
        },
        "additionalProperties": False,
        "$defs": {
            "PostFilter": {
                "type": "object",
                "description": "Which posts to return.",
                "properties": {
                    "title": {"type": ["string", "null"], "default": None},
                    "status": {
                        "type": ["string", "null"],
                        "enum": ["DRAFT", "PUBLISHED", None],
                        "description": "Where a post is in its life.",
                    },
                    "drafts": {
                        "type": "boolean",
                        "description": "Include drafts.",
                        "default": False,
                    },
                    "anyOf": {
                        "type": ["array", "null"],
                        "items": {"$ref": "#/$defs/PostFilter"},
                    },
                },
                "additionalProperties": False,
            }
        },
    }


def test_required_variables_and_one_of_inputs():
    assert _operation(OPERATIONS, "FindPost").input_schema == {
        "type": "object",
        "properties": {
            "key": {"$ref": "#/$defs/PostKey"},
            "withStatus": {"type": "boolean"},
        },
        "required": ["key", "withStatus"],
        "additionalProperties": False,
        "$defs": {
            "PostKey": {
                "type": "object",
                "properties": {
                    "id": {"type": ["string", "integer", "null"]},
                    "title": {"type": ["string", "null"]},
                },
                "additionalProperties": False,
                "minProperties": 1,
                "maxProperties": 1,
            }
        },
    }


def test_lists_and_custom_scalars():
    source = """
    query Q($where: [PostFilter!]!, $day: Date!) {
      posts(filter: {anyOf: $where}) { title }
      postsOn(day: $day) { title }
    }
    """
    arguments = _operation(source).input_schema

    assert arguments["properties"]["where"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/PostFilter"},
    }
    assert arguments["properties"]["day"] == {"description": "Date (isoformat)"}
    assert arguments["required"] == ["where", "day"]


def test_required_input_fields():
    source = "mutation M($tag: NewTag!) { tag(input: $tag) { label } }"

    assert _operation(source).input_schema["$defs"]["NewTag"] == {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "uses": {"type": "integer", "default": 0},
        },
        "required": ["label"],
        "additionalProperties": False,
    }


def test_an_operation_without_variables_takes_no_arguments():
    assert _operation(OPERATIONS, "Missing").input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


# Data


def test_the_data_follows_the_selection_set_and_its_fragments():
    assert _data(OPERATIONS, "ListPosts") == {
        "type": ["object", "null"],
        "properties": {
            "posts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "The headline."},
                        "status": {
                            "type": "string",
                            "enum": ["DRAFT", "PUBLISHED"],
                            "description": "Where a post is in its life.",
                        },
                        "published": {"description": "Date (isoformat)"},
                        "author": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"],
                        },
                    },
                    "required": ["title", "status", "published", "author"],
                },
            }
        },
        "required": ["posts"],
    }


def test_the_result_holds_data_and_maybe_errors():
    result = _operation(OPERATIONS, "Missing").output_schema

    assert result["required"] == ["data"]
    errors = result["properties"]["errors"]
    assert errors["type"] == "array"
    assert errors["items"]["required"] == ["message"]


def test_a_field_under_include_or_skip_may_be_missing():
    post = _data(OPERATIONS, "FindPost")["properties"]["post"]

    assert post["type"] == ["object", "null"]
    assert post["required"] == ["title"]
    assert "status" in post["properties"]


def test_fields_on_some_members_of_a_union_may_be_missing():
    item = _data(OPERATIONS, "Search")["properties"]["search"]["items"]

    assert item["required"] == ["__typename"]
    assert set(item["properties"]) == {"__typename", "title", "label"}
    assert item["properties"]["__typename"] == {"type": "string"}


def test_fields_on_an_implementation_may_be_missing_and_ids_are_strings():
    named = _data(OPERATIONS, "WhoIsNamed")["properties"]["named"]

    assert named["required"] == ["name"]
    assert named["properties"]["id"] == {"type": "string"}


def test_a_fragment_on_the_interface_itself_always_applies():
    source = (
        "query Q { named { ... on Named { name } ...N } } fragment N on Named { name }"
    )

    assert _data(source)["properties"]["named"]["required"] == ["name"]


def test_a_fragment_on_an_interface_of_an_object_always_applies():
    source = "query Q { posts { author { ... on Named { name } } } }"

    author = _data(source)["properties"]["posts"]["items"]["properties"]["author"]
    assert author["required"] == ["name"]


def test_one_key_of_different_object_types_on_different_members_keeps_each():
    source = """
    query Q {
      search(text: "a") {
        ... on Post { x: author { __typename name } }
        ... on Tag { x: pinned { __typename title } }
      }
    }
    """

    item = _data(source)["properties"]["search"]["items"]
    assert item["properties"]["x"] == {
        "anyOf": [
            {
                "type": "object",
                "properties": {
                    "__typename": {"type": "string"},
                    "name": {"type": "string"},
                },
                "required": ["__typename", "name"],
            },
            {
                "type": "object",
                "properties": {
                    "__typename": {"type": "string"},
                    "title": {"type": "string", "description": "The headline."},
                },
                "required": ["__typename", "title"],
            },
        ]
    }
    assert "required" not in item


def test_a_key_selected_twice_merges_its_selections():
    source = "query Q { posts { author { name } author { id } } }"

    author = _data(source)["properties"]["posts"]["items"]["properties"]["author"]
    assert author["required"] == ["name", "id"]


def test_fragments_used_by_fragments_travel_with_the_operation():
    source = """
    query Q { posts { ...A } }
    query R { missing }
    fragment A on Post { title author { ...B } }
    fragment B on Author { name }
    fragment C on Post { status }
    query S { posts { ...C } }
    """
    operations = {op.name: op for op in read_operations(schema._schema, source)}

    assert "fragment A" in operations["Q"].document
    assert "fragment B" in operations["Q"].document
    assert "fragment C" not in operations["Q"].document
    assert "fragment" not in operations["R"].document
    author = _data(source, "Q")["properties"]["posts"]["items"]["properties"]
    assert author["author"]["required"] == ["name"]


def test_introspection_fields_have_schemas_too():
    source = 'query Q { __schema { queryType { name } } __type(name: "Post") { name } }'

    data = _data(source)["properties"]
    assert data["__schema"]["properties"]["queryType"]["required"] == ["name"]
    assert data["__type"]["type"] == ["object", "null"]
    assert data["__type"]["properties"]["name"]["type"] == ["string", "null"]


def test_a_mutation_describes_its_root_field():
    publish = _data(OPERATIONS, "Publish")["properties"]["publish"]

    assert publish["description"] == "Publish a post."
    assert publish["required"] == ["title"]
