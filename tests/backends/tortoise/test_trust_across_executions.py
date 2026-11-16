"""Tortoise refuses an eager ``on=`` view (see ``test_on.py``), so the view
case does not apply here."""

from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.trust_across_executions import AbstractTestTrustAcrossExecutions
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post)


class TestTrustAcrossExecutions(AbstractTestTrustAcrossExecutions):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, context):
        return await schema.execute(query, context_value=context)

    async def subscribe(self, schema, query, context):
        events = await schema.subscribe(query, context_value=context)
        return [answer async for answer in events]

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def all_posts():
        return Post.all().order_by("id")

    @staticmethod
    def posts_of(user):
        return Post.filter(author_id=user.id).order_by("id")

    async def load_posts(self):
        return list(await Post.all().order_by("id"))

    async def load_users(self):
        return list(await User.all().order_by("id"))
