"""A Tortoise subclass of a concrete model is a table of its own.

Tortoise copies the base's fields into the subclass's table, so no row of the
subclass is a row of its base: a write through the base cannot reach one, and
there is no subclass for its repo to tell apart.
"""

from types import SimpleNamespace

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import UpsertNotFoundError
from tests.abstract.write_path_subclass_rows import (
    AbstractTestSubclassRowsReadAsTheBase,
    recorder,
)
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class WaDoc(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    class Meta:
        table = "wa_doc"


class WaSealedDoc(WaDoc):
    note = fields.CharField(max_length=50, default="")

    class Meta:
        table = "wa_sealed_doc"


@pytest_asyncio.fixture
async def writes():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"wa_subclass_rows": [__name__]}
    )
    await Tortoise.generate_schemas()
    yield TortoiseWriteDb(SimpleNamespace(Doc=WaDoc, SealedDoc=WaSealedDoc), True)
    await Tortoise.close_connections()


async def test_a_write_through_the_base_reaches_only_the_bases_table(writes):
    await writes.create(WaDoc, id=1, title="plain")
    await writes.create(WaSealedDoc, id=2, title="sealed")
    calls = []
    orm = writes.new_orm(
        repos={
            WaDoc: recorder(writes, calls, "doc", None),
            WaSealedDoc: recorder(writes, calls, "sealed", []),
        }
    )
    tests = AbstractTestSubclassRowsReadAsTheBase
    upsert = tests.upsert_by(orm, writes, "id")
    await writes.upsert(orm, tests.edit(upsert, "edited", id="1"))
    with pytest.raises(UpsertNotFoundError):
        await writes.upsert(orm, tests.edit(upsert, "pwned", id="2"))
    assert calls == [("doc", "can_update", 1)]
    assert [doc.title for doc in await writes.rows(WaDoc)] == ["edited"]
    assert [doc.title for doc in await writes.rows(WaSealedDoc)] == ["sealed"]
