"""Tortoise refuses an eager ``on=`` view (see ``test_on.py``), so the view
case does not apply here."""

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.batching_sibling_types import AbstractTestBatchingSiblingTypes
from tests.backends.tortoise.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


@contextmanager
def _record_queries():
    from tortoise import connections

    client = connections.get("default")
    statements: list[str] = []
    originals = {}

    def _wrap(original):
        async def counting(sql, *args, **kwargs):
            statements.append(sql)
            return await original(sql, *args, **kwargs)

        return counting

    for name in ("execute_query", "execute_query_dict"):
        originals[name] = getattr(client, name)
        setattr(client, name, _wrap(originals[name]))
    try:
        yield statements
    finally:
        for name, original in originals.items():
            setattr(client, name, original)


class TestBatchingSiblingTypes(AbstractTestBatchingSiblingTypes):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, variables):
        with _record_queries() as statements:
            result = await schema.execute(query, variable_values=variables)
        self.query_count = len(statements)
        return result

    @staticmethod
    def published_only(qs):
        return qs.filter(is_published=True)

    @staticmethod
    def posts_of(user):
        return Post.filter(author_id=user.id).order_by("id")

    @staticmethod
    def comments_on(post):
        return Comment.filter(post_id=post.id).order_by("id")

    async def load_users(self):
        return list(await User.all().order_by("id"))

    async def load_posts(self):
        return list(await Post.all().order_by("id"))
