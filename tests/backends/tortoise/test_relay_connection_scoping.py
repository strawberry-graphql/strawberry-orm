from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.relay_connection_scoping import AbstractTestRelayConnectionScoping
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post)


class TestRelayConnectionScoping(AbstractTestRelayConnectionScoping):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, execution):
        return await schema.execute(query)

    @staticmethod
    def published_only(query):
        return query.filter(is_published=True)

    async def load_posts(self):
        return list(await self.every_post())

    @staticmethod
    def every_post():
        return Post.all().order_by("id")
