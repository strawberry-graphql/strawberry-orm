"""A row scope means the same thing wherever it is applied (Django)."""

from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db.models import Count, Q

from strawberry_orm import StrawberryORM
from tests.abstract.scope_conversions import AbstractTestScopeConversions
from tests.backends.django.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


class TestScopeConversions(AbstractTestScopeConversions):
    DENY = {
        "none": lambda qs: qs.none(),
        "zero_slice": lambda qs: qs[:0],
        "empty_in": lambda qs: qs.filter(id__in=[]),
        "contradiction": lambda qs: qs.filter(Q(title="a") & Q(title="b")),
    }
    NARROWING = {
        "published": lambda qs: qs.filter(is_published=True),
        "commented": lambda qs: qs.annotate(n=Count("comments")).filter(n__gte=1),
        "tagged": lambda qs: qs.filter(tags__isnull=False).distinct(),
    }
    BOUNDED = {
        "limit": lambda qs: qs.order_by("id")[:2],
        "offset": lambda qs: qs.order_by("id")[1:],
    }
    # Django filters no queryset once it is sliced, so no scope narrows the
    # rows of a resolver that sliced its own.
    UNSUPPORTED = frozenset({"resolver_limit", "resolver_limit_groups"})

    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query):
        return await sync_to_async(
            lambda: schema.execute_sync(query, context_value={}),
            thread_sensitive=True,
        )()

    @staticmethod
    def read(obj, name):
        return list(getattr(obj, name).all())

    @staticmethod
    def rows(info, model):
        return list(model.objects.all())

    first_ten = None

    @staticmethod
    def nodes(info, model, ids):
        found = model.objects.in_bulk(ids)
        return [found.get(pk) for pk in ids]
