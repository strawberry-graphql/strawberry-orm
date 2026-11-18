from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.probe_lifetime import AbstractTestProbeLifetime
from tests.backends.django.models import Post


@pytest.fixture
def make_orm():
    return StrawberryORM.for_django


@pytest.fixture
def models():
    return SimpleNamespace(Post=Post)


@pytest.fixture
def run(seed):
    async def _run(schema, query, context):
        return await schema.execute(query, context_value=context)

    return _run


@pytest.fixture
def hooks():
    from django.db import connections

    def _hooks():
        return [list(each.execute_wrappers) for each in connections.all()]

    return _hooks


@pytest.fixture
def many_posts(seed):
    Post.objects.bulk_create(
        [Post(id=i, title=f"p{i}", body="", author_id=1) for i in range(10, 1210)]
    )


class TestProbeLifetime(AbstractTestProbeLifetime):
    pass
