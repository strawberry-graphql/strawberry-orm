"""What one call can make the server do: each limit bounds the work left
once aliases, fragments, variables and lists have multiplied an operation,
and each call parses and validates the operation once, in a worker thread."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
import strawberry
from django.core.exceptions import SynchronousOnlyOperation
from graphql import ValidationRule
from mcp import Client
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from strawberry.directive import DirectiveLocation, DirectiveValue
from strawberry.extensions import AddValidationRules, MaxTokensLimiter

from strawberry_orm.contrib.mcp import GraphQLTool, mcp_server
from tests.backends.sqlalchemy.fixtures import main_schema
from tests.backends.sqlalchemy.models import Base, Post, User

from .fakes import schema as posts_schema
from .work import Work, longest_stall, on_loop

BUDGET = 1_000


@strawberry.input
class Match:
    title: str | None = None
    any_of: list[Match] | None = strawberry.UNSET


@strawberry.input
class Tuning:
    a: int = 0
    b: int = 0
    c: int = 0
    d: int = 0
    e: int = 0
    f: int = 0
    g: int = 0
    h: int = 0
    i: int = 0


@strawberry.directive(locations=[DirectiveLocation.FIELD])
def tagged(value: DirectiveValue[int], match: Match | None = None) -> int:
    return value


@strawberry.type
class Row:
    id: int = 1

    @strawberry.field
    def matches(self, match: Match | None = None) -> bool | None:
        return True

    @strawberry.field
    def tuned(self, tuning: list[Tuning] | None = None) -> bool | None:
        return True

    @strawberry.field
    def row(self) -> Row | None:
        return Row()

    @strawberry.field
    def rows(self, n: int = 1) -> list[Row] | None:
        return [Row() for _ in range(n)]


@strawberry.type
class Query:
    @strawberry.field
    def matches(self, match: Match | None = None) -> bool | None:
        return True

    @strawberry.field
    def tuned(self, tuning: list[Tuning] | None = None) -> bool | None:
        return True

    @strawberry.field
    def row(self) -> Row | None:
        return Row()

    @strawberry.field
    def rows(self, n: int = 1) -> list[Row] | None:
        return [Row() for _ in range(n)]


rows = strawberry.Schema(query=Query, directives=[tagged])


def _match(values: int) -> dict:
    """A ``Match`` of *values* values: its object, its list, and two for each
    branch."""
    return {"anyOf": [{"title": "x"}] * ((values - 2) // 2)}


def _aliases(count: int, selection: str) -> str:
    return " ".join(f"a{i}: {selection}" for i in range(count))


#: A literal ``Match`` of 42 values.
LITERAL = "{anyOf: [" + ", ".join(['{title: "x"}'] * 20) + "]}"
#: A literal ``Match`` of 122 values.
DEFAULT = "{anyOf: [" + ", ".join(['{title: "x"}'] * 60) + "]}"

REFUSED, STOPPED, RUNS = "refused", "stopped", "runs"


@pytest.fixture(autouse=True)
def _quiet_strawberry_errors():
    logger = logging.getLogger("strawberry.execution")
    disabled = logger.disabled
    logger.disabled = True
    yield
    logger.disabled = disabled


@pytest.fixture
def work(monkeypatch):
    return Work().install(monkeypatch)


def _assert_bounded(result, work: Work, outcome: str) -> None:
    text = result.content[0].text
    if outcome == REFUSED:
        assert result.is_error is True, (work.converted, text[:200])
        assert f"the limit is {BUDGET}." in text
        assert work.converted == 0
    elif outcome == STOPPED:
        assert f"the limit is {BUDGET}." in text, (work.converted, text[:200])
        assert work.converted <= BUDGET
    else:
        assert "errors" not in result.structured_content, text[:200]
        assert work.converted <= BUDGET


# Arguments, as the graphql tool runs them

GRAPHQL_CELLS = [
    ("one reference", "query($m: Match) { matches(match: $m) }", RUNS),
    (
        "aliases",
        "query($m: Match) { a: matches(match: $m) b: matches(match: $m) }",
        REFUSED,
    ),
    (
        "a fragment spread under aliases",
        "query($m: Match) { a: row { ...F } b: row { ...F } } "
        "fragment F on Row { matches(match: $m) }",
        REFUSED,
    ),
    (
        "nested fragments",
        "query($m: Match) { row { ...F } } "
        "fragment F on Row { a: row { ...G } b: row { ...G } } "
        "fragment G on Row { matches(match: $m) }",
        REFUSED,
    ),
    (
        "inline fragments",
        "query($m: Match) { row { ... on Row { a: matches(match: $m) } "
        "... on Row { b: matches(match: $m) } } }",
        REFUSED,
    ),
    (
        "a list parent",
        "query($m: Match) { rows(n: 3) { matches(match: $m) } }",
        STOPPED,
    ),
    (
        "nested list parents",
        "query($m: Match) { rows(n: 2) { rows(n: 2) { matches(match: $m) } } }",
        STOPPED,
    ),
    (
        "a list parent through a fragment",
        "query($m: Match) { rows(n: 3) { ...F } } "
        "fragment F on Row { matches(match: $m) }",
        STOPPED,
    ),
    (
        "a variable inside a literal",
        "query($m: Match!) { matches(match: {anyOf: [$m, $m]}) }",
        REFUSED,
    ),
    (
        "a directive argument under aliases",
        "query($m: Match) { a: row { id @tagged(match: $m) } "
        "b: row { id @tagged(match: $m) } }",
        REFUSED,
    ),
    (
        "a directive argument under a list",
        "query($m: Match) { rows(n: 3) { id @tagged(match: $m) } }",
        STOPPED,
    ),
    (
        "a big variable once and a small literal under a list",
        'query($m: Match) { matches(match: $m) rows(n: 30) { matches(match: {title: "x"}) } }',
        RUNS,
    ),
]


@pytest.mark.parametrize(
    ("query", "outcome"),
    [cell[1:] for cell in GRAPHQL_CELLS],
    ids=[cell[0] for cell in GRAPHQL_CELLS],
)
async def test_a_variable_counts_wherever_the_operation_uses_it(work, query, outcome):
    server = mcp_server(rows, graphql_tool=True, max_variable_values=BUDGET)

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": query, "variables": {"m": _match(900)}}
        )

    _assert_bounded(result, work, outcome)


OTHER_CELLS = [
    (
        "several variables",
        "query($a: Match, $b: Match) { a: matches(match: $a) "
        "b: matches(match: $b) c: matches(match: $a) }",
        {"a": _match(450), "b": _match(450)},
        REFUSED,
    ),
    (
        "input field defaults",
        "query($t: [Tuning!]) { tuned(tuning: $t) }",
        {"t": [{}] * 200},
        REFUSED,
    ),
    (
        "a variable's default under aliases",
        f"query($m: Match = {DEFAULT}) {{ " + _aliases(10, "matches(match: $m)") + " }",
        {},
        REFUSED,
    ),
    (
        "a literal under a list parent",
        f"{{ rows(n: 30) {{ matches(match: {LITERAL}) }} }}",
        {},
        STOPPED,
    ),
    (
        "a literal through a fragment under aliases",
        "{ "
        + _aliases(30, "row { ...F }")
        + f" }} fragment F on Row {{ matches(match: {LITERAL}) }}",
        {},
        REFUSED,
    ),
    (
        "a small variable under many aliases",
        "query($m: Match) { " + _aliases(100, "matches(match: $m)") + " }",
        {"m": {"anyOf": [{"title": "x"}]}},
        RUNS,
    ),
    (
        "scalar arguments under nested lists",
        "{ rows(n: 30) { rows(n: 30) { id } } }",
        {},
        RUNS,
    ),
]


@pytest.mark.parametrize(
    ("query", "variables", "outcome"),
    [cell[1:] for cell in OTHER_CELLS],
    ids=[cell[0] for cell in OTHER_CELLS],
)
async def test_arguments_count_once_expanded(work, query, variables, outcome):
    server = mcp_server(rows, graphql_tool=True, max_variable_values=BUDGET)

    async with Client(server) as client:
        result = await client.call_tool(
            "graphql", {"query": query, "variables": variables}
        )

    _assert_bounded(result, work, outcome)


# Arguments, as a stored operation runs them

STORED = f"""
query Once($m: Match) {{ matches(match: $m) }}
query Twice($m: Match) {{ a: matches(match: $m) b: matches(match: $m) }}
query Each($m: Match) {{ rows(n: 3) {{ matches(match: $m) }} }}
query Spread($m: Match) {{ a: row {{ ...Matching }} b: row {{ ...Matching }} }}
query Tuned($t: [Tuning!]) {{ tuned(tuning: $t) }}
query Literal {{ rows(n: 30) {{ matches(match: {LITERAL}) }} }}
fragment Matching on Row {{ matches(match: $m) }}
"""

STORED_CELLS = [
    ("Once", {"m": _match(900)}, RUNS),
    ("Twice", {"m": _match(900)}, REFUSED),
    ("Each", {"m": _match(900)}, STOPPED),
    ("Spread", {"m": _match(900)}, REFUSED),
    ("Tuned", {"t": [{}] * 200}, REFUSED),
    ("Literal", {}, STOPPED),
]


@pytest.mark.parametrize(
    ("name", "arguments", "outcome"),
    STORED_CELLS,
    ids=[cell[0] for cell in STORED_CELLS],
)
async def test_a_stored_operation_counts_its_arguments_once_expanded(
    work, name, arguments, outcome
):
    server = mcp_server(rows, operations=STORED, max_variable_values=BUDGET)

    async with Client(server) as client:
        result = await client.call_tool(name, arguments)

    _assert_bounded(result, work, outcome)


# The audit's operations


def _orm_server():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    alice = User(name="Alice", email="alice@example.com")
    session.add(alice)
    session.flush()
    session.add(Post(title="Hello", body="b", is_published=True, author_id=alice.id))
    session.commit()
    return mcp_server(
        main_schema,
        graphql_tool=True,
        context_getter=lambda request: {"session": session},
    )


def _orm_query(aliases: int) -> str:
    return (
        "query($f: PostFilter) { "
        + " ".join(f"u{i}: user(id: 1) {{ ...F }}" for i in range(aliases))
        + " } fragment F on UserType { posts(filter: $f) { id } }"
    )


async def test_an_orm_filter_spread_under_aliases_is_refused_off_the_loop(work):
    #: 1 + 1 + 2,499 * 4 = 9,999 values: under max_variable_values=10,000.
    big = {"any": [{"field": {"title": {"exact": "x"}}}] * 2_499}

    async with Client(_orm_server()) as client:
        result, stall = await longest_stall(
            client.call_tool(
                "graphql", {"query": _orm_query(40), "variables": {"f": big}}
            )
        )

    assert result.is_error is True
    assert "the limit is 10000." in result.content[0].text
    assert work.converted == 0
    assert stall < 1.0


async def test_the_same_orm_operation_with_a_small_filter_runs():
    async with Client(_orm_server()) as client:
        result = await client.call_tool(
            "graphql",
            {
                "query": _orm_query(40),
                "variables": {"f": {"field": {"title": {"exact": "Hello"}}}},
            },
        )

    assert result.structured_content["data"]["u0"] == {"posts": [{"id": 1}]}


async def test_a_filter_under_root_aliases_is_refused(work):
    query = (
        "query($f: PostFilter) { " + _aliases(40, "posts(filter: $f) { title }") + " }"
    )
    variables = {"f": {"anyOf": [{"title": "x"}] * 4_999}}

    async with Client(mcp_server(posts_schema, graphql_tool=True)) as client:
        result, stall = await longest_stall(
            client.call_tool("graphql", {"query": query, "variables": variables})
        )

    assert result.is_error is True
    assert "the limit is 10000." in result.content[0].text
    assert work.converted == 0
    assert stall < 1.0


async def test_a_null_for_an_argument_with_a_default_fails_as_the_field_runs(work):
    query = "query($n: Int) { rows(n: $n) { id } }"

    async with Client(mcp_server(rows, graphql_tool=True)) as client:
        result = await client.call_tool(
            "graphql", {"query": query, "variables": {"n": None}}
        )

    assert result.structured_content["data"] == {"rows": None}
    assert [e["message"] for e in result.structured_content["errors"]] == [
        "Argument 'n' has invalid value: Expected variable '$n' provided to "
        "non-null type 'Int!' not to be None."
    ]
    assert work.converted == 0


AT_BUDGET = [{"title": "Hello"}] * 3_332
ONE_PAST = [{"title": "Hello", "status": "PUBLISHED"}, *AT_BUDGET[1:]]


@pytest.mark.parametrize(
    ("branches", "refused"),
    [(AT_BUDGET, False), (ONE_PAST, True)],
    ids=["at the budget", "one past it"],
)
async def test_arguments_count_their_defaults_up_to_the_budget(branches, refused):
    # Each branch holds 3 values once its drafts default is filled in.
    variables = {"filter": {"anyOf": branches, "status": "PUBLISHED"}}
    server = mcp_server(
        posts_schema,
        operations=(
            "query ListPosts($filter: PostFilter) { posts(filter: $filter) { title } }"
        ),
    )

    async with Client(server) as client:
        result = await client.call_tool("ListPosts", variables)

    assert result.is_error is refused
    if refused:
        assert (
            "The arguments hold over 10000 values, each counted wherever an alias "
            "or fragment repeats it; the limit is 10000."
        ) in result.content[0].text
    else:
        assert result.structured_content == {"data": {"posts": [{"title": "Hello"}]}}


# The document, beyond its tokens

HEAVY = " ".join(["row { id }"] * 280)

SHAPES = [
    (
        "an unused fragment",
        "{ row { id } } fragment U on Row { " + HEAVY + " }",
        "Fragment 'U' is never used.",
    ),
    (
        "a fragment defined twice",
        "{ row { ...F } } fragment F on Row { " + HEAVY + " } fragment F on Row { id }",
        "There can be only one fragment named 'F'.",
    ),
    (
        "many operations",
        " ".join(f"query Q{i} {{ row {{ id }} }}" for i in range(100)),
        "Send exactly one operation.",
    ),
]


@pytest.mark.parametrize(
    ("query", "error"),
    [shape[1:] for shape in SHAPES],
    ids=[shape[0] for shape in SHAPES],
)
async def test_what_the_limits_do_not_measure_is_refused_before_validation(
    work, query, error
):
    async with Client(mcp_server(rows, graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.is_error is True
    assert error in result.content[0].text
    assert work.validations == []


LONG = 200_000

TEXTS = [
    ("a comment", "{ row { id } }\n#" + "x" * LONG),
    ("whitespace", "{ row { id } }" + " " * LONG),
    ("commas", "{ row { id" + "," * LONG + " } }"),
    ("a string", '{ row { matches(match: {title: "' + "x" * LONG + '"}) } }'),
    (
        "a block string",
        '{ row { matches(match: {title: """' + "x" * LONG + '"""}) } }',
    ),
]


@pytest.mark.parametrize(
    "query", [text[1] for text in TEXTS], ids=[text[0] for text in TEXTS]
)
async def test_a_long_document_is_refused_before_it_is_parsed(work, query):
    async with Client(mcp_server(rows, graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.is_error is True
    assert "characters long; the limit is 100000." in result.content[0].text
    assert [length for length, _ in work.parses if length > 100_000] == []


async def test_a_document_up_to_the_length_limit_runs():
    query = "{ row { id } }" + " " * (100_000 - 14)

    async with Client(mcp_server(rows, graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.structured_content == {"data": {"row": {"id": 1}}}


# Parsed once, validated once, in a worker thread


@strawberry.type
class DjangoQuery:
    @strawberry.field
    def ok(self) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        raise SynchronousOnlyOperation("You cannot call this from an async context")


@strawberry.type
class DjangoMutation:
    @strawberry.mutation
    def charge(self) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        raise SynchronousOnlyOperation("You cannot call this from an async context")


django_like = strawberry.Schema(query=DjangoQuery, mutation=DjangoMutation)

PATHS = [
    ("awaited", rows, {}, GraphQLTool(), "{ row { id } }"),
    (
        "without suggestions",
        rows,
        {},
        GraphQLTool(schema_tool=False),
        "{ row { id } }",
    ),
    ("sync", rows, {"sync": True}, GraphQLTool(), "{ row { id } }"),
    ("rerun with execute_sync", django_like, {}, GraphQLTool(), "{ ok }"),
    (
        "a mutation SynchronousOnlyOperation stopped",
        django_like,
        {},
        GraphQLTool(allow_mutations=True),
        "mutation { charge }",
    ),
]


@pytest.mark.filterwarnings("ignore:The MCP server fell back")
@pytest.mark.parametrize(
    ("target", "options", "tool", "query"),
    [path[1:] for path in PATHS],
    ids=[path[0] for path in PATHS],
)
async def test_the_graphql_tool_parses_and_validates_once_off_the_loop(
    work, target, options, tool, query
):
    server = mcp_server(target, graphql_tool=tool, **options)

    async with Client(server) as client:
        await client.call_tool("graphql", {"query": query})

    assert work.parses == [(len(query), False)]
    assert work.validations == [False]


STORED_PATHS = [
    ("awaited", rows, {}, "query Rows { row { id } }", "Rows"),
    ("sync", rows, {"sync": True}, "query Rows { row { id } }", "Rows"),
    ("rerun with execute_sync", django_like, {}, "query Ok { ok }", "Ok"),
    (
        "a mutation SynchronousOnlyOperation stopped",
        django_like,
        {},
        "mutation Charge { charge }",
        "Charge",
    ),
]


@pytest.mark.filterwarnings("ignore:The MCP server fell back")
@pytest.mark.parametrize(
    ("target", "options", "operations", "name"),
    [path[1:] for path in STORED_PATHS],
    ids=[path[0] for path in STORED_PATHS],
)
async def test_a_stored_operation_is_not_parsed_or_validated_again(
    work, target, options, operations, name
):
    server = mcp_server(target, operations=operations, **options)
    work.parses.clear()
    work.validations.clear()

    async with Client(server) as client:
        await client.call_tool(name, {})

    assert work.parses == []
    assert work.validations == []


class Recording(ValidationRule):
    threads: list[bool] = []

    def enter_document(self, *_args):
        Recording.threads.append(on_loop())


recorded = strawberry.Schema(
    query=Query, extensions=[lambda: AddValidationRules([Recording])]
)


@pytest.mark.parametrize(
    ("options", "call"),
    [
        ({"graphql_tool": True}, ("graphql", {"query": "{ row { id } }"})),
        ({"operations": "query Rows { row { id } }"}, ("Rows", {})),
        (
            {"graphql_tool": True, "sync": True},
            ("graphql", {"query": "{ row { id } }"}),
        ),
    ],
    ids=["graphql", "stored", "sync"],
)
async def test_the_schemas_own_rules_run_once_off_the_loop(work, options, call):
    Recording.threads.clear()
    server = mcp_server(recorded, **options)

    async with Client(server) as client:
        result = await client.call_tool(*call)

    assert result.structured_content == {"data": {"row": {"id": 1}}}
    assert Recording.threads == [False]


async def test_an_operation_within_the_schemas_token_limit_is_parsed_once(work):
    limited = strawberry.Schema(
        query=Query, extensions=[lambda: MaxTokensLimiter(max_token_count=8)]
    )
    query = "{ row { id } }"

    async with Client(mcp_server(limited, graphql_tool=True)) as client:
        result = await client.call_tool("graphql", {"query": query})

    assert result.structured_content == {"data": {"row": {"id": 1}}}
    assert work.parses == [(len(query), False)]


async def test_a_stored_operation_decodes_its_arguments_off_the_loop(monkeypatch):
    sent = json.dumps(_match(900))
    threads = []
    loads = json.loads

    def recording(text, *args, **kwargs):
        if text == sent:
            threads.append(on_loop())
        return loads(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", recording)
    server = mcp_server(rows, operations=STORED, max_variable_values=BUDGET)

    async with Client(server) as client:
        result = await client.call_tool("Once", {"m": sent})

    assert result.structured_content == {"data": {"matches": True}}
    assert threads == [False]
