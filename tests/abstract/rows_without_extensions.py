"""A schema serves rows held to ``scope_rows``, or refuses them.

``scope_rows`` is applied by the extensions ``orm.schema()`` installs, or by
``orm.optimizer_extension()`` given to ``strawberry.Schema``. A schema built
with neither would serve every row a resolver hands it, hidden or not, and
every row its relations reach, whatever kind of field it went through. So
there a row of an ``@orm.type`` is refused however it was reached, and so is
a ``relay.connection`` over rows, as it would count them; an
``orm.connection`` counts only the rows its own backend's scope keeps. Under
either extension the same fields serve only what the scope keeps.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``User``, ``Post``),
and on the test class:

- ``run(schema, query, context)``, executing asynchronously against the
  seeded rows, with no context at all when ``context`` is ``None``;
- ``published_only(qs)``, keeping the published posts;
- ``all_posts()``, a query of every post, in id order;
- ``load_posts()`` and ``load_users()``, every row, read before any
  execution, in id order.

Seed: Bob wrote the only draft, "Draft Post", whose id is 3.

These modules get no pytest assertion rewriting, so every assert says what
it found.
"""

import base64
import json
from collections.abc import Iterable
from typing import Annotated

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

PUBLISHED_TITLES = ["Hello World", "GraphQL Guide", "Rust Adventures"]
TITLES = [*PUBLISHED_TITLES, "Draft Post"]
DRAFT = 3


def global_id(id_):
    return base64.b64encode(f"PostType:{id_}".encode()).decode()


def titled(titles):
    return [{"title": title} for title in titles]


def edges(titles):
    return {"totalCount": len(titles), "edges": [{"node": t} for t in titled(titles)]}


#: Per field, the query and what a schema holding rows to scope serves.
FIELDS = {
    "lazy-rows": ("{ lazyRows { title } }", {"lazyRows": titled(PUBLISHED_TITLES)}),
    "lazy-query": (
        "{ lazyQuery { title } }",
        {"lazyQuery": titled(PUBLISHED_TITLES)},
    ),
    "lazy-filtered": (
        "{ lazyFiltered { title } }",
        {"lazyFiltered": titled(PUBLISHED_TITLES)},
    ),
    "lazy-one": (
        f"{{ draft: lazyOne(id: {DRAFT}) {{ title }} shown: lazyOne(id: 1) {{ title }} }}",
        {"draft": None, "shown": {"title": "Hello World"}},
    ),
    "auto": ("{ auto { title } }", {"auto": titled(PUBLISHED_TITLES)}),
    "strawberry-rows": (
        "{ strawberryRows { title } }",
        {"strawberryRows": titled(PUBLISHED_TITLES)},
    ),
    "strawberry-one": (
        f"{{ draft: strawberryOne(id: {DRAFT}) {{ title }} "
        "shown: strawberryOne(id: 1) { title } }",
        {"draft": None, "shown": {"title": "Hello World"}},
    ),
    "orm.connection": (
        "{ ormConnection { totalCount edges { node { title } } } }",
        {"ormConnection": edges(PUBLISHED_TITLES)},
    ),
    "orm.connection-query": (
        "{ ormConnectionQuery { totalCount edges { node { title } } } }",
        {"ormConnectionQuery": edges(PUBLISHED_TITLES)},
    ),
    "relay.connection": (
        "{ relayConnection { totalCount edges { node { title } } } }",
        {"relayConnection": edges(PUBLISHED_TITLES)},
    ),
    "relation": (
        "{ users { name posts { title } } }",
        {
            "users": [
                {"name": "Alice", "posts": titled(["Hello World", "GraphQL Guide"])},
                {"name": "Bob", "posts": []},
                {"name": "Charlie", "posts": titled(["Rust Adventures"])},
            ]
        },
    ),
    "node": (
        f'{{ draft: node(id: "{global_id(DRAFT)}") {{ ... on PostType {{ title }} }} '
        f'shown: node(id: "{global_id(1)}") {{ ... on PostType {{ title }} }} }}',
        {"draft": None, "shown": {"title": "Hello World"}},
    ),
    "union": (
        "{ entries { ... on PostType { title } } }",
        {"entries": titled(PUBLISHED_TITLES)},
    ),
    "own-is_type_of": (
        "{ decided { title } }",
        {"decided": titled(PUBLISHED_TITLES)},
    ),
}

#: Fields whose resolver hands over a query, which without the optimizer
#: nothing reads: on some backends it fails before any row is reached.
QUERY_FIELDS = {"lazy-query", "auto"}

SCHEMAS = {
    "orm.schema": lambda orm, Query: orm.schema(query=Query),
    "strawberry.Schema-optimizer_extension": lambda orm, Query: strawberry.Schema(
        query=Query, extensions=[orm.optimizer_extension()]
    ),
}

ROWS_REFUSED = (
    "Cannot serve {}: no extension of this schema holds its rows to scope_rows. "
    "Build the schema with orm.schema(), or give strawberry.Schema "
    "orm.optimizer_extension()."
)
PAGE_REFUSED = (
    "Cannot page relayConnection: its pages would count rows the caller cannot read."
)

#: Per field, what a schema with no extension refuses it with.
REFUSED = {
    field: {ROWS_REFUSED.format("PostType")}
    for field in FIELDS
    if field not in QUERY_FIELDS
} | {
    "relay.connection": {PAGE_REFUSED},
    "relation": {ROWS_REFUSED.format("WriterType")},
    "own-is_type_of": {ROWS_REFUSED.format("DecidedPostType")},
}


class AbstractTestRowsWithoutExtensions:
    async def query_type(self, orm, models):
        published_only = self.published_only
        all_posts = self.all_posts
        posts = await self.load_posts()
        by_id = {post.id: post for post in posts}
        users = await self.load_users()

        @classmethod
        def scope_rows(cls, qs, info):
            return published_only(qs)

        @classmethod
        def resolve_nodes(cls, *, info, node_ids, required=False):
            return [by_id.get(int(node_id)) for node_id in node_ids]

        PostType = orm.type(models.Post)(
            type(
                "PostType",
                (relay.Node,),
                {
                    "__annotations__": {"id": relay.NodeID[int], "title": auto},
                    "scope_rows": scope_rows,
                    "resolve_nodes": resolve_nodes,
                },
            )
        )

        @orm.type(models.User)
        class WriterType:
            name: auto
            posts: list[PostType]

        @orm.type(models.Post)
        class DecidedPostType:
            """Scoped by ``PostType.scope_rows``, which holds for every type
            over the model."""

            title: auto

            @classmethod
            def is_type_of(cls, obj, info):
                return isinstance(obj, models.Post)

        Entry = Annotated[PostType | WriterType, strawberry.union("Entry")]

        @strawberry.type
        class Label(relay.Node):
            id: relay.NodeID[int]
            text: str

        @strawberry.type
        class Query:
            node: relay.Node | None = relay.node()
            auto: list[PostType] = orm.field.auto()

            @orm.field.lazy
            def lazy_rows(self, info: strawberry.Info) -> list[PostType]:
                return posts

            @orm.field.lazy
            def lazy_query(self, info: strawberry.Info) -> list[PostType]:
                return all_posts()

            @orm.field.lazy(filters=orm.filter(models.Post, include=["title"]))
            def lazy_filtered(self, info: strawberry.Info) -> list[PostType]:
                return posts

            @orm.field.lazy
            def lazy_one(self, info: strawberry.Info, id: int) -> PostType | None:
                return by_id[id]

            @strawberry.field
            def strawberry_rows(self, info: strawberry.Info) -> list[PostType]:
                return posts

            @strawberry.field
            def strawberry_one(self, info: strawberry.Info, id: int) -> PostType | None:
                return by_id[id]

            @orm.connection(ORMListConnection[PostType])
            def orm_connection(self, info: strawberry.Info) -> Iterable[PostType]:
                return posts

            @orm.connection(ORMListConnection[PostType])
            def orm_connection_query(self, info: strawberry.Info) -> Iterable[PostType]:
                return all_posts()

            @relay.connection(ORMListConnection[Label])
            def labels(self, info: strawberry.Info) -> Iterable[Label]:
                return [Label(id=1, text="first"), Label(id=2, text="second")]

            @relay.connection(ORMListConnection[PostType])
            def relay_connection(self, info: strawberry.Info) -> Iterable[PostType]:
                return posts

            @strawberry.field
            def users(self, info: strawberry.Info) -> list[WriterType]:
                return users

            @strawberry.field
            def entries(self, info: strawberry.Info) -> list[Entry]:
                return posts

            @strawberry.field
            def decided(self, info: strawberry.Info) -> list[DecidedPostType]:
                return posts

        return Query

    @pytest.mark.parametrize("schema", SCHEMAS)
    @pytest.mark.parametrize("field", FIELDS)
    async def test_a_schema_holding_rows_to_scope_serves_what_it_keeps(
        self, orm, models, field, schema
    ):
        query, expected = FIELDS[field]
        built = SCHEMAS[schema](orm, await self.query_type(orm, models))
        result = await self.run(built, query, {})
        assert result.errors is None, f"{field}, {schema}: {result.errors}"
        assert result.data == expected, f"{field}, {schema}: {result.data}"

    @pytest.mark.parametrize("field", FIELDS)
    async def test_a_schema_with_no_extension_serves_no_row(self, orm, models, field):
        query, _expected = FIELDS[field]
        built = strawberry.Schema(query=await self.query_type(orm, models))
        result = await self.run(built, query, {})
        served = json.dumps(result.data)
        assert not any(title in served for title in TITLES), f"{field}: {served}"
        messages = {error.message for error in result.errors or ()}
        if field in QUERY_FIELDS:
            assert messages, f"{field}: served {served} without an error"
        else:
            assert messages == REFUSED[field], f"{field}: {messages}"

    @pytest.mark.parametrize("schema", [*SCHEMAS, "strawberry.Schema"])
    async def test_a_connection_over_plain_nodes_is_paged_in_every_schema(
        self, orm, models, schema
    ):
        """Nodes of no model are no rows to hold to scope."""
        Query = await self.query_type(orm, models)
        built = SCHEMAS.get(schema, lambda orm, Query: strawberry.Schema(query=Query))(
            orm, Query
        )
        result = await self.run(
            built, "{ labels(first: 1) { totalCount edges { node { text } } } }", {}
        )
        assert result.errors is None, f"{schema}: {result.errors}"
        assert result.data == {
            "labels": {"totalCount": 2, "edges": [{"node": {"text": "first"}}]}
        }, f"{schema}: {result.data}"

    @pytest.mark.parametrize(
        ("field", "expected", "messages"),
        [
            pytest.param("ormConnection", {"ormConnection": {"totalCount": 3}}, set()),
            pytest.param(
                "ormConnectionQuery", {"ormConnectionQuery": {"totalCount": 3}}, set()
            ),
            pytest.param("relayConnection", None, {PAGE_REFUSED}),
        ],
    )
    async def test_a_schema_with_no_extension_counts_no_hidden_row(
        self, orm, models, field, expected, messages
    ):
        """A count serves no row to refuse: an ``orm.connection`` holds it to
        its own backend's scope, and a ``relay.connection``, with none, is
        refused."""
        built = strawberry.Schema(query=await self.query_type(orm, models))
        result = await self.run(built, f"{{ {field} {{ totalCount }} }}", {})
        found = {error.message for error in result.errors or ()}
        assert (result.data, found) == (expected, messages), f"{result}"

    async def test_a_count_with_no_context_is_held_to_scope(self, orm, models):
        """No extension keeps the connection's state, and no context can: the
        count is still taken under the scope."""
        built = strawberry.Schema(query=await self.query_type(orm, models))
        result = await self.run(built, "{ ormConnectionQuery { totalCount } }", None)
        assert result.errors is None, f"{result.errors}"
        assert result.data == {"ormConnectionQuery": {"totalCount": 3}}, (
            f"{result.data}"
        )
