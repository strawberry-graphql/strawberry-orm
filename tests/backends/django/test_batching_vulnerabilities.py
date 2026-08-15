"""Adversarial batching tests for the Django backend."""

import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_vulnerabilities import (
    AbstractTestBatchingVulnerabilities,
)
from tests.backends.django.models import Comment as DjComment
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class TestBatchingVulnerabilities(AbstractTestBatchingVulnerabilities):
    def nested_schema(self, *, batching):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )

        @orm.type(DjComment)
        class CT:
            id: auto
            body: auto

        @orm.type(DjPost)
        class PT:
            id: auto
            title: auto

            @strawberry.field
            def comments(self) -> list[CT]:
                return DjComment.objects.filter(post=self).order_by("id")  # type: ignore[return-value]

        @orm.type(DjUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                return DjPost.objects.filter(author=self).order_by("id")  # type: ignore[return-value]

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def give_every_user_a_comment(self):
        author = DjUser.objects.get(name="Alice")
        for user in DjUser.objects.exclude(name="Alice"):
            post = DjPost.objects.filter(author=user).first()
            DjComment.objects.create(
                body=f"comment for {user.name}", post=post, author=author
            )

    def parent_scoped_query(self):
        from strawberry_orm.backends.django import DjangoBackend

        backend = DjangoBackend(warn_missing_scope=False)
        return backend, DjPost.objects.filter(author_id=1)

    def row_ids(self, query):
        return sorted(query.values_list("id", flat=True))

    def tenant_schema(self, *, batching):
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
                published = info.context["published_only"]
                return qs.filter(is_published=True) if published else qs

        @orm.type(DjUser)
        class UT:
            id: auto
            name: auto

            @strawberry.field
            def posts(self) -> list[PT]:
                return DjPost.objects.filter(author=self).order_by("id")  # type: ignore[return-value]

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def tenant_expectations(self):
        published = {"Hello World", "GraphQL Guide", "Rust Adventures"}
        return [
            (True, published),
            (False, published | {"Draft Post"}),
        ]

    def execute_with_context(self, schema, query, published_only):
        return schema.execute_sync(
            query, context_value={"published_only": published_only}
        )

    def execute(self, schema, query):
        return schema.execute_sync(query)
