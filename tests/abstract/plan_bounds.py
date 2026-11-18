"""Planning a document costs what the document says, on every shape of it.

The optimizer plans the relations a selection reads below a field's rows. A
fragment spread under two relations was planned again under each, so one
spreading the next under both doubled the plan at every level:
``{ comments { ...F0 } }`` with ``fragment F<n> on CommentType { id parent {
...F<n+1> } replies { ...F<n+1> } }`` twelve levels deep planned 8,190 loads
out of a document of a few dozen fields, taking seconds before a row was
read. Each distinct part of the document is now analysed once per operation,
and an operation that would plan more loads than a small multiple of the
fields it selects is refused before anything is read. ``@skip`` and
``@include`` leave out of the plan what they leave out of the result.

Each shape is run shallow, where the plan is small and the results must be
those the optimizer-off schema gives, and deep, where a plan that doubles at
every level must be refused rather than built. The work is counted through
the backend's planner, not timed.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``,
``Tag``, ``Comment``), and on the test class:

- ``run(schema, query, variables)``, executing against the seeded rows;
- ``watch_plans(backend, monkeypatch, sink)``, appending to *sink* the number
  of relation loads in each plan the backend makes;
- ``hide(qs, model, field, value)``, excluding rows whose *field* is *value*.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob the unpublished
"Draft Post", Charlie "Rust Adventures". Tags: python on the first two,
graphql on the second, rust on the last. Bob commented "Nice post!" and
Alice replied "Thanks!" on "Hello World"; Charlie commented "Great guide" on
"GraphQL Guide".
"""

import json
from collections import Counter
from dataclasses import dataclass, field

import pytest
import strawberry
from graphql import parse, visit
from graphql.language import Visitor
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

#: The most relation loads an operation may plan per field it selects, and
#: the loads any operation may plan whatever its size.
LOADS_PER_FIELD = 8
LOADS_FLOOR = 200

SHALLOW = 3
DEEP = 12

#: What the scopes hide, which no result may show.
HIDDEN = ("Draft Post", "Great guide")


def _bound(document: str) -> int:
    return LOADS_FLOOR + LOADS_PER_FIELD * _field_nodes(document)


class _Fields(Visitor):
    def __init__(self):
        super().__init__()
        self.found = 0

    def enter_field(self, *args):
        self.found += 1


def _field_nodes(document: str) -> int:
    """Fields the document writes out, each fragment's once."""
    fields = _Fields()
    visit(parse(document), fields)
    return fields.found


@dataclass(frozen=True)
class Shape:
    """A document whose fragments spread the next level *depth* times over."""

    name: str
    root: str
    on: str
    body: object
    last: str
    head: str = "query"
    variables: dict = field(default_factory=dict)
    #: Whether deep, the plan still doubles at every level.
    branches: bool = True
    #: The depth that counts as deep, when the data under it grows too.
    deep: int = DEEP

    def document(self, depth: int) -> str:
        """``F0`` .. ``F<depth>``, each spreading the next through ``body``."""
        fragments = [
            f"fragment F{level} on {self.on} {{ {self.body(f'...F{level + 1}')} }}"
            for level in range(depth)
        ]
        fragments.append(f"fragment F{depth} on {self.on} {{ {self.last} }}")
        return "\n".join([f"{self.head} {{ {self.root} }}", *fragments])


def _comment(spread: str) -> str:
    return f"id body parent {{ {spread} }} replies {{ {spread} }}"


SHAPES = [
    Shape("two relations", "comments { ...F0 }", "CommentType", _comment, "id body"),
    Shape(
        "three relations",
        "posts { ...F0 }",
        "PostType",
        lambda spread: (
            f"id title author {{ posts {{ {spread} }} }} "
            f"comments {{ post {{ {spread} }} }} tags {{ posts {{ {spread} }} }}"
        ),
        "id title",
        deep=6,
    ),
    Shape(
        "inline fragments",
        "comments { ...F0 }",
        "CommentType",
        lambda spread: (
            f"id ... on CommentType {{ parent {{ {spread} }} }} "
            f"... @include(if: true) {{ replies {{ {spread} }} }}"
        ),
        "id body",
    ),
    Shape(
        "aliases with arguments",
        "comments { ...F0 }",
        "CommentType",
        lambda spread: (
            f"id mine: replies {{ {spread} }} "
            f'nice: replies(filter: {{ field: {{ body: {{ contains: "a" }} }} }}) '
            f"{{ {spread} }} up: parent {{ {spread} }}"
        ),
        "id body",
    ),
    Shape(
        "connection",
        "commentPage(first: 10) { edges { node { ...F0 } } }",
        "CommentNode",
        _comment,
        "id body",
    ),
    Shape(
        "relation connection",
        "users { name comments(first: 5) { edges { node { ...F0 } } } }",
        "CommentNode",
        _comment,
        "id body",
    ),
    Shape(
        "other relations to the same type",
        "users { ...F0 }",
        "UserType",
        lambda spread: (
            f"id name posts {{ author {{ {spread} }} "
            f"comments {{ author {{ {spread} }} }} }}"
        ),
        "id name",
        deep=8,
    ),
    Shape(
        "skip",
        "comments { ...F0 }",
        "CommentType",
        lambda spread: (
            f"id body parent @skip(if: true) {{ {spread} }} replies {{ {spread} }}"
        ),
        "id body",
        branches=False,
    ),
    Shape(
        "include by variable",
        "comments { ...F0 }",
        "CommentType",
        lambda spread: (
            f"id body parent @include(if: $deep) {{ {spread} }} replies {{ {spread} }}"
        ),
        "id body",
        head="query ($deep: Boolean!)",
        variables={"deep": False},
        branches=False,
    ),
    Shape(
        "skipped spreads",
        "comments { ...F0 }",
        "CommentType",
        lambda spread: (
            f"id body parent {{ id {spread} @include(if: $deep) }} "
            f"replies {{ {spread} }} "
            f"... on CommentType @include(if: $deep) {{ up: parent {{ {spread} }} }}"
        ),
        "id body",
        head="query ($deep: Boolean!)",
        variables={"deep": False},
        branches=False,
    ),
]


def _schemas(orm, models, hide):
    """The schema, and one over the same types with the optimizer off."""

    class TagType:
        id: auto
        name: auto

    # The connections' nodes. A relation connection takes its arguments from
    # a node type registered before the parent's class body.
    class CommentNode(relay.Node):
        id: relay.NodeID[int]
        body: auto

    CommentNode.__annotations__.update(
        parent=CommentNode | None, replies=list[CommentNode]
    )
    orm.type(models.Comment, name="CommentNode")(CommentNode)

    class CommentType:
        id: auto
        body: auto

    class PostType:
        id: auto
        title: auto
        tags: list[TagType]

        @classmethod
        def scope_rows(cls, qs, info):
            return hide(qs, models.Post, "title", "Draft Post")

    class UserType:
        id: auto
        name: auto
        posts: list[PostType]
        comments: ORMListConnection[CommentNode] = orm.connection.eager()

    # Set here, as types local to a function cannot be named in a string.
    CommentType.__annotations__.update(
        post=PostType,
        author=UserType,
        parent=CommentType | None,
        replies=list[CommentType],
    )
    PostType.__annotations__.update(author=UserType, comments=list[CommentType])
    TagType.__annotations__["posts"] = list[PostType]

    def comment_scope(cls, qs, info):
        return hide(qs, models.Comment, "body", "Great guide")

    CommentType.scope_rows = classmethod(comment_scope)
    orm.type(
        models.Comment,
        filters=orm.filter(models.Comment),
        order=orm.order(models.Comment),
    )(CommentType)
    orm.type(models.Post)(PostType)
    orm.type(models.User)(UserType)
    orm.type(models.Tag)(TagType)

    @strawberry.type
    class Query:
        comments: list[CommentType] = orm.field.eager()
        posts: list[PostType] = orm.field.eager()
        users: list[UserType] = orm.field.eager()
        comment_page: ORMListConnection[CommentNode] = orm.connection.eager()

    return orm.schema(query=Query), orm.schema(query=Query, optimizer=False)


class AbstractTestPlanBounds:
    @pytest.fixture
    def work(self, orm, monkeypatch):
        """Fields the planner analyses, and the loads of every plan it makes."""
        counts: Counter[str] = Counter()
        resolves_itself = orm.backend.resolves_itself

        def analysed(type_name, field_name):
            counts["analysed"] += 1
            return resolves_itself(type_name, field_name)

        monkeypatch.setattr(orm.backend, "resolves_itself", analysed)
        loads: list[int] = []
        self.watch_plans(orm.backend, monkeypatch, loads)
        return counts, loads

    async def _results(self, orm, models, work, shape, depth):
        counts, loads = work
        document = shape.document(depth)
        schema, _plain = _schemas(orm, models, self.hide)
        counts.clear()
        loads.clear()
        result = await self.run(schema, document, shape.variables)
        return document, result, counts["analysed"], sum(loads)

    async def _both(self, orm, models, shape, depth):
        """*shape*'s results with the optimizer, then without it."""
        document = shape.document(depth)
        optimized, plain = _schemas(orm, models, self.hide)
        results = []
        for schema in (optimized, plain):
            result = await self.run(schema, document, shape.variables)
            assert result.errors is None, result.errors
            results.append(result.data)
        return results

    @pytest.mark.parametrize("shape", SHAPES, ids=lambda shape: shape.name)
    async def test_a_shallow_document_plans_within_its_size(
        self, orm, models, work, shape
    ):
        document, result, analysed, loads = await self._results(
            orm, models, work, shape, SHALLOW
        )
        assert result.errors is None, result.errors
        assert analysed <= _bound(document), (analysed, _bound(document))
        assert loads <= _bound(document), (loads, _bound(document))
        shown = json.dumps(result.data)
        assert not [title for title in HIDDEN if title in shown]

    @pytest.mark.parametrize("shape", SHAPES, ids=lambda shape: shape.name)
    async def test_a_shallow_document_reads_what_the_optimizer_off_reads(
        self, orm, models, shape
    ):
        optimized, plain = await self._both(orm, models, shape, SHALLOW)
        assert optimized == plain

    @pytest.mark.parametrize("shape", SHAPES, ids=lambda shape: shape.name)
    async def test_a_deep_document_plans_within_its_size(
        self, orm, models, work, shape
    ):
        document, result, analysed, loads = await self._results(
            orm, models, work, shape, shape.deep
        )
        assert analysed <= _bound(document), (analysed, _bound(document))
        assert loads <= _bound(document), (loads, _bound(document))
        shown = json.dumps(result.data)
        assert not [title for title in HIDDEN if title in shown]
        if shape.branches:
            assert result.errors, result.data
            assert all("relation loads" in error.message for error in result.errors)
            assert not [value for value in (result.data or {}).values() if value]
        else:
            assert result.errors is None, result.errors

    @pytest.mark.parametrize(
        "shape",
        [shape for shape in SHAPES if not shape.branches],
        ids=lambda shape: shape.name,
    )
    async def test_what_a_deep_document_skips_is_not_read(self, orm, models, shape):
        optimized, plain = await self._both(orm, models, shape, shape.deep)
        assert optimized == plain

    async def test_the_reported_document_is_refused_before_it_is_planned(
        self, orm, models, work
    ):
        """The audit's document: twelve levels of fragments over two relations."""
        counts, loads = work
        document = SHAPES[0].document(DEEP)
        assert len(document) < 1000
        schema, _plain = _schemas(orm, models, self.hide)
        counts.clear()
        result = await self.run(schema, document, {})
        assert sum(loads) <= _bound(document)
        assert counts["analysed"] <= _bound(document)
        assert result.errors
        assert result.data is None or result.data == {"comments": None}

    async def test_spreading_a_fragment_more_often_plans_no_more(
        self, orm, models, work
    ):
        """One fragment under one relation, spread once or many times."""
        counts, loads = work
        schema, _plain = _schemas(orm, models, self.hide)
        planned = []
        for repeats in (1, 6):
            spreads = " ".join(["...C"] * repeats)
            document = (
                f"query {{ posts {{ title comments {{ {spreads} }} }} }}\n"
                "fragment C on CommentType { body replies { body } post { title } }"
            )
            counts.clear()
            loads.clear()
            result = await self.run(schema, document, {})
            assert result.errors is None, result.errors
            planned.append((sum(loads), counts["analysed"], result.data))
        assert planned[0] == planned[1]
