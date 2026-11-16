from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.batching_parent_identity import AbstractTestBatchingParentIdentity
from tests.backends.django.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class TestBatchingParentIdentity(AbstractTestBatchingParentIdentity):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await sync_to_async(
            lambda: schema.execute_sync(query), thread_sensitive=True
        )()

    @staticmethod
    def comments_on(post):
        return Comment.objects.filter(post_id=post.id).order_by("id")
