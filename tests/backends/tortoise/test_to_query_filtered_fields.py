from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.to_query_filtered_fields import (
    AbstractTestToQueryFilteredFields,
    to_query,
)
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=policy(),
        to_query=to_query,
    )


@pytest.fixture
def models(tortoise_db):
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
        return Post.all()

    async def load_posts(self):
        return list(await Post.all().order_by("id"))
