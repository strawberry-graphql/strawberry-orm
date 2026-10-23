from types import SimpleNamespace

import pytest
import strawberry
from asgiref.sync import sync_to_async
from django.db import connection

from strawberry_orm import StrawberryORM
from tests.abstract.hint_paths import AbstractTestHintPaths
from tests.backends.django.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def lenient_orm():
    return StrawberryORM.for_django(
        lazy_resolution="off", warn_missing_scope=False, strict_hints=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment, Tag=Tag)


class TestHintPaths(AbstractTestHintPaths):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        def materialized_posts(info: strawberry.Info):
            return list(Post.objects.all())

        self.materialized_posts = materialized_posts

    async def run(self, schema, query):
        def _run():
            statements: list[str] = []

            def count(execute, sql, params, many, context):
                statements.append(sql)
                return execute(sql, params, many, context)

            # Connections are per thread, so the wrapper goes on the one the
            # query runs in.
            with connection.execute_wrapper(count):
                result = schema.execute_sync(query)
            return result, len(statements)

        result, self.query_count = await sync_to_async(_run, thread_sensitive=True)()
        return result

    @staticmethod
    def read(obj, name):
        field = obj._meta.get_field(name)
        if field.one_to_many or field.many_to_many:
            return list(getattr(obj, name).all())
        # A hidden row on a non-nullable key raises RelatedObjectDoesNotExist,
        # which is an AttributeError.
        return getattr(obj, name, None)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
