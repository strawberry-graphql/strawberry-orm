"""Tests for lazy-query (waterfall) logging when relations load without prefetch."""

import logging

import strawberry
from sqlalchemy import select

from strawberry_orm.types import auto


class TestLazyQueryLogging:
    def test_logs_waterfall_when_author_not_prefetched(
        self, caplog, sa_session, seed, User, Post
    ):
        from strawberry_orm import StrawberryORM

        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
        )

        @orm.type(User)
        class UserType:
            id: auto
            name: auto

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: UserType

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                session = info.context["session"]
                return session.execute(select(Post)).scalars().unique().all()

        schema = strawberry.Schema(
            query=Query,
            extensions=[orm.lazy_resolution_extension(mode="warn")],
        )
        result = schema.execute_sync(
            "{ posts { author { name } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None

        lazy_logs = [r for r in caplog.records if r.name == "strawberry_orm.lazy_query"]
        assert len(lazy_logs) == 1
        message = lazy_logs[0].message
        assert "Unoptimized relation loads detected" in message
        assert "PostType.author" in message
        assert "query path: query { posts { author { name } } }" in message
        assert "Post.author" in message
        assert "joinedload(Post.author)" in message

    def test_logs_reverse_fk_waterfall(self, caplog, sa_session, seed, User, Post):
        from strawberry_orm import StrawberryORM

        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
        )

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserType]:
                session = info.context["session"]
                return session.execute(select(User)).scalars().unique().all()

        schema = strawberry.Schema(
            query=Query,
            extensions=[orm.lazy_resolution_extension(mode="warn")],
        )
        result = schema.execute_sync(
            "{ users { posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None

        lazy_logs = [r for r in caplog.records if r.name == "strawberry_orm.lazy_query"]
        assert len(lazy_logs) == 1
        message = lazy_logs[0].message
        assert "Unoptimized relation loads detected" in message
        assert "UserType.posts" in message
        assert "query path: query { users { posts { title } } }" in message
        assert "User.posts" in message
        assert "selectinload(User.posts)" in message

    def test_no_lazy_query_log_when_optimizer_prefetches(
        self, caplog, sa_session, seed, User, Post
    ):
        from strawberry_orm import StrawberryORM

        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
        )

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserType]:
                return select(User)

        schema = strawberry.Schema(
            query=Query,
            extensions=[
                orm.optimizer_extension(),
                orm.lazy_resolution_extension(mode="warn"),
            ],
        )
        result = schema.execute_sync(
            "{ users { posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert [
            r for r in caplog.records if r.name == "strawberry_orm.lazy_query"
        ] == []

    def test_orm_schema_auto_mounts_lazy_resolution_when_enabled(self, User, Post):
        from strawberry_orm import StrawberryORM

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="warn",
            warn_missing_queryset=False,
        )

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field()

        schema = orm.schema(query=Query)
        extension_names = [ext.__name__ for ext in schema.extensions]
        assert any(name.startswith("OptimizerExtension_") for name in extension_names)
        assert any(
            name.startswith("LazyResolutionExtension_") for name in extension_names
        )
