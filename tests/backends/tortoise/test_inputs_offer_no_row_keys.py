from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from strawberry_orm import AbstractRepo
from tests.abstract.inputs_offer_no_row_keys import (
    ME,
    THEM,
    AbstractTestACreateOnlyInserts,
    AbstractTestALinkKeepsItsRow,
    AbstractTestAnUpdateKeepsItsRow,
    AbstractTestInputsOfferNoRowKeys,
    KeyedDb,
    Shape,
)
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class WkBinder(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.IntField(default=0)

    class Meta:
        table = "wk_binder"


class WkStamped(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    owner = fields.IntField(default=0)

    class Meta:
        abstract = True


class WkTag(WkStamped):
    binder = fields.ForeignKeyField(
        "wk_row_keys.WkBinder",
        null=True,
        related_name="tags",
        on_delete=fields.SET_NULL,
    )

    class Meta:
        table = "wk_tag"


class WkBadge(Model):
    binder = fields.OneToOneField(
        "wk_row_keys.WkBinder",
        primary_key=True,
        related_name="badge",
        on_delete=fields.CASCADE,
    )
    title = fields.CharField(max_length=50)
    owner = fields.IntField(default=0)

    class Meta:
        table = "wk_badge"


class WkSlot(Model):
    binder = fields.ForeignKeyField(
        "wk_row_keys.WkBinder",
        primary_key=True,
        related_name="slots",
        on_delete=fields.CASCADE,
    )
    title = fields.CharField(max_length=50)
    owner = fields.IntField(default=0)

    class Meta:
        table = "wk_slot"


class WkPin(Model):
    id = fields.IntField(primary_key=True)
    tag = fields.ForeignKeyField(
        "wk_row_keys.WkTag", null=True, related_name=False, on_delete=fields.SET_NULL
    )
    badge = fields.ForeignKeyField(
        "wk_row_keys.WkBadge",
        null=True,
        related_name=False,
        on_delete=fields.SET_NULL,
    )

    class Meta:
        table = "wk_pin"


SHAPES = {
    "abstract_base": Shape(WkTag, {"id"}, ("id",), WkTag, relation="tags", link="tag"),
    "one_to_one_key": Shape(
        WkBadge,
        {"binder", "binder_id"},
        ("binder_id",),
        WkBadge,
        victim_key="binder_id",
        link="badge",
    ),
}


@dataclass
class TortoiseKeyedDb(KeyedDb, TortoiseWriteDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.filter(owner=ME)

    async def rows(self, model: type) -> list[Any]:
        if model is WkSlot:
            return await model.all().order_by("binder_id")
        return await super().rows(model)


@pytest_asyncio.fixture
async def keyed():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"wk_row_keys": [__name__]})
    await Tortoise.generate_schemas()
    for pk in (1, 2, 3):
        await WkBinder.create(id=pk, owner=THEM)
    yield TortoiseKeyedDb(
        SimpleNamespace(Binder=WkBinder, Pin=WkPin, Slot=WkSlot),
        async_hooks=True,
        shapes=SHAPES,
    )
    await Tortoise.close_connections()


@pytest.fixture(params=sorted(SHAPES))
def shape(request):
    return request.param


class TestInputsOfferNoRowKeys(AbstractTestInputsOfferNoRowKeys):
    pass


class TestACreateOnlyInserts(AbstractTestACreateOnlyInserts):
    pass


class TestAnUpdateKeepsItsRow(AbstractTestAnUpdateKeepsItsRow):
    pass


class TestALinkKeepsItsRow(AbstractTestALinkKeepsItsRow):
    @pytest.fixture(autouse=True)
    def reads_by_key_column(self, monkeypatch):
        """An app's repo reading rows by their key column: Tortoise cannot
        filter a row keyed by a foreign key by ``pk``."""

        async def _get_async(repo, model, pk, info):
            rows = repo.scope_query(model.all(), info)
            return await rows.filter(**{model._meta.pk_attr: pk}).first()

        monkeypatch.setattr(AbstractRepo, "_get_async", _get_async)
