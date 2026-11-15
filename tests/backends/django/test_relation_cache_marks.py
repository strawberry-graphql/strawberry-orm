from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.relation_cache_marks import AbstractTestRelationCacheMarks
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestRelationCacheMarks(AbstractTestRelationCacheMarks):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await sync_to_async(
            lambda: schema.execute_sync(query), thread_sensitive=True
        )()

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
