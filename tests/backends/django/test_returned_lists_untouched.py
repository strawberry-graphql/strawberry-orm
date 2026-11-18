from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.returned_lists_untouched import (
    AbstractTestReturnedListsUntouched,
)
from tests.backends.django.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestReturnedListsUntouched(AbstractTestReturnedListsUntouched):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await sync_to_async(
            lambda: schema.execute_sync(query), thread_sensitive=True
        )()

    async def load_users(self):
        return await sync_to_async(
            lambda: list(User.objects.prefetch_related("posts").order_by("id")),
            thread_sensitive=True,
        )()

    @staticmethod
    def collection(user):
        return user._prefetched_objects_cache["posts"]._result_cache

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    async def written(self):
        return await sync_to_async(
            lambda: dict(Post.objects.values_list("title", "author_id")),
            thread_sensitive=True,
        )()
