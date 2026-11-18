"""A connection is paged only over the rows ``scope_rows`` lets through.

Paging counts every row it is handed into the edges, their cursors,
``pageInfo`` and ``totalCount``, and each ``node`` is checked against scope
only as its edge resolves. Paged over rows the scope hides, a connection
counts them, and a hidden node answers null at its own position, so
``first: 1, after:`` finds where each one sits. Strawberry's own
``relay.connection`` pages whatever the resolver returns.

So every connection over rows of a model is paged once its rows are held to
scope: declared with ``orm.connection`` or Strawberry's ``relay.connection``;
over ``ORMListConnection``, ``ORMConnection``, ``ListConnection``,
``Connection``, a subclass paging rows its own way, an interface, a union or
a node named lazily;
on ``Query`` or on an ``@orm.type``; returning rows, a query, a generator, an
async iterable, or rows or a query awaited; with the optimizer on or off,
however the backend executes. A field the library cannot page that way is
refused when ``orm.schema()`` builds it, and in a schema built with
``strawberry.Schema`` directly, when it resolves.

A backend provides ``orm`` and ``models`` (``User``, ``Post``), and on the
test class:

- ``EXECUTIONS``: the ways a schema runs, such as ``("sync", "async")``;
- ``run(schema, query, execution)``, an awaitable result;
- ``published_only(query)``, keeping the published posts;
- ``load_posts()``, every post, read before any execution, in id order;
- ``every_post()``, a query over every post, in id order.

Seed: four posts, the third of them ("Draft Post") the only draft.
"""

import base64
from collections.abc import AsyncIterable, Iterable
from typing import Annotated

import pytest
import strawberry
from strawberry import relay
from strawberry.relay.fields import ConnectionExtension

from strawberry_orm import auto
from strawberry_orm.relay import ORMConnection, ORMListConnection

PUBLISHED_TITLES = ["Hello World", "GraphQL Guide", "Rust Adventures"]

SYNC_SHAPES = ("rows", "query", "generator")
ASYNC_SHAPES = ("awaited rows", "awaited query", "async iterable")
#: What a connection Strawberry pages can be handed: it cannot slice every
#: backend's query.
ROW_SHAPES = ("rows", "generator", "awaited rows", "async iterable")
PLAIN_SHAPES = ("rows", "awaited rows")

UNPAGED = "is paged before the rows in it are held to scope_rows"
REFUSED_AT_EXECUTION = (
    "Cannot page posts: its pages would count rows the caller cannot read."
)

#: The node type ``LazyPost`` names: the last one ``node_types`` declared.
PostNode = None
LazyPost = Annotated["PostNode", strawberry.lazy(__name__)]


def cursor(offset: int) -> str:
    return base64.b64encode(f"arrayconnection:{offset}".encode()).decode()


def offset(value: str) -> int:
    return int(base64.b64decode(value).decode().split(":")[1])


def _resolver(shape, posts, every_post, node):
    """A resolver returning every post, *posts* or *every_post()*, as *shape*."""
    if shape == "rows":

        def resolve(self, info: strawberry.Info) -> Iterable[node]:
            return list(posts)

    elif shape == "query":

        def resolve(self, info: strawberry.Info) -> Iterable[node]:
            return every_post()

    elif shape == "generator":

        def resolve(self, info: strawberry.Info) -> Iterable[node]:
            return (post for post in posts)

    elif shape == "awaited rows":

        async def resolve(self, info: strawberry.Info) -> Iterable[node]:
            return list(posts)

    elif shape == "awaited query":

        async def resolve(self, info: strawberry.Info) -> Iterable[node]:
            return every_post()

    else:

        def resolve(self, info: strawberry.Info) -> AsyncIterable[node]:
            async def stream():
                for post in posts:
                    yield post

            return stream()

    return resolve


def _own_page(node):
    """An ``ORMListConnection`` paging the rows it is given as Strawberry does."""

    @strawberry.type
    class OwnPage(ORMListConnection[node]):
        @classmethod
        def resolve_connection(cls, nodes, *, info, **kwargs):
            return relay.ListConnection.resolve_connection.__func__(
                cls, nodes, info=info, **kwargs
            )

    return OwnPage


def _list_page(node):
    @strawberry.type
    class ListPage(relay.ListConnection[node]):
        note: str = "a page"

    return ListPage


class _OwnPaging(ConnectionExtension):
    """Strawberry's paging under another name."""


def _paging_of_its_own(types, resolve, posts):
    return strawberry.field(
        resolver=resolve,
        graphql_type=relay.ListConnection[types.post],
        extensions=[_OwnPaging()],
    )


def _built_by_its_resolver(types, resolve, posts):
    def build(self, info: strawberry.Info) -> relay.ListConnection[types.post]:
        return relay.ListConnection.resolve_connection(list(posts), info=info)

    return strawberry.field(resolver=build)


def _held_by_a_type(types, resolve, posts):
    """``holder``, a ``Holder`` whose ``posts`` its resolver has paged."""

    @strawberry.type
    class Holder:
        posts: relay.ListConnection[types.post]

    def holder(self, info: strawberry.Info) -> Holder:
        return Holder(
            posts=relay.ListConnection.resolve_connection(list(posts), info=info)
        )

    return strawberry.field(resolver=holder)


def _connection(connection_type):
    return lambda types, resolve, posts: relay.connection(connection_type(types))(
        resolve
    )


#: Each declaration: its field, built from the node types, a resolver and
#: the rows, and the shapes a resolver may return, every one when ``None``.
DECLARED = {
    "orm.connection": (None, None),
    "relay.connection, ORMListConnection": (
        _connection(lambda types: ORMListConnection[types.post]),
        None,
    ),
    "relay.connection, ORMConnection": (
        _connection(lambda types: ORMConnection[types.post]),
        None,
    ),
    "relay.connection, ListConnection": (
        _connection(lambda types: relay.ListConnection[types.post]),
        None,
    ),
    "relay.connection, Connection": (
        _connection(lambda types: relay.Connection[types.post]),
        None,
    ),
    "relay.connection, a page of its own": (
        _connection(lambda types: _own_page(types.post)),
        ROW_SHAPES,
    ),
    "relay.connection over an interface": (
        _connection(lambda types: relay.ListConnection[relay.Node]),
        None,
    ),
    "relay.connection over a union": (
        _connection(
            lambda types: relay.ListConnection[
                Annotated[types.post | types.author, strawberry.union("PostOrAuthor")]
            ]
        ),
        ROW_SHAPES,
    ),
    "relay.connection over a lazily named node": (
        _connection(lambda types: relay.ListConnection[LazyPost]),
        None,
    ),
    "relay.connection, a ListConnection subclass": (
        _connection(lambda types: _list_page(types.post)),
        ("rows",),
    ),
    "a paging extension of its own": (_paging_of_its_own, ROW_SHAPES),
    "a connection its resolver builds": (_built_by_its_resolver, ("rows",)),
    "a connection a type holds": (_held_by_a_type, ("rows",)),
}

#: Declarations ``orm.schema()`` pages in scope, and whether they show
#: ``totalCount``.
PAGED = {
    "orm.connection": True,
    "relay.connection, ORMListConnection": True,
    "relay.connection, ORMConnection": True,
    "relay.connection, ListConnection": True,
    "relay.connection, Connection": True,
    "relay.connection, a page of its own": False,
    "relay.connection over an interface": True,
    "relay.connection over a union": False,
    "relay.connection over a lazily named node": True,
}

#: Declarations ``orm.schema()`` refuses to build, with what it says.
REFUSED = {
    "relay.connection, a ListConnection subclass": (
        "slices the rows it is given in Python"
    ),
    "a paging extension of its own": UNPAGED,
    "a connection its resolver builds": UNPAGED,
    "a connection a type holds": UNPAGED,
}

#: Declarations a schema built with ``strawberry.Schema`` directly pages in
#: scope; it refuses every other one as it resolves, but Strawberry's own
#: ``Connection``, which pages nothing.
PLAIN_PAGED = {"orm.connection", "relay.connection, ORMListConnection"}
PLAIN = [name for name in DECLARED if name != "relay.connection, Connection"]

LOCATIONS = ("Query", "an @orm.type")


class AbstractTestRelayConnectionScoping:
    EXECUTIONS: tuple = ("async",)

    def pytest_generate_tests(self, metafunc):
        if "paged" in metafunc.fixturenames:
            argument, declarations, within = "paged", list(PAGED), None
        elif "plain" in metafunc.fixturenames:
            argument, declarations, within = "plain", PLAIN, PLAIN_SHAPES
        else:
            return
        metafunc.parametrize(
            ("execution", "shape", argument),
            [
                (execution, shape, declared)
                for execution in self.EXECUTIONS
                for declared in declarations
                for shape in self._shapes(execution, DECLARED[declared][1], within)
            ],
        )

    @staticmethod
    def _shapes(execution, *allowed):
        possible = SYNC_SHAPES + (ASYNC_SHAPES if execution == "async" else ())
        return [
            shape
            for shape in possible
            if all(shapes is None or shape in shapes for shapes in allowed)
        ]

    def node_types(self, orm, models):
        global PostNode
        published_only = self.published_only

        @classmethod
        def scope_rows(cls, query, info):
            return published_only(query)

        post = PostNode = orm.type(models.Post)(
            type(
                "PostNode",
                (relay.Node,),
                {
                    "__annotations__": {"id": relay.NodeID[int], "title": auto},
                    "scope_rows": scope_rows,
                },
            )
        )
        author = orm.type(models.User)(
            type(
                "AuthorNode",
                (relay.Node,),
                {"__annotations__": {"id": relay.NodeID[int], "name": auto}},
            )
        )
        return type("NodeTypes", (), {"post": post, "author": author})

    async def field(self, orm, models, declared, shape):
        types = self.nodes = self.node_types(orm, models)
        posts = await self.load_posts()
        resolve = _resolver(shape, posts, self.every_post, types.post)
        build = DECLARED[declared][0]
        if build is None:
            return orm.connection(ORMListConnection[types.post])(resolve)
        return build(types, resolve, posts)

    @staticmethod
    def name(declared):
        return "holder" if declared == "a connection a type holds" else "posts"

    def schema(self, orm, models, location, declared, field, build=None, **options):
        """A schema with *field* on *location*, built by *build*.

        On an ``@orm.type`` it is a field of ``UserNode``, every user's the same.
        ``PostNode`` is named for the connections over an interface.
        """
        build = build or orm.schema
        namespace = {self.name(declared): field}
        if location != "Query":
            namespace["__annotations__"] = {"name": auto}
            user = orm.type(models.User)(type("UserNode", (), namespace))
            namespace = {
                "__annotations__": {"users": list[user]},
                "users": orm.field.eager(),
            }
        return build(
            query=strawberry.type(type("Query", (), namespace)),
            types=[self.nodes.post],
            **options,
        )

    def query(self, location, declared, arguments="", total=False):
        count = "totalCount " if total else ""
        selection = (
            f"posts{arguments} {{ {count}pageInfo {{ endCursor hasNextPage }} "
            f"edges {{ cursor node {{ ... on PostNode {{ title }} }} }} }}"
        )
        if self.name(declared) == "holder":
            selection = f"holder {{ {selection} }}"
        if location == "Query":
            return f"{{ {selection} }}"
        return f"{{ users {{ name {selection} }} }}"

    def connections(self, location, declared, data):
        parents = [data] if location == "Query" else data["users"]
        if self.name(declared) == "holder":
            parents = [parent["holder"] for parent in parents]
        return [parent["posts"] for parent in parents]

    async def page(self, schema, location, declared, execution, arguments="", **kw):
        query = self.query(location, declared, arguments, **kw)
        result = await self.run(schema, query, execution)
        assert result.errors is None, f"{query}: {result.errors}"
        connections = self.connections(location, declared, result.data)
        assert connections, f"{query}: no connection was read"
        return connections

    async def assert_paged_in_scope(self, schema, location, declared, execution):
        total = PAGED.get(declared, True)
        page = await self.page(schema, location, declared, execution, total=total)
        for connection in page:
            titles = [edge["node"]["title"] for edge in connection["edges"]]
            assert titles == PUBLISHED_TITLES, f"edges: {titles}"
            end = offset(connection["pageInfo"]["endCursor"])
            assert end == len(PUBLISHED_TITLES) - 1, f"endCursor at {end}"
            if total:
                count = connection["totalCount"]
                assert count == len(PUBLISHED_TITLES), f"totalCount {count}"

        probe = f'(first: 1, after: "{cursor(1)}")'
        for connection in await self.page(schema, location, declared, execution, probe):
            titles = [edge["node"]["title"] for edge in connection["edges"]]
            assert titles == PUBLISHED_TITLES[2:], f"after the second: {titles}"
            info = connection["pageInfo"]
            assert info["hasNextPage"] is False, f"after the second: {info}"

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("location", LOCATIONS)
    async def test_a_connection_is_paged_over_the_rows_in_scope(
        self, orm, models, execution, shape, paged, location, optimizer
    ):
        field = await self.field(orm, models, paged, shape)
        schema = self.schema(orm, models, location, paged, field, optimizer=optimizer)
        await self.assert_paged_in_scope(schema, location, paged, execution)

    @pytest.mark.parametrize("location", LOCATIONS)
    @pytest.mark.parametrize("declared", list(REFUSED))
    async def test_a_connection_paged_before_scoping_is_refused_at_build(
        self, orm, models, declared, location
    ):
        field = await self.field(orm, models, declared, "rows")
        with pytest.raises(TypeError, match=REFUSED[declared]):
            self.schema(orm, models, location, declared, field)

    async def test_a_plain_schema_pages_in_scope_or_refuses(
        self, orm, models, execution, shape, plain
    ):
        """On ``Query``: the extension refuses a connection field at any depth
        alike, and Strawberry's own paging cannot page what Django's async-safe
        resolvers on an ``@orm.type`` return."""
        field = await self.field(orm, models, plain, shape)

        def build(**options):
            return strawberry.Schema(extensions=[orm.optimizer_extension()], **options)

        schema = self.schema(orm, models, "Query", plain, field, build)
        if plain in PLAIN_PAGED:
            await self.assert_paged_in_scope(schema, "Query", plain, execution)
            return
        result = await self.run(schema, self.query("Query", plain), execution)
        messages = {error.message for error in result.errors or ()}
        assert messages == {REFUSED_AT_EXECUTION}, f"errors: {result.errors}"
        for error in result.errors:
            assert error.path[-1] == "posts", f"refused at {error.path}"
            cause = str(error.original_error.__cause__)
            assert UNPAGED in cause, f"the server log is told {cause}"
