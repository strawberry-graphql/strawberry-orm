"""Tests for lazy-query (waterfall) logging when relations load without prefetch."""

import logging

import strawberry

from strawberry_orm.types import auto
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class TestLazyQueryLogging:
    def test_logs_waterfall_when_author_not_prefetched(self, caplog, seed):
        from strawberry_orm import StrawberryORM

        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        orm = StrawberryORM.for_django(lazy_resolution="off")

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self) -> list[PostType]:
                return list(DjPost.objects.all())

        schema = strawberry.Schema(
            query=Query,
            extensions=[orm.lazy_resolution_extension(mode="warn")],
        )
        result = schema.execute_sync("{ posts { author { name } } }")
        assert result.errors is None

        lazy_logs = [r for r in caplog.records if r.name == "strawberry_orm.lazy_query"]
        assert len(lazy_logs) == 1
        message = lazy_logs[0].message
        assert "Unoptimized relation loads detected" in message
        assert "PostType.author" in message
        assert "path: query { posts { author { name } } }" in message
        assert "Post.author" in message
        assert "fix: return a QuerySet instead of list" in message

    def test_no_lazy_query_log_when_optimizer_prefetches(self, caplog, seed):
        from strawberry_orm import StrawberryORM

        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        orm = StrawberryORM.for_django(lazy_resolution="off")

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.auto()

        schema = strawberry.Schema(
            query=Query,
            extensions=[
                orm.optimizer_extension(),
                orm.lazy_resolution_extension(mode="warn"),
            ],
        )
        result = schema.execute_sync("{ posts { author { name } } }")
        assert result.errors is None
        assert [
            r for r in caplog.records if r.name == "strawberry_orm.lazy_query"
        ] == []

    def test_orm_schema_auto_mounts_lazy_resolution_when_enabled(self, seed):
        from strawberry_orm import StrawberryORM

        orm = StrawberryORM.for_django(
            lazy_resolution="warn",
            warn_missing_scope=False,
        )

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()

        schema = orm.schema(query=Query)
        extension_names = [ext.__name__ for ext in schema.extensions]
        assert any(name.startswith("OptimizerExtension_") for name in extension_names)
        assert any(
            name.startswith("LazyResolutionExtension_") for name in extension_names
        )

    def test_logs_reverse_fk_waterfall(self, caplog, seed):
        from strawberry_orm import StrawberryORM

        caplog.set_level(logging.WARNING, logger="strawberry_orm.lazy_query")

        orm = StrawberryORM.for_django(lazy_resolution="off")

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self) -> list[UserType]:
                return list(DjUser.objects.all())

        schema = strawberry.Schema(
            query=Query,
            extensions=[orm.lazy_resolution_extension(mode="warn")],
        )
        result = schema.execute_sync("{ users { posts { title } } }")
        assert result.errors is None

        lazy_logs = [r for r in caplog.records if r.name == "strawberry_orm.lazy_query"]
        assert len(lazy_logs) == 1
        message = lazy_logs[0].message
        assert "Unoptimized relation loads detected" in message
        assert "UserType.posts" in message
        assert "path: query { users { posts { title } } }" in message
        assert "User.posts" in message
        assert "fix: return a QuerySet instead of list" in message
