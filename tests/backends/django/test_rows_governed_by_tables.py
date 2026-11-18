"""Each way Django lets a model inherit another: a proxy, a child, an abstract base."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from asgiref.sync import sync_to_async
from django.db import connection, models

from tests.abstract.rows_governed_by_tables import (
    AbstractTestRowsGovernedByTables,
    Shape,
)
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class GpDoc(models.Model):
    title = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "gp_doc"


class GpSub(GpDoc):
    class Meta:
        app_label = "testapp"
        proxy = True


class GmDoc(models.Model):
    title = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "gm_doc"


class GmSub(GmDoc):
    class Meta:
        app_label = "testapp"
        db_table = "gm_sub"


class GaDoc(models.Model):
    title = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        abstract = True


class GaSub(GaDoc):
    class Meta:
        app_label = "testapp"
        db_table = "ga_sub"


class GpShelf(models.Model):
    name = models.CharField(max_length=50, default="")
    docs = models.ManyToManyField(GpSub, related_name="shelves")

    class Meta:
        app_label = "testapp"
        db_table = "gp_shelf"


class GpPin(models.Model):
    label = models.CharField(max_length=50, default="")
    doc = models.ForeignKey(GpSub, null=True, on_delete=models.SET_NULL)

    class Meta:
        app_label = "testapp"
        db_table = "gp_pin"


class GmShelf(models.Model):
    name = models.CharField(max_length=50, default="")
    docs = models.ManyToManyField(GmSub, related_name="shelves")

    class Meta:
        app_label = "testapp"
        db_table = "gm_shelf"


class GmPin(models.Model):
    label = models.CharField(max_length=50, default="")
    doc = models.ForeignKey(GmSub, null=True, on_delete=models.SET_NULL)

    class Meta:
        app_label = "testapp"
        db_table = "gm_pin"


class GaShelf(models.Model):
    name = models.CharField(max_length=50, default="")
    docs = models.ManyToManyField(GaSub, related_name="shelves")

    class Meta:
        app_label = "testapp"
        db_table = "ga_shelf"


class GaPin(models.Model):
    label = models.CharField(max_length=50, default="")
    doc = models.ForeignKey(GaSub, null=True, on_delete=models.SET_NULL)

    class Meta:
        app_label = "testapp"
        db_table = "ga_pin"


SHAPES = {
    "proxy": (
        SimpleNamespace(Base=GpDoc, Sub=GpSub, Shelf=GpShelf, Pin=GpPin),
        Shape(True, True),
    ),
    "multi-table": (
        SimpleNamespace(Base=GmDoc, Sub=GmSub, Shelf=GmShelf, Pin=GmPin),
        Shape(True, True),
    ),
    "abstract": (
        SimpleNamespace(Base=GaDoc, Sub=GaSub, Shelf=GaShelf, Pin=GaPin),
        Shape(False, True, typed=False),
    ),
}

TABLES = (GpDoc, GmDoc, GmSub, GaSub, GpShelf, GpPin, GmShelf, GmPin, GaShelf, GaPin)


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in TABLES:
            if model._meta.db_table not in existing:
                editor.create_model(model)


@dataclass
class GovernedWriteDb(DjangoWriteDb):
    shape: Shape | None = None

    def docs(self) -> Any:
        return self.m.Sub.objects.order_by("pk")

    def shelves(self) -> Any:
        return self.m.Shelf.objects.order_by("pk")

    def read(self, query: Any) -> Any:
        if self.sync:
            return list(query)
        return sync_to_async(list)(query)


@pytest_asyncio.fixture(
    params=[(shape, mode) for shape in SHAPES for mode in ("sync", "async")],
    ids=lambda param: f"{param[0]}-{param[1]}",
)
async def writes(request, setup_tables):
    name, mode = request.param
    shaped, shape = SHAPES[name]
    await _on_db_thread(_create_tables)
    return GovernedWriteDb(shaped, async_hooks=False, sync=mode == "sync", shape=shape)


class TestRowsGovernedByTables(AbstractTestRowsGovernedByTables):
    pass
