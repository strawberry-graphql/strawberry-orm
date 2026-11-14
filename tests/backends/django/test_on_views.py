from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connection

from strawberry_orm import StrawberryORM
from tests.abstract.on_views import (
    AbstractTestASelfReadFieldIsChecked,
    AbstractTestOnViews,
)
from tests.backends.django.models import Post, Tag, User


def _orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def orm():
    return _orm()


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


class _Django:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    fresh_orm = staticmethod(_orm)

    async def run(self, schema, query):
        def _run():
            statements: list[str] = []

            def count(execute, sql, params, many, context):
                statements.append(sql)
                return execute(sql, params, many, context)

            with connection.execute_wrapper(count):
                result = schema.execute_sync(query)
            return result, len(statements)

        result, self.query_count = await sync_to_async(_run, thread_sensitive=True)()
        return result

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})

    @staticmethod
    def newest_first(qs, model):
        return qs.order_by("-pk")

    async def add_authors(self, count):
        def add():
            for number in range(1, count + 1):
                author = User.objects.create(name=f"Author {number}", email="")
                Post.objects.create(
                    title=f"Book {number}", body="", is_published=True, author=author
                )
                Post.objects.create(title=f"Draft {number}", body="", author=author)

        await sync_to_async(add, thread_sensitive=True)()

    @staticmethod
    def own_posts(user, info):
        return list(Post.objects.filter(author=user).order_by("pk"))


class TestOnViews(_Django, AbstractTestOnViews):
    view_statements = 3


class TestASelfReadFieldIsChecked(_Django, AbstractTestASelfReadFieldIsChecked):
    pass
