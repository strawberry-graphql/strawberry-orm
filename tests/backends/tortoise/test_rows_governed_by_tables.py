"""Tortoise gives a model inheriting another a table of its own.

Its base's fields are copied into it, so a query of the subclass filters them
as its own: a repo or row scope the subclass takes from its base reads the
subclass's table, while the base's table holds none of its rows.
"""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.rows_governed_by_tables import (
    AbstractTestRowsGovernedByTables,
    Shape,
)
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb

APP = "gt_governed"


class GcDoc(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    class Meta:
        table = "gc_doc"


class GcSub(GcDoc):
    class Meta:
        table = "gc_sub"


class GaDoc(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)

    class Meta:
        abstract = True


class GaSub(GaDoc):
    class Meta:
        table = "ga_sub"


class GcShelf(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, default="")
    docs = fields.ManyToManyField(f"{APP}.GcSub", related_name="shelves")

    class Meta:
        table = "gc_shelf"


class GcPin(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50, default="")
    doc = fields.ForeignKeyField(
        f"{APP}.GcSub", null=True, related_name="pins", on_delete=fields.SET_NULL
    )

    class Meta:
        table = "gc_pin"


class GaShelf(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, default="")
    docs = fields.ManyToManyField(f"{APP}.GaSub", related_name="shelves")

    class Meta:
        table = "ga_shelf"


class GaPin(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50, default="")
    doc = fields.ForeignKeyField(
        f"{APP}.GaSub", null=True, related_name="pins", on_delete=fields.SET_NULL
    )

    class Meta:
        table = "ga_pin"


SHAPES = {
    "concrete base": (
        SimpleNamespace(Base=GcDoc, Sub=GcSub, Shelf=GcShelf, Pin=GcPin),
        Shape(False, True, apart=True),
    ),
    "abstract base": (
        SimpleNamespace(Base=GaDoc, Sub=GaSub, Shelf=GaShelf, Pin=GaPin),
        Shape(False, True),
    ),
}


@dataclass
class GovernedWriteDb(TortoiseWriteDb):
    shape: Shape | None = None

    def docs(self) -> Any:
        return self.m.Sub.all().order_by("id")

    def shelves(self) -> Any:
        return self.m.Shelf.all().order_by("id")

    async def read(self, query: Any) -> list[Any]:
        return list(await query)


@pytest_asyncio.fixture(params=list(SHAPES))
async def writes(request):
    shaped, shape = SHAPES[request.param]
    await Tortoise.init(db_url="sqlite://:memory:", modules={APP: [__name__]})
    await Tortoise.generate_schemas()
    yield GovernedWriteDb(shaped, async_hooks=True, shape=shape)
    await Tortoise.close_connections()


class TestRowsGovernedByTables(AbstractTestRowsGovernedByTables):
    pass
