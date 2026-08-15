"""Security properties of batching for the Django backend."""

import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_security import AbstractTestBatchingSecurity
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class TestBatchingSecurity(AbstractTestBatchingSecurity):
    def scoped_schema(self, *, batching):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )

        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(is_published=True)

        @orm.type(DjUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                return DjPost.objects.filter(author=self)  # type: ignore[return-value]

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query)

    def expected_titles_by_user(self):
        return {
            "Alice": ["Hello World", "GraphQL Guide"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }
