"""Batching semantics for the Django backend."""

import strawberry
from django.db import connection
from django.test.utils import CaptureQueriesContext

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_semantics import AbstractTestBatchingSemantics
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class TestBatchingSemantics(AbstractTestBatchingSemantics):
    def schema_for(self, shape, *, batching=True):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )

        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

        @orm.type(DjUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                if shape == "ordered":
                    return DjPost.objects.filter(author=self).order_by(  # type: ignore[return-value]
                        "-title"
                    )
                if shape == "raises_for_bob" and self.name == "Bob":
                    raise RuntimeError("no posts for Bob")
                if shape in ("filtered", "aliased", "raises_for_bob"):
                    return DjPost.objects.filter(  # type: ignore[return-value]
                        author=self, is_published=True
                    )
                return DjPost.objects.filter(author=self)  # type: ignore[return-value]

            @strawberry.field
            def all_posts(self) -> list[PT]:
                return DjPost.objects.filter(author=self)  # type: ignore[return-value]

            @strawberry.field
            def maybe_posts(self) -> list[PT] | None:
                if shape == "raises_for_bob" and self.name == "Bob":
                    raise RuntimeError("no posts for Bob")
                return DjPost.objects.filter(author=self)  # type: ignore[return-value]

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query)

    def count_queries(self, schema, query):
        with CaptureQueriesContext(connection) as ctx:
            schema.execute_sync(query)
        return len(ctx)
