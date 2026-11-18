from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.rows_without_extensions import AbstractTestRowsWithoutExtensions
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRowsWithoutExtensions(AbstractTestRowsWithoutExtensions):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, context):
        return await schema.execute(query, context_value=context)

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def all_posts():
        return Post.objects.order_by("id")

    async def load_posts(self):
        return await sync_to_async(
            lambda: list(Post.objects.order_by("id")), thread_sensitive=True
        )()

    async def load_users(self):
        return await sync_to_async(
            lambda: list(User.objects.order_by("id")), thread_sensitive=True
        )()
