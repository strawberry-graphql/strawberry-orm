"""Files keyed by text SQLite compares with ``COLLATE NOCASE``, and the errors
writing them raises.

Tortoise declares no collation, so the tables are made by hand.
"""

from types import SimpleNamespace

import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.write_path_error_names import AbstractTestErrorsNameTheClientsType
from tests.abstract.write_path_stored_key import AbstractTestAuthorizeIsToldTheStoredKey
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class WaShelf(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)

    files: fields.ReverseRelation["WaFile"]

    class Meta:
        table = "wa_shelf"


class WaFile(Model):
    id = fields.CharField(max_length=20, primary_key=True)
    title = fields.CharField(max_length=50)
    shelf = fields.ForeignKeyField(
        "wa_stored_key.WaShelf",
        null=True,
        on_delete=fields.SET_NULL,
        related_name="files",
    )

    class Meta:
        table = "wa_file"


class WaTag(Model):
    id = fields.IntField(primary_key=True)
    label = fields.CharField(max_length=50)
    file = fields.ForeignKeyField(
        "wa_stored_key.WaFile", null=True, on_delete=fields.SET_NULL, related_name=False
    )

    class Meta:
        table = "wa_tag"


SCHEMA = """
CREATE TABLE wa_shelf (id INTEGER PRIMARY KEY, label VARCHAR(50) NOT NULL);
CREATE TABLE wa_file (
    id VARCHAR(20) NOT NULL PRIMARY KEY COLLATE NOCASE,
    title VARCHAR(50) NOT NULL,
    shelf_id INT REFERENCES wa_shelf (id) ON DELETE SET NULL
);
CREATE TABLE wa_tag (
    id INTEGER PRIMARY KEY,
    label VARCHAR(50) NOT NULL,
    file_id VARCHAR(20) REFERENCES wa_file (id) ON DELETE SET NULL
);
"""


@pytest_asyncio.fixture
async def writes():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"wa_stored_key": [__name__]}
    )
    await Tortoise.get_connection("default").execute_script(SCHEMA)
    yield TortoiseWriteDb(
        SimpleNamespace(Shelf=WaShelf, File=WaFile, Tag=WaTag), async_hooks=True
    )
    await Tortoise.close_connections()


class TestAuthorizeIsToldTheStoredKey(AbstractTestAuthorizeIsToldTheStoredKey):
    pass


class TestErrorsNameTheClientsType(AbstractTestErrorsNameTheClientsType):
    pass
