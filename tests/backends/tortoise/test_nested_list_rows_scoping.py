from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.nested_list_rows_scoping import AbstractTestNestedListRowsScoping
from tests.abstract.payload import policy
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(
        lazy_resolution="off", warn_missing_scope=False, payload=policy()
    )


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post)


class TestNestedListRowsScoping(AbstractTestNestedListRowsScoping):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await schema.execute(query)

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    async def load_posts(self):
        return list(await Post.all().order_by("id"))
