from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.write_path_identity_columns import (
    ME,
    AbstractTestACreateKeepsItsModel,
    AbstractTestALinkKeepsItsRowAndModel,
    AbstractTestAnUpdateKeepsItsRowAndModel,
    AbstractTestInputsOfferNoIdentityColumn,
    IdentityDb,
    Shape,
)
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class IcHolder(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.IntField(default=0)

    class Meta:
        table = "ic_holder"


def _holder(related_name: str) -> Any:
    return fields.ForeignKeyField(
        "ic_identity.IcHolder",
        null=True,
        related_name=related_name,
        on_delete=fields.SET_NULL,
    )


class IcNote(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    owner = fields.IntField(default=0)
    holder = _holder("notes")

    class Meta:
        table = "ic_note"


class IcPin(Model):
    id = fields.IntField(primary_key=True)
    note = fields.ForeignKeyField(
        "ic_identity.IcNote", null=True, related_name=False, on_delete=fields.SET_NULL
    )

    class Meta:
        table = "ic_pin"


# Tortoise keys a model by one field, and has no model inheritance to type
# its rows by. Nor does a link key a row: Tortoise drops ``primary_key`` from
# a foreign key, so the table it generates holds any number of rows a link
# "keys", and its queries cannot filter on that key.
SHAPES = {
    "key": Shape(
        IcNote,
        {"id"},
        mine={"id": 1},
        theirs={"id": 2},
        moves=[{"id": 2}, {"id": 50}],
        forges=[{"id": 2}],
        columns=("id",),
        own_id="1",
        relation="notes",
        link="note",
    ),
}


@dataclass
class TortoiseIdentityDb(IdentityDb, TortoiseWriteDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.filter(owner=ME)

    async def fetch(self, model: type, pk: Any) -> Any:
        return await model.get(pk=pk)

    async def rows(self, model: type) -> list[Any]:
        return await model.all()


@pytest_asyncio.fixture
async def identity():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"ic_identity": [__name__]})
    await Tortoise.generate_schemas()
    yield TortoiseIdentityDb(
        SimpleNamespace(Holder=IcHolder, Pin=IcPin), async_hooks=True, shapes=SHAPES
    )
    await Tortoise.close_connections()


@pytest.fixture(params=sorted(SHAPES))
def shape(request):
    return request.param


class TestInputsOfferNoIdentityColumn(AbstractTestInputsOfferNoIdentityColumn):
    pass


class TestAnUpdateKeepsItsRowAndModel(AbstractTestAnUpdateKeepsItsRowAndModel):
    pass


class TestACreateKeepsItsModel(AbstractTestACreateKeepsItsModel):
    pass


class TestALinkKeepsItsRowAndModel(AbstractTestALinkKeepsItsRowAndModel):
    pass
