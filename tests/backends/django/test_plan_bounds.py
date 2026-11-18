from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async

from strawberry_orm import StrawberryORM
from tests.abstract.plan_bounds import AbstractTestPlanBounds
from tests.backends.django.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


class TestPlanBounds(AbstractTestPlanBounds):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, variables):
        return await sync_to_async(
            lambda: schema.execute_sync(query, variable_values=variables),
            thread_sensitive=True,
        )()

    @staticmethod
    def watch_plans(backend, monkeypatch, sink):
        relation_lookups = backend._relation_lookups

        def watched(*args, **kwargs):
            select_related, prefetch_related = relation_lookups(*args, **kwargs)
            sink.append(len(select_related) + len(prefetch_related))
            return select_related, prefetch_related

        monkeypatch.setattr(backend, "_relation_lookups", watched)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
