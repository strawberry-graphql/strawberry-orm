from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.relay_connection_scoping import AbstractTestRelayConnectionScoping
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRelayConnectionScoping(AbstractTestRelayConnectionScoping):
    EXECUTIONS = ("sync", "async")

    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, execution):
        if execution == "async":
            return await schema.execute(query)
        return await sync_to_async(
            lambda: schema.execute_sync(query), thread_sensitive=True
        )()

    @staticmethod
    def published_only(query):
        return query.filter(is_published=True)

    async def load_posts(self):
        return await sync_to_async(
            lambda: list(self.every_post()), thread_sensitive=True
        )()

    @staticmethod
    def every_post():
        return Post.objects.order_by("id")
