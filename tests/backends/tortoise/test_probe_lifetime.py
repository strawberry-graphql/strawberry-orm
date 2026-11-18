from types import SimpleNamespace

import pytest
import pytest_asyncio

from strawberry_orm import StrawberryORM
from tests.abstract.probe_lifetime import AbstractTestProbeLifetime
from tests.backends.tortoise.models import Post


@pytest.fixture
def make_orm():
    return StrawberryORM.for_tortoise


@pytest.fixture
def models(tortoise_db):
    return SimpleNamespace(Post=Post)


@pytest_asyncio.fixture
async def run(seed):
    async def _run(schema, query, context):
        return await schema.execute(query, context_value=context)

    return _run


@pytest.fixture
def hooks(tortoise_db):
    from tortoise import connections

    def _hooks():
        client = connections.get("default")
        return (client.execute_query, client.execute_query_dict)

    return _hooks


@pytest_asyncio.fixture
async def many_posts(seed):
    await Post.bulk_create(
        [Post(id=i, title=f"p{i}", body="", author_id=1) for i in range(10, 1210)]
    )


class TestProbeLifetime(AbstractTestProbeLifetime):
    pass
