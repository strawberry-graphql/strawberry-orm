from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.plan_bounds import AbstractTestPlanBounds
from tests.backends.tortoise.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


def _loads(level):
    return sum(1 + _loads(load.children) for load in level.values())


class TestPlanBounds(AbstractTestPlanBounds):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    async def run(self, schema, query, variables):
        return await schema.execute(query, variable_values=variables)

    @staticmethod
    def watch_plans(backend, monkeypatch, sink):
        relation_prefetches = backend._relation_prefetches

        def watched(*args, **kwargs):
            plan = relation_prefetches(*args, **kwargs)
            sink.append(_loads(plan))
            return plan

        monkeypatch.setattr(backend, "_relation_prefetches", watched)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.exclude(**{field: value})
