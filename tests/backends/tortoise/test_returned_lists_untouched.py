from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.returned_lists_untouched import (
    AbstractTestReturnedListsUntouched,
)
from tests.backends.tortoise.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post)


class TestReturnedListsUntouched(AbstractTestReturnedListsUntouched):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await schema.execute(query)

    async def load_users(self):
        return list(await User.all().order_by("id").prefetch_related("posts"))

    @staticmethod
    def collection(user):
        return user.posts.related_objects

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    async def written(self):
        return dict(await Post.all().values_list("title", "author_id"))
