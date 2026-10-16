from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.schema_tags import AbstractTestSchemaTags
from tests.backends.django.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class TestSchemaTags(AbstractTestSchemaTags):
    async def run(self, schema, query):
        return await sync_to_async(schema.execute_sync, thread_sensitive=True)(query)
