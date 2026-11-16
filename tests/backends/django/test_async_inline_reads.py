"""Under async execution, a read of what is already loaded stays on the event loop.

A hop to the worker thread costs far more than reading a loaded value, so only
a read that may query is sent there.
"""

import asyncio
import time

import pytest
import strawberry
from asgiref.sync import sync_to_async
from django.db import models
from django.db.backends.sqlite3.base import SQLiteCursorWrapper
from django.db.models.query_utils import DeferredAttribute
from strawberry import relay

from strawberry_orm import StrawberryORM, _async
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class _Shouted(DeferredAttribute):
    def __get__(self, instance, cls=None):
        if instance is None:
            return self
        return super().__get__(instance, cls).upper()

    def __set__(self, instance, value):
        # Without it, the value Django stores on the row would hide __get__.
        instance.__dict__[self.field.attname] = value


class ShoutedField(models.CharField):
    descriptor_class = _Shouted


class Announcement(models.Model):
    """Only ever built in memory; its column changes the value on the way out."""

    text = ShoutedField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "async_inline_announcement"
        managed = False


@pytest.fixture
def hops(monkeypatch):
    """The fields whose reads were sent to the worker thread.

    A column read is handed only the row, so it is recorded as ``"column"``.
    """
    names = []
    run_sync = _async.run_sync

    def spy(func, *args, **kwargs):
        if func.__qualname__.endswith(
            "async_safe_resolver.<locals>.wrapper.<locals>.run"
        ):
            cells = {
                name: cell.cell_contents
                for name, cell in zip(
                    func.__code__.co_freevars, func.__closure__, strict=True
                )
            }
            info = _async.resolver_info(cells["args"], cells["kwargs"])
            names.append("column" if info is None else info.field_name)
        return run_sync(func, *args, **kwargs)

    monkeypatch.setattr(_async, "run_sync", spy)
    return names


def user_node(orm, User, Post, *, published_only=False):
    @orm.type(User)
    class AuthorType:
        id: auto
        name: auto

    @orm.type(Post, filters=orm.filter(Post), order=orm.order(Post))
    class PostType:
        id: auto
        title: auto
        author: AuthorType

        if published_only:

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(is_published=True)

    @orm.type(User)
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto
        posts: list[PostType]

    return UserNode


def users_from(queryset, UserNode):
    @strawberry.type
    class Query:
        @strawberry.field
        async def users(self) -> list[UserNode]:
            return await sync_to_async(list)(queryset)

    return strawberry.Schema(query=Query)


def titles(result):
    assert result.errors is None, result.errors
    return {
        user["name"]: [p["title"] for p in user["posts"]]
        for user in result.data["users"]
    }


@pytest.mark.asyncio
class TestAsyncInlineReads:
    async def test_loaded_rows_are_read_without_leaving_the_event_loop(
        self, orm, seed, User, Post, hops
    ):
        """Only the root's own query leaves it; the optimizer loaded the rest."""
        UserNode = user_node(orm, User, Post)

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection()

        result = await orm.schema(query=Query).execute(
            "{ users { edges { node { id name posts { title author { name } } } } } }"
        )

        assert result.errors is None, result.errors
        nodes = [edge["node"] for edge in result.data["users"]["edges"]]
        assert [(node["name"], node["posts"]) for node in nodes] == [
            (
                "Alice",
                [
                    {"title": "Hello World", "author": {"name": "Alice"}},
                    {"title": "GraphQL Guide", "author": {"name": "Alice"}},
                ],
            ),
            ("Bob", [{"title": "Draft Post", "author": {"name": "Bob"}}]),
            ("Charlie", [{"title": "Rust Adventures", "author": {"name": "Charlie"}}]),
        ]
        assert all(node["id"] for node in nodes)
        assert hops == ["users"]

    async def test_a_deferred_column_is_read_in_the_worker_thread(
        self, orm, seed, User, hops
    ):
        """Reading it queries, which Django refuses on the event loop."""

        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        @strawberry.type
        class Query:
            @strawberry.field
            async def users(self) -> list[UserType]:
                return await sync_to_async(list)(User.objects.only("id").order_by("id"))

        result = await strawberry.Schema(query=Query).execute("{ users { name } }")

        assert result.errors is None, result.errors
        assert result.data == {
            "users": [{"name": "Alice"}, {"name": "Bob"}, {"name": "Charlie"}]
        }
        assert hops == ["column"] * 3

    async def test_a_relation_given_a_filter_is_read_in_the_worker_thread(
        self, orm, seed, User, Post, hops
    ):
        """The loaded rows were not narrowed by it."""
        UserNode = user_node(orm, User, Post)

        @strawberry.type
        class Query:
            users: ORMListConnection[UserNode] = orm.connection()

        result = await orm.schema(query=Query).execute(
            """{ users { edges { node {
                name posts(filter: { field: { title: { contains: "Guide" } } }) { title }
            } } } }"""
        )

        assert result.errors is None, result.errors
        assert [
            (edge["node"]["name"], edge["node"]["posts"])
            for edge in result.data["users"]["edges"]
        ] == [("Alice", [{"title": "GraphQL Guide"}]), ("Bob", []), ("Charlie", [])]
        assert hops == ["users"] + ["posts"] * 3

    async def test_a_relation_the_app_prefetched_is_still_scoped(
        self, orm, seed, User, Post, hops
    ):
        """Its cache holds every row, the ones ``scope_rows`` hides included."""
        UserNode = user_node(orm, User, Post, published_only=True)
        schema = users_from(
            User.objects.prefetch_related("posts").order_by("id"), UserNode
        )

        result = await schema.execute("{ users { name posts { title } } }")

        assert titles(result) == {
            "Alice": ["Hello World", "GraphQL Guide"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }
        assert hops == ["posts"] * 3

    async def test_a_relation_not_loaded_is_read_in_the_worker_thread(
        self, orm, seed, User, Post, hops
    ):
        UserNode = user_node(orm, User, Post)
        schema = users_from(User.objects.order_by("id"), UserNode)

        result = await schema.execute("{ users { name posts { title } } }")

        assert titles(result) == {
            "Alice": ["Hello World", "GraphQL Guide"],
            "Bob": ["Draft Post"],
            "Charlie": ["Rust Adventures"],
        }
        assert hops == ["posts"] * 3

    async def test_a_column_whose_descriptor_changes_the_value_keeps_it(
        self, orm, hops
    ):
        @orm.type(Announcement)
        class AnnouncementType:
            id: auto
            text: auto

        @strawberry.type
        class Query:
            @strawberry.field
            async def announcements(self) -> list[AnnouncementType]:
                return [Announcement(id=1, text="doors open at nine")]

        result = await strawberry.Schema(query=Query).execute(
            "{ announcements { text } }"
        )

        assert result.errors is None, result.errors
        assert result.data == {"announcements": [{"text": "DOORS OPEN AT NINE"}]}
        assert hops == []

    @pytest.mark.parametrize(
        ("table", "query"),
        [
            ("user", "{ userPages(first: 2) { edges { node { name } } } }"),
            ("post", "{ users { posts(first: 2) { edges { node { title } } } } }"),
        ],
    )
    async def test_a_page_read_without_the_optimizer_leaves_the_loop_running(
        self, seed, User, Post, monkeypatch, table, query
    ):
        """The page is read in the worker thread and awaited, not waited on."""
        delay = 0.3
        execute = SQLiteCursorWrapper.execute

        def slow(self, sql, params=None):
            if f'FROM "{table}"' in sql:
                time.sleep(delay)
            return execute(self, sql, params)

        monkeypatch.setattr(SQLiteCursorWrapper, "execute", slow)
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(Post)
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @orm.type(User)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        @strawberry.type
        class Query:
            users: list[UserNode] = orm.field.eager()
            user_pages: ORMListConnection[UserNode] = orm.connection.eager()

        schema = orm.schema(query=Query, optimizer=False)
        gaps = []
        done = asyncio.Event()

        async def tick():
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        ticker = asyncio.create_task(tick())
        await asyncio.sleep(0.02)
        result = await schema.execute(query)
        done.set()
        await ticker

        assert result.errors is None, result.errors
        assert max(gaps) < delay / 2
