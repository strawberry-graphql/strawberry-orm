from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.schema_tags import AbstractTestSchemaTags
from tests.backends.tortoise.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    """The models, once ``Tortoise.init`` has wired their relations."""
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class TestSchemaTags(AbstractTestSchemaTags):
    async def run(self, schema, query):
        return await schema.execute(query)
