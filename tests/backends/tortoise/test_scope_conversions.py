"""A row scope means the same thing wherever it is applied (Tortoise)."""

from types import SimpleNamespace

import pytest
from tortoise.expressions import Q
from tortoise.functions import Count

from strawberry_orm import StrawberryORM
from tests.abstract.scope_conversions import AbstractTestScopeConversions
from tests.backends.tortoise.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


class TestScopeConversions(AbstractTestScopeConversions):
    DENY = {
        "zero_limit": lambda qs: qs.limit(0),
        "zero_limit_after_offset": lambda qs: qs.offset(1).limit(0),
        "empty_in": lambda qs: qs.filter(id__in=[]),
        "contradiction": lambda qs: qs.filter(Q(title="a") & Q(title="b")),
    }
    NARROWING = {
        "published": lambda qs: qs.filter(is_published=True),
        "commented": lambda qs: qs.annotate(n=Count("comments")).filter(n__gte=1),
        "tagged": lambda qs: qs.filter(tags__id__not_isnull=True).distinct(),
    }
    BOUNDED = {
        "limit": lambda qs: qs.order_by("id").limit(2),
        "offset": lambda qs: qs.order_by("id").offset(1),
    }

    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await schema.execute(query, context_value={})

    @staticmethod
    def read(obj, name):
        return list(getattr(obj, name))

    @staticmethod
    async def rows(info, model):
        return list(await model.all())

    @staticmethod
    def first_ten(model):
        return model.all().order_by("id").limit(10)

    @staticmethod
    async def nodes(info, model, ids):
        found = {row.pk: row for row in await model.filter(id__in=ids)}
        return [found.get(pk) for pk in ids]
