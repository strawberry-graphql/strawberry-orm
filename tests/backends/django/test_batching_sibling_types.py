from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connection

from strawberry_orm import StrawberryORM
from tests.abstract.batching_sibling_types import (
    AbstractTestBatchingSiblingTypes,
    AbstractTestOnViewSiblingTypes,
)
from tests.backends.django.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class _Django:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, variables):
        def _run():
            statements: list[str] = []

            def count(execute, sql, params, many, context):
                statements.append(sql)
                return execute(sql, params, many, context)

            with connection.execute_wrapper(count):
                result = schema.execute_sync(query, variable_values=variables)
            return result, len(statements)

        result, self.query_count = await sync_to_async(_run, thread_sensitive=True)()
        return result

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def posts_of(user):
        return Post.objects.filter(author_id=user.id).order_by("id")

    @staticmethod
    def comments_on(post):
        return Comment.objects.filter(post_id=post.id).order_by("id")

    async def load_users(self):
        return await sync_to_async(
            lambda: list(User.objects.order_by("id")), thread_sensitive=True
        )()

    async def load_posts(self):
        return await sync_to_async(
            lambda: list(Post.objects.order_by("id")), thread_sensitive=True
        )()


class TestBatchingSiblingTypes(_Django, AbstractTestBatchingSiblingTypes):
    pass


class TestOnViewSiblingTypes(_Django, AbstractTestOnViewSiblingTypes):
    pass
