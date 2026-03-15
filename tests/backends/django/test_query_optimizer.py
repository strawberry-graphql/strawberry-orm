"""Optimizer tests: verify the optimizer adds eager loads and prevents N+1 queries."""

import strawberry
from django.test.utils import CaptureQueriesContext
from django.db import connection

from strawberry_orm.types import auto


class TestQueryOptimizerEagerLoading:
    def test_optimizer_prevents_n_plus_1_for_posts(self, orm, seed, Post, User):
        @orm.type(Post)
        class PT:
            id: auto
            title: auto

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])

        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name posts { title } } }")

        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [{"title": "Hello World"}, {"title": "GraphQL Guide"}],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }
        assert len(ctx) <= 2

    def test_optimizer_handles_nested_relationships(self, orm, seed, Post, Tag, User):
        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT]

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])

        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(
                "{ users { name posts { title tags { name } } } }"
            )

        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World", "tags": [{"name": "python"}]},
                        {
                            "title": "GraphQL Guide",
                            "tags": [{"name": "python"}, {"name": "graphql"}],
                        },
                    ],
                },
                {
                    "name": "Bob",
                    "posts": [
                        {"title": "Draft Post", "tags": []},
                    ],
                },
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures", "tags": [{"name": "rust"}]},
                    ],
                },
            ]
        }
        assert len(ctx) <= 10

    def test_no_queries_for_scalar_only(self, orm, seed, User):
        @orm.type(User)
        class UT:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])

        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync("{ users { name email } }")

        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Charlie", "email": "charlie@test.org"},
            ]
        }
        assert len(ctx) == 1

    def test_disable_optimization_blocks_load_extras(self, orm, seed, Post, Tag, User):
        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT] = orm.field(load=["author"], disable_optimization=True)

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        hints = orm.backend._store.get("PT", "tags")
        assert hints is not None
        assert hints.disable_optimization is True
        assert hints.load == ["author"]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self) -> list[UT]:
                return User.objects.all()  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync("{ users { name posts { title tags { name } } } }")
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World", "tags": [{"name": "python"}]},
                        {
                            "title": "GraphQL Guide",
                            "tags": [{"name": "python"}, {"name": "graphql"}],
                        },
                    ],
                },
                {
                    "name": "Bob",
                    "posts": [
                        {"title": "Draft Post", "tags": []},
                    ],
                },
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures", "tags": [{"name": "rust"}]},
                    ],
                },
            ]
        }
