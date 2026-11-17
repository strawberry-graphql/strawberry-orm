"""Files keyed by text SQLite compares with ``COLLATE NOCASE``, and the errors
writing them raises."""

from __future__ import annotations

from types import SimpleNamespace

import pytest_asyncio
from django.db import connection, models

from tests.abstract.write_path_error_names import AbstractTestErrorsNameTheClientsType
from tests.abstract.write_path_stored_key import AbstractTestAuthorizeIsToldTheStoredKey
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class WaShelf(models.Model):
    label = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "wa_shelf"


class WaFile(models.Model):
    id = models.CharField(max_length=20, primary_key=True, db_collation="NOCASE")
    title = models.CharField(max_length=50)
    shelf = models.ForeignKey(
        WaShelf, null=True, on_delete=models.SET_NULL, related_name="files"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wa_file"


class WaTag(models.Model):
    label = models.CharField(max_length=50)
    file = models.ForeignKey(
        WaFile, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wa_tag"


MODELS = SimpleNamespace(Shelf=WaShelf, File=WaFile, Tag=WaTag)


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in vars(MODELS).values():
            if model._meta.db_table not in existing:
                editor.create_model(model)


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request, setup_tables):
    await _on_db_thread(_create_tables)
    return DjangoWriteDb(MODELS, async_hooks=False, sync=request.param == "sync")


class TestAuthorizeIsToldTheStoredKey(AbstractTestAuthorizeIsToldTheStoredKey):
    pass


class TestErrorsNameTheClientsType(AbstractTestErrorsNameTheClientsType):
    pass
