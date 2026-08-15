"""Optimizer tests: verify the optimizer adds eager loads and prevents N+1 queries."""

import strawberry
from sqlalchemy import select

from strawberry_orm.types import auto


class TestQueryOptimizerEagerLoading:
    def test_optimizer_prevents_n_plus_1_for_posts(
        self, orm, sa_session, seed, query_counter, Post, User
    ):
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
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
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
        assert len(query_counter) <= 2

    def test_optimizer_handles_nested_relationships(
        self, orm, sa_session, seed, query_counter, Post, Tag, User
    ):
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
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title tags { name } } } }",
            context_value={"session": sa_session},
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
        assert len(query_counter) <= 3

    def test_no_queries_for_scalar_only(
        self, orm, sa_session, seed, query_counter, User
    ):
        @orm.type(User)
        class UT:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name email } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Charlie", "email": "charlie@test.org"},
            ]
        }
        assert len(query_counter) == 1

    def test_disable_optimization_blocks_load_extras(
        self, orm, sa_session, seed, query_counter, Post, Tag, User
    ):
        @orm.type(Tag)
        class TT:
            id: auto
            name: auto

        @orm.type(Post)
        class PT:
            id: auto
            title: auto
            tags: list[TT] = orm.field.auto(using=["author"], disable_optimization=True)

        @orm.type(User)
        class UT:
            id: auto
            name: auto
            posts: list[PT]

        hints = orm.backend._store.get("PT", "tags")
        assert hints is not None
        assert hints.disable_optimization is True
        assert hints.using == ["author"]

        @strawberry.type
        class Q:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UT]:
                return select(User)  # type: ignore[return-value]

        schema = strawberry.Schema(query=Q, extensions=[orm.optimizer_extension()])
        result = schema.execute_sync(
            "{ users { name posts { title tags { name } } } }",
            context_value={"session": sa_session},
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
