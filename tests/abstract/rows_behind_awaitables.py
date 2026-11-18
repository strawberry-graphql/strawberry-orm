"""Rows behind awaitables are scoped, however many awaitables there are.

graphql-core awaits what a field resolves to, and each awaitable item of a
list field, and what either gives can be awaitable in turn: the library
awaits it again, and so does graphql-core for a list's items. A row reached
that way is served as surely as a row returned as it is, so it has to be
checked against ``scope_rows`` all the same: behind a coroutine an
``async def`` resolver returns unawaited, a DataLoader's future, a list of
either, a generator or async generator of them, or a sync resolver on an
``@orm.type`` that Django runs in a worker thread. That holds for a schema
field, with the optimizer and without, for a field of an ``@orm.type``, an
attribute of a plain ``@strawberry.type`` read by the default resolver, a
payload's ``data``, a subscription's events and a connection, whose count and
cursors must not count the hidden row either, and for an operation the MCP
``graphql`` tool runs.

A backend provides ``orm`` (a fresh ORM with a payload policy) and ``models``
(``User``, ``Post``), and on the test class:

- ``run(schema, query, context)``, executing asynchronously against the
  seeded rows;
- ``run_sync(schema, query, context)``, executing synchronously;
- ``subscribe(schema, query, context)``, every answer of a subscription run;
- ``context()``, what an execution's context needs to reach the seeded rows;
- ``published_only(qs)``, keeping the published posts;
- ``all_posts()``, a query of every post, in id order;
- ``load_posts()`` and ``load_users()``, every row, read before any
  execution, in id order.

Seed: Bob wrote the only draft, "Draft Post", whose id is 3.

These modules get no pytest assertion rewriting, so every assert says what
it found.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, Iterable

import pytest
import strawberry
from mcp import Client
from strawberry import relay
from strawberry.dataloader import DataLoader

from strawberry_orm import auto
from strawberry_orm.contrib.mcp import mcp_server
from strawberry_orm.relay import ORMListConnection

PUBLISHED_TITLES = ["Hello World", "GraphQL Guide", "Rust Adventures"]
DRAFT = 3
WRITTEN = {
    "Alice": ["Hello World", "GraphQL Guide"],
    "Bob": [],
    "Charlie": ["Rust Adventures"],
}


async def later(value):
    """*value*, behind a coroutine."""
    return value


def loaded(value):
    """*value*, behind a future that already holds it, as a DataLoader's is."""
    future = asyncio.get_running_loop().create_future()
    future.set_result(value)
    return future


async def stream(items):
    for item in items:
        yield item


#: What a resolver of a ``[PostType]`` field hands back for *rows*.
LIST_SHAPES = {
    "coroutine-of-coroutine": lambda rows: later(later(rows)),
    "three-coroutines-deep": lambda rows: later(later(later(rows))),
    "list-of-coroutines": lambda rows: [later(row) for row in rows],
    "list-of-futures": lambda rows: [loaded(row) for row in rows],
    "list-of-coroutines-of-coroutines": lambda rows: [
        later(later(row)) for row in rows
    ],
    "coroutine-of-list-of-coroutines": lambda rows: later([later(row) for row in rows]),
    "tuple-of-futures": lambda rows: tuple(loaded(row) for row in rows),
    "generator-of-coroutines": lambda rows: (later(row) for row in rows),
    "async-generator-of-coroutines": lambda rows: stream([later(row) for row in rows]),
}

#: What a resolver of a ``PostType`` field hands back for *row*.
SINGLE_SHAPES = {
    "coroutine-of-coroutine": lambda row: later(later(row)),
    "coroutine-of-future": lambda row: later(loaded(row)),
    "future-of-coroutine": lambda row: loaded(later(row)),
}

#: What a plain type's ``posts`` and ``post`` hold, for *rows* and *row*: one
#: of them behind awaitables, the other as it is, which is checked already.
SHELF_HOLDINGS = {
    "list-of-coroutines": lambda rows, row: ([later(each) for each in rows], row),
    "coroutine-of-coroutine": lambda rows, row: (list(rows), later(later(row))),
}

#: What a resolver of a ``[[PostType]]`` field hands back for *rows*.
NESTED_SHAPES = {
    "list-of-lists-of-coroutines": lambda rows: [[later(row) for row in rows]],
    "list-of-coroutines-of-lists": lambda rows: [later(list(rows))],
    "list-of-generators-of-futures": lambda rows: [(loaded(row) for row in rows)],
    "coroutine-of-coroutine-of-lists": lambda rows: later(later([list(rows)])),
}

#: What a resolver hands back for a *query*.
QUERY_SHAPES = {
    "coroutine-of-coroutine": lambda query: later(later(query)),
    "three-coroutines-deep": lambda query: later(later(later(query))),
}

#: The shapes a resolver run off the event loop can make: a future needs it.
OFF_LOOP_SHAPES = {
    "coroutine": lambda rows: later(rows),
    "coroutine-of-coroutine": LIST_SHAPES["coroutine-of-coroutine"],
    "list-of-coroutines": LIST_SHAPES["list-of-coroutines"],
    "coroutine-of-list-of-coroutines": LIST_SHAPES["coroutine-of-list-of-coroutines"],
}

#: Shapes of rows a connection is handed.
CONNECTION_SHAPES = {
    name: LIST_SHAPES[name]
    for name in (
        "coroutine-of-coroutine",
        "three-coroutines-deep",
        "list-of-coroutines",
        "list-of-futures",
        "generator-of-coroutines",
    )
}

CONNECTION_FIELDS = {
    "orm.connection": lambda orm: orm.connection,
    "relay.connection": lambda orm: relay.connection,
    # In a schema strawberry.Schema builds with orm.optimizer_extension() as
    # its only extension; rows_without_extensions covers one with none.
    "orm.connection-strawberry.Schema": lambda orm: orm.connection,
    "relay.connection-strawberry.Schema": lambda orm: relay.connection,
}


def connection_cases(shapes):
    """*shapes* by connection field and optimizer: a schema strawberry.Schema
    builds has the optimizer by its extension, and so has no case without."""
    return [
        pytest.param(shape, field, optimizer, id=f"{shape}-{field}-{optimizer}")
        for shape in shapes
        for field in CONNECTION_FIELDS
        for optimizer in (True, False)
        if optimizer or not field.endswith("strawberry.Schema")
        # orm.connection casts each item of a list to the node type, which a
        # coroutine refuses: see test_an_orm_connection_refuses_coroutines_to_cast.
        if shape != "list-of-coroutines" or not field.startswith("orm.connection")
    ]


def titles(rows):
    return [row["title"] for row in rows]


class AbstractTestRowsBehindAwaitables:
    async def execute(self, schema, query, context=None):
        result = await self.run(schema, query, {} if context is None else context)
        assert result.errors is None, f"{query}: {result.errors}"
        return result.data

    def post_type(self, orm, models, base=(), **fields):
        published_only = self.published_only

        @classmethod
        def scope_rows(cls, qs, info):
            return published_only(qs)

        @orm.type(models.User)
        class AuthorType:
            id: auto
            name: auto

        namespace = {
            "__annotations__": {
                "id": auto,
                "title": auto,
                "author": AuthorType | None,
                **fields,
            },
            "scope_rows": scope_rows,
        }
        return orm.type(models.Post)(type("PostType", base, namespace))

    def post_node(self, orm, models):
        return self.post_type(orm, models, base=(relay.Node,), id=relay.NodeID[int])

    # -- a schema field ------------------------------------------------------

    _LIST = "{ posts { title author { name } } awaited { title } }"

    async def list_schema(self, orm, models, shape, optimizer):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        make = LIST_SHAPES[shape]

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostType]:
                return make(posts)

            @strawberry.field
            async def awaited(self, info: strawberry.Info) -> list[PostType]:
                return make(posts)

        return orm.schema(query=Query, optimizer=optimizer)

    @staticmethod
    def assert_list_served(data, shape):
        served = (titles(data["posts"]), titles(data["awaited"]))
        assert served == (PUBLISHED_TITLES, PUBLISHED_TITLES), f"{shape}: {served}"

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", LIST_SHAPES)
    async def test_a_list_field(self, orm, models, shape, optimizer):
        schema = await self.list_schema(orm, models, shape, optimizer)
        self.assert_list_served(await self.execute(schema, self._LIST), shape)

    _SINGLE = (
        f"{{ draft: post(id: {DRAFT}) {{ title author {{ name }} }} "
        f"awaitedDraft: awaited(id: {DRAFT}) {{ title }} "
        "post(id: 1) { title } }"
    )

    async def single_schema(self, orm, models, shape, optimizer):
        PostType = self.post_type(orm, models)
        posts = {post.id: post for post in await self.load_posts()}
        make = SINGLE_SHAPES[shape]

        @strawberry.type
        class Query:
            @strawberry.field
            def post(self, info: strawberry.Info, id: int) -> PostType | None:
                return make(posts[id])

            @strawberry.field
            async def awaited(self, info: strawberry.Info, id: int) -> PostType | None:
                return make(posts[id])

        return orm.schema(query=Query, optimizer=optimizer)

    @staticmethod
    def assert_single_served(data, shape):
        assert data == {
            "draft": None,
            "awaitedDraft": None,
            "post": {"title": "Hello World"},
        }, f"{shape}: {data}"

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", SINGLE_SHAPES)
    async def test_a_single_field(self, orm, models, shape, optimizer):
        schema = await self.single_schema(orm, models, shape, optimizer)
        self.assert_single_served(await self.execute(schema, self._SINGLE), shape)

    # -- a plain type's attribute, read by the default resolver ---------------

    _SHELF = (
        f"{{ draft: shelf(id: {DRAFT}) {{ posts {{ title author {{ name }} }} "
        "post { title } } first: shelf(id: 1) { posts { title } post { title } } }"
    )

    async def shelf_schema(self, orm, models, holding, optimizer):
        """A plain ``@strawberry.type``, not an ORM type, whose attributes
        hold the posts and one post, with no resolver of their own."""
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        by_id = {post.id: post for post in posts}
        hold = SHELF_HOLDINGS[holding]

        @strawberry.type
        class Shelf:
            posts: list[PostType]
            post: PostType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def shelf(self, info: strawberry.Info, id: int) -> Shelf:
                held_posts, held_post = hold(posts, by_id[id])
                return Shelf(posts=held_posts, post=held_post)

        return orm.schema(query=Query, optimizer=optimizer)

    @staticmethod
    def assert_shelf_served(data, holding):
        served = (
            titles(data["draft"]["posts"]),
            data["draft"]["post"],
            titles(data["first"]["posts"]),
            data["first"]["post"],
        )
        assert served == (
            PUBLISHED_TITLES,
            None,
            PUBLISHED_TITLES,
            {"title": "Hello World"},
        ), f"{holding}: {served}"

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("holding", SHELF_HOLDINGS)
    async def test_a_plain_types_attribute(self, orm, models, holding, optimizer):
        schema = await self.shelf_schema(orm, models, holding, optimizer)
        self.assert_shelf_served(await self.execute(schema, self._SHELF), holding)

    # -- the MCP graphql tool -------------------------------------------------

    async def call_graphql_tool(self, schema, query):
        """*query*'s answer as the MCP ``graphql`` tool gives it to a client."""
        server = mcp_server(
            schema, graphql_tool=True, context_getter=lambda request: self.context()
        )
        async with Client(server) as client:
            result = await client.call_tool("graphql", {"query": query})
        answer = json.loads(result.content[0].text)
        assert "errors" not in answer, f"{query}: {answer}"
        return answer["data"]

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", LIST_SHAPES)
    async def test_a_list_field_through_mcp(self, orm, models, shape, optimizer):
        schema = await self.list_schema(orm, models, shape, optimizer)
        self.assert_list_served(await self.call_graphql_tool(schema, self._LIST), shape)

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", SINGLE_SHAPES)
    async def test_a_single_field_through_mcp(self, orm, models, shape, optimizer):
        schema = await self.single_schema(orm, models, shape, optimizer)
        self.assert_single_served(
            await self.call_graphql_tool(schema, self._SINGLE), shape
        )

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("holding", SHELF_HOLDINGS)
    async def test_a_plain_types_attribute_through_mcp(
        self, orm, models, holding, optimizer
    ):
        schema = await self.shelf_schema(orm, models, holding, optimizer)
        self.assert_shelf_served(
            await self.call_graphql_tool(schema, self._SHELF), holding
        )

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", NESTED_SHAPES)
    async def test_a_list_of_lists_field(self, orm, models, shape, optimizer):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        make = NESTED_SHAPES[shape]

        @strawberry.type
        class Query:
            @strawberry.field
            def shelves(self, info: strawberry.Info) -> list[list[PostType]]:
                return make(posts)

        data = await self.execute(
            orm.schema(query=Query, optimizer=optimizer),
            "{ shelves { title author { name } } }",
        )
        served = [titles(shelf) for shelf in data["shelves"]]
        assert served == [PUBLISHED_TITLES], f"{shape}: {served}"

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", QUERY_SHAPES)
    async def test_a_query(self, orm, models, shape, optimizer):
        """The query is scoped as one returned as it is, not read whole."""
        PostType = self.post_type(orm, models)
        all_posts = self.all_posts
        make = QUERY_SHAPES[shape]

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostType]:
                return make(all_posts())

        data = await self.execute(
            orm.schema(query=Query, optimizer=optimizer),
            "{ posts { title author { name } } }",
        )
        served = titles(data["posts"])
        assert served == PUBLISHED_TITLES, f"{shape}: {served}"

    # -- a field of an @orm.type ---------------------------------------------

    def writer_type(self, orm, models, PostType, kind, resolve):
        """The users, with what each wrote resolved by *resolve(user)*."""
        if kind == "async":

            async def written(self, info: strawberry.Info) -> list[PostType]:
                return resolve(self)

        else:

            def written(self, info: strawberry.Info) -> list[PostType]:
                return resolve(self)

        field = orm.field.lazy(written) if kind == "lazy" else strawberry.field(written)
        namespace = {"__annotations__": {"name": auto}, "written": field}
        return orm.type(models.User)(type("WriterType", (), namespace))

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("kind", ["sync", "async", "lazy"])
    @pytest.mark.parametrize("shape", OFF_LOOP_SHAPES)
    async def test_a_field_of_an_orm_type(self, orm, models, shape, kind, optimizer):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        users = await self.load_users()
        make = OFF_LOOP_SHAPES[shape]
        WriterType = self.writer_type(
            orm,
            models,
            PostType,
            kind,
            lambda user: make([post for post in posts if post.author_id == user.id]),
        )

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.Info) -> list[WriterType]:
                return users

        data = await self.execute(
            orm.schema(query=Query, optimizer=optimizer),
            "{ users { name written { title author { name } } } }",
        )
        served = {user["name"]: titles(user["written"]) for user in data["users"]}
        assert served == WRITTEN, f"{shape}, {kind}: {served}"

    # -- payloads and subscriptions ------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", OFF_LOOP_SHAPES)
    async def test_a_payload(self, orm, models, shape, optimizer):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        make = OFF_LOOP_SHAPES[shape]

        @strawberry.type
        class Query:
            @orm.payload.query
            def posts(self, info: strawberry.Info) -> list[PostType]:
                return make(posts)

            @orm.payload.query
            async def awaited(self, info: strawberry.Info) -> list[PostType]:
                return make(posts)

        data = await self.execute(
            orm.schema(query=Query, optimizer=optimizer),
            "{ posts { data { title author { name } } } awaited { data { title } } }",
        )
        served = (titles(data["posts"]["data"]), titles(data["awaited"]["data"]))
        assert served == (PUBLISHED_TITLES, PUBLISHED_TITLES), f"{shape}: {served}"

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", LIST_SHAPES)
    async def test_a_subscription_event(self, orm, models, shape, optimizer):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        make = LIST_SHAPES[shape]

        @strawberry.type
        class Query:
            @strawberry.field
            def ok(self) -> bool:
                return True

        @strawberry.type
        class Subscription:
            @strawberry.subscription
            async def posts(
                self, info: strawberry.Info
            ) -> AsyncGenerator[list[PostType], None]:
                yield make(posts)

        schema = orm.schema(query=Query, subscription=Subscription, optimizer=optimizer)
        answers = await self.subscribe(
            schema, "subscription { posts { title author { name } } }", {}
        )
        assert [answer.errors for answer in answers] == [None], f"{shape}: {answers}"
        served = titles(answers[0].data["posts"])
        assert served == PUBLISHED_TITLES, f"{shape}: {served}"

    # -- connections ---------------------------------------------------------

    _WHOLE = "{ posts { totalCount edges { node { title author { name } } } } }"

    def connection_schema(self, orm, models, field, resolve, optimizer):
        PostNode = self.post_node(orm, models)
        connection = CONNECTION_FIELDS[field](orm)

        @strawberry.type
        class Query:
            @connection(ORMListConnection[PostNode])
            def posts(self, info: strawberry.Info) -> Iterable[PostNode]:
                return resolve()

        if field.endswith("strawberry.Schema"):
            return strawberry.Schema(
                query=Query, extensions=[orm.optimizer_extension()]
            )
        return orm.schema(query=Query, optimizer=optimizer)

    async def _connection(self, orm, models, field, resolve, optimizer):
        schema = self.connection_schema(orm, models, field, resolve, optimizer)
        whole = await self.execute(schema, self._WHOLE)
        page = await self.execute(
            schema,
            "{ posts(first: 2) { totalCount pageInfo { hasNextPage } "
            "edges { node { title } } } }",
        )
        return whole["posts"], page["posts"]

    @pytest.mark.parametrize(
        ("shape", "field", "optimizer"), connection_cases(CONNECTION_SHAPES)
    )
    async def test_a_connection_over_rows(self, orm, models, shape, field, optimizer):
        """Counted, cut into pages and given cursors without the hidden row."""
        posts = await self.load_posts()
        make = CONNECTION_SHAPES[shape]
        whole, page = await self._connection(
            orm, models, field, lambda: make(posts), optimizer
        )
        served = (
            whole["totalCount"],
            [edge["node"]["title"] for edge in whole["edges"]],
            page["totalCount"],
            page["pageInfo"]["hasNextPage"],
            [edge["node"]["title"] for edge in page["edges"]],
        )
        assert served == (
            3,
            PUBLISHED_TITLES,
            3,
            True,
            PUBLISHED_TITLES[:2],
        ), f"{shape}, {field}: {served}"

    @pytest.mark.parametrize(
        ("shape", "field", "optimizer"), connection_cases(QUERY_SHAPES)
    )
    async def test_a_connection_over_a_query(
        self, orm, models, shape, field, optimizer
    ):
        all_posts = self.all_posts
        make = QUERY_SHAPES[shape]
        whole, page = await self._connection(
            orm, models, field, lambda: make(all_posts()), optimizer
        )
        served = (
            whole["totalCount"],
            [edge["node"]["title"] for edge in whole["edges"]],
            page["totalCount"],
            page["pageInfo"]["hasNextPage"],
            [edge["node"]["title"] for edge in page["edges"]],
        )
        assert served == (
            3,
            PUBLISHED_TITLES,
            3,
            True,
            PUBLISHED_TITLES[:2],
        ), f"{shape}, {field}: {served}"

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_an_orm_connection_refuses_coroutines_to_cast(
        self, orm, models, optimizer
    ):
        """``orm.connection`` casts each item of the list it is handed to the
        node type, before the connection reads it, and a coroutine cannot be
        cast: the field fails, serving nothing."""
        posts = await self.load_posts()
        schema = self.connection_schema(
            orm,
            models,
            "orm.connection",
            lambda: LIST_SHAPES["list-of-coroutines"](posts),
            optimizer,
        )
        result = await self.run(schema, self._WHOLE, {})
        messages = [error.message for error in result.errors or ()]
        assert result.data is None, f"{result.data}"
        assert len(messages) == 1, f"{messages}"
        assert "__as_strawberry_type__" in messages[0], f"{messages}"

    # -- DataLoader batching -------------------------------------------------

    def _loader(self, posts, batches):
        by_id = {post.id: post for post in posts}

        async def load(keys):
            batches.append(sorted(keys))
            return [by_id[key] for key in keys]

        return DataLoader(load_fn=load)

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_loads_a_list_waits_on_are_batched(self, orm, models, optimizer):
        """Loads asked for by a list's items, each in a coroutine of its own,
        still go to the loader together."""
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostType]:
                async def one(key):
                    return await info.context["loader"].load(key)

                return [one(post.id) for post in posts]

        batches = []
        data = await self.execute(
            orm.schema(query=Query, optimizer=optimizer),
            "{ posts { title } }",
            {"loader": self._loader(posts, batches)},
        )
        served = titles(data["posts"])
        assert served == PUBLISHED_TITLES, f"{served}"
        assert batches == [[1, 2, 3, 4]], f"{batches}"

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_loads_every_parent_waits_on_are_batched(
        self, orm, models, optimizer
    ):
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        users = await self.load_users()
        batches = []
        loader = self._loader(posts, batches)

        def resolve(user):
            async def one(key):
                return await loader.load(key)

            return [one(post.id) for post in posts if post.author_id == user.id]

        WriterType = self.writer_type(orm, models, PostType, "async", resolve)

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.Info) -> list[WriterType]:
                return users

        data = await self.execute(
            orm.schema(query=Query, optimizer=optimizer),
            "{ users { name written { title } } }",
        )
        served = {user["name"]: titles(user["written"]) for user in data["users"]}
        assert served == WRITTEN, f"{served}"
        assert batches == [[1, 2, 3, 4]], f"{batches}"

    # -- sync execution ------------------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("shape", OFF_LOOP_SHAPES)
    async def test_sync_execution_serves_nothing_left_to_await(
        self, orm, models, shape, optimizer
    ):
        """Sync execution awaits nothing: it refuses the whole answer. Off the
        event loop's thread it says so as it fails to cancel what was left."""
        PostType = self.post_type(orm, models)
        posts = await self.load_posts()
        make = OFF_LOOP_SHAPES[shape]

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.Info) -> list[PostType]:
                return make(posts)

        result = await self.run_sync(
            orm.schema(query=Query, optimizer=optimizer), "{ posts { title } }", {}
        )
        messages = [error.message for error in result.errors or ()]
        assert result.data is None, f"{shape}: {result.data}"
        assert len(messages) == 1, f"{shape}: {messages}"
        assert messages[0].startswith(
            (
                "GraphQL execution failed to complete synchronously.",
                "There is no current event loop in thread",
            )
        ), f"{shape}: {messages}"
