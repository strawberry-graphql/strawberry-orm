"""Differential batching tests for the Django backend."""

import pytest
import strawberry
from django.db import connection
from django.db.models import Q
from django.test.utils import CaptureQueriesContext

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_differential import (
    AbstractTestBatchingDifferential,
    build_user_post_types,
)
from tests.backends.django.models import Comment as DjComment
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


def _resolver_for(shape):
    if shape == "plain":
        return lambda parent: DjPost.objects.filter(author=parent)
    if shape == "filtered":
        return lambda parent: DjPost.objects.filter(author=parent, is_published=True)
    if shape == "branching":
        return lambda parent: (
            DjPost.objects.filter(author=parent)
            if parent.name == "Alice"
            else DjPost.objects.filter(author=parent, is_published=True)
        )
    if shape == "per_parent_value":
        return lambda parent: DjPost.objects.filter(
            author=parent, created_at__gte=parent.created_at
        )
    if shape == "ordered":
        return lambda parent: DjPost.objects.filter(author=parent).order_by("-title")
    if shape == "excluded":
        return lambda parent: DjPost.objects.filter(author=parent).exclude(
            title="Draft Post"
        )
    if shape == "or_clause":
        return lambda parent: DjPost.objects.filter(
            Q(author=parent) | Q(title="nothing")
        )
    if shape == "negated":
        return lambda parent: DjPost.objects.filter(author=parent).exclude(
            Q(title="Draft Post") | Q(title="nothing")
        )
    if shape == "sliced":
        return lambda parent: DjPost.objects.filter(author=parent).order_by("id")[:1]
    if shape == "materialized":
        return lambda parent: list(DjPost.objects.filter(author=parent))
    raise AssertionError(f"unknown shape {shape}")  # pragma: no cover


@pytest.fixture
def extra_users():
    def _add(count):
        for index in range(count):
            user = DjUser.objects.create(
                name=f"Extra{index}", email=f"extra{index}@example.com"
            )
            DjPost.objects.create(
                title=f"Extra post {index}",
                body="body",
                is_published=True,
                author=user,
            )

    return _add


class TestBatchingDifferential(AbstractTestBatchingDifferential):
    def build_schema(self, shape, *, batching):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )
        UT = build_user_post_types(orm, DjUser, DjPost, _resolver_for(shape))

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def random_schema(self, spec, *, batching):
        def resolver(parent):
            qs = DjPost.objects.filter(author=parent)
            if spec["published_only"] or (
                spec["branch_on_name"] and parent.name != "Alice"
            ):
                qs = qs.filter(is_published=True)
            if spec["exclude_draft"]:
                qs = qs.exclude(title="Draft Post")
            if spec["extra_predicate"] == "id_gt_0":
                qs = qs.filter(id__gt=0)
            elif spec["extra_predicate"] == "title_not_null":
                qs = qs.exclude(title=None)
            if spec["order"]:
                qs = qs.order_by(spec["order"])
            return qs

        return self._schema_from(resolver, batching=batching)

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
                return DjComment.objects.filter(post=self)  # type: ignore[return-value]

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

    def _schema_from(self, resolver, *, batching):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batching,
        )
        UT = build_user_post_types(orm, DjUser, DjPost, resolver)

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()

        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query)

    def count_queries(self, schema, query):
        with CaptureQueriesContext(connection) as ctx:
            result = schema.execute_sync(query)
        assert result.errors is None, result.errors
        return len(ctx)
