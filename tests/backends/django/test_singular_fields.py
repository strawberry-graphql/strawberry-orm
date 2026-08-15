"""Singular field tests for the Django backend."""

import strawberry

from tests.abstract.singular_fields import (
    AbstractTestSingularFields,
    build_query,
    build_types,
)
from tests.backends.django.models import Post as DjPost
from tests.backends.django.models import User as DjUser


class TestSingularFields(AbstractTestSingularFields):
    def single_post_schema(self, orm):
        PT = build_types(orm, DjUser, DjPost)

        def post(id: int) -> PT | None:
            return DjPost.objects.filter(pk=id)  # type: ignore[return-value]

        def posts() -> list[PT]:
            return DjPost.objects.all()  # type: ignore[return-value]

        Query = build_query(orm, PT, post, posts)
        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def execute(self, schema, query):
        return schema.execute_sync(query)
