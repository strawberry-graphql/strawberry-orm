from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.to_query_filtered_fields import (
    AbstractTestToQueryFilteredFields,
    to_query,
)
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=policy(),
        to_query=to_query,
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestToQueryFilteredFields(AbstractTestToQueryFilteredFields):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await schema.execute(query)

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def all_posts():
        return Post.objects.all()

    async def load_posts(self):
        return await sync_to_async(
            lambda: list(Post.objects.order_by("id")), thread_sensitive=True
        )()
