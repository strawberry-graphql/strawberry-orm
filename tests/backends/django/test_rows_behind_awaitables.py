from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.rows_behind_awaitables import AbstractTestRowsBehindAwaitables
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(
        lazy_resolution="off", warn_missing_scope=False, payload=policy()
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRowsBehindAwaitables(AbstractTestRowsBehindAwaitables):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def context(self):
        return {}

    async def run(self, schema, query, context):
        return await schema.execute(query, context_value=context)

    async def run_sync(self, schema, query, context):
        return await sync_to_async(
            lambda: schema.execute_sync(query, context_value=context),
            thread_sensitive=True,
        )()

    async def subscribe(self, schema, query, context):
        events = await schema.subscribe(query, context_value=context)
        return [answer async for answer in events]

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
