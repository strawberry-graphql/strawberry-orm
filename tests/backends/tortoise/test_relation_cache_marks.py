from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.relation_cache_marks import AbstractTestRelationCacheMarks
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post)


class TestRelationCacheMarks(AbstractTestRelationCacheMarks):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await schema.execute(query)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
