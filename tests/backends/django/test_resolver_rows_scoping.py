from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.resolver_rows_scoping import AbstractTestResolverRowsScoping
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(
        lazy_resolution="off", warn_missing_scope=False, payload=policy()
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestResolverRowsScoping(AbstractTestResolverRowsScoping):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await sync_to_async(
            lambda: schema.execute_sync(query), thread_sensitive=True
        )()

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    async def load_posts(self):
        return await sync_to_async(
            lambda: list(Post.objects.order_by("id")), thread_sensitive=True
        )()
