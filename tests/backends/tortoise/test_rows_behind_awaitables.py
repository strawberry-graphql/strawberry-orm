import asyncio
from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.rows_behind_awaitables import AbstractTestRowsBehindAwaitables
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(
        lazy_resolution="off", warn_missing_scope=False, payload=policy()
    )


@pytest.fixture
def models(tortoise_db):
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
        # On the event loop's own thread, execute_sync would find it running.
        return await asyncio.to_thread(
            schema.execute_sync, query, context_value=context
        )

    async def subscribe(self, schema, query, context):
        events = await schema.subscribe(query, context_value=context)
        return [answer async for answer in events]

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def all_posts():
        return Post.all().order_by("id")

    async def load_posts(self):
        return list(await Post.all().order_by("id"))

    async def load_users(self):
        return list(await User.all().order_by("id"))
