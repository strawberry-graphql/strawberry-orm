from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM
from tests.abstract.casts_per_execution import AbstractTestCastsPerExecution


class DtMember(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    email = fields.CharField(max_length=100)

    class Meta:
        table = "dt_member"


@pytest_asyncio.fixture
async def cached():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"dt_casts": [__name__]})
    await Tortoise.generate_schemas()
    await DtMember.create(id=1, name="ada", email="ada@corp.example")
    yield list(await DtMember.all())
    await Tortoise.close_connections()


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(Member=DtMember)


class TestCastsPerExecution(AbstractTestCastsPerExecution):
    asynchronous = True

    async def run(self, schema, query, context):
        return await schema.execute(query, context_value=context)
