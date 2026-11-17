from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.optimizer_repeated_fragments import AbstractTestRepeatedFragments
from tests.backends.tortoise.models import Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User, Post=Post, Tag=Tag)


class TestRepeatedFragments(AbstractTestRepeatedFragments):
    @pytest.fixture(autouse=True)
    def _seed(self, orm, seed):
        self.backend = orm.backend

    async def run(self, schema, query):
        with self.backend.query_probe(None) as probe:
            result = await schema.execute(query)
        self.query_count = probe.count
        return result
