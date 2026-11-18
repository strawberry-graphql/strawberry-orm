"""A write decides on the rows of the database it writes to."""

from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from tortoise import Tortoise, connections, fields
from tortoise.models import Model
from tortoise.utils import get_schema_sql

from tests.abstract.writes_read_where_they_write import (
    ME,
    AbstractTestAWriteReadsWhereItWrites,
    ReplicaDb,
)
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


class RwTag(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.CharField(max_length=50)
    name = fields.CharField(max_length=50)

    class Meta:
        table = "rw_tag"


class RwDoc(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.CharField(max_length=50)
    title = fields.CharField(max_length=50)
    tags = fields.ManyToManyField(
        "rw.RwTag",
        through="rw_tagging",
        forward_key="tag_id",
        backward_key="doc_id",
        related_name="docs",
    )

    class Meta:
        table = "rw_doc"


class RwNote(Model):
    id = fields.IntField(primary_key=True)
    owner = fields.CharField(max_length=50)
    text = fields.CharField(max_length=50)
    doc = fields.ForeignKeyField(
        "rw.RwDoc", null=True, on_delete=fields.SET_NULL, related_name="notes"
    )

    class Meta:
        table = "rw_note"


class RwGrant(Model):
    id = fields.IntField(primary_key=True)
    doc_id = fields.IntField()
    user = fields.CharField(max_length=50)

    class Meta:
        table = "rw_grant"


class RwTagging(Model):
    id = fields.IntField(primary_key=True)
    doc = fields.ForeignKeyField("rw.RwDoc", related_name="taggings")
    tag = fields.ForeignKeyField("rw.RwTag", related_name="taggings")
    owner = fields.CharField(max_length=50)

    class Meta:
        table = "rw_tagging"


MODELS = SimpleNamespace(
    Tag=RwTag, Doc=RwDoc, Note=RwNote, Grant=RwGrant, Tagging=RwTagging
)

CONNECTIONS = {"primary": "default", "replica": "replica"}


class ReplicaRouter:
    def db_for_read(self, model):
        return "replica"

    def db_for_write(self, model):
        return "default"


class TortoiseReplicaDb(TortoiseWriteDb, ReplicaDb):
    def owned(self, query: Any, model: type) -> Any:
        return query.filter(owner=ME)

    def granted(self, doc_id: int) -> Any:
        return RwGrant.filter(doc_id=doc_id, user=ME).exists()

    async def create_in(self, database: str, model: type, **values: Any) -> None:
        await model.create(using_db=connections.get(CONNECTIONS[database]), **values)

    async def primary_rows(self, model: type) -> list[Any]:
        return await model.all().using_db(connections.get("default")).order_by("id")

    async def fetch(self, model: type, pk: int) -> Any:
        return await model.get(pk=pk)


@pytest_asyncio.fixture
async def db():
    await Tortoise.init(
        config={
            "connections": {name: "sqlite://:memory:" for name in CONNECTIONS.values()},
            "apps": {"rw": {"models": [__name__], "default_connection": "default"}},
            "routers": [ReplicaRouter],
        }
    )
    await Tortoise.generate_schemas()
    schema = get_schema_sql(connections.get("default"), safe=True)
    await connections.get("replica").execute_script(schema)
    yield TortoiseReplicaDb(MODELS, async_hooks=True)
    await Tortoise.close_connections()


class TestAWriteReadsWhereItWrites(AbstractTestAWriteReadsWhereItWrites):
    pass
