"""Multi-table children read through their parent as rows of the parent."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import pytest_asyncio
from django.db import connection, models

from strawberry_orm import UpsertNotFoundError
from tests.abstract.write_path_subclass_rows import (
    AbstractTestSubclassRowsReadAsTheBase,
    recorder,
)
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


class WaFolder(models.Model):
    class Meta:
        app_label = "testapp"
        db_table = "wa_folder"


class WaDoc(models.Model):
    title = models.CharField(max_length=50)
    folder = models.ForeignKey(
        WaFolder, null=True, on_delete=models.CASCADE, related_name="docs"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wa_doc"


class WaSealedDoc(WaDoc):
    note = models.CharField(max_length=50, default="")

    class Meta:
        app_label = "testapp"
        db_table = "wa_sealed_doc"


class WaRestrictedDoc(WaSealedDoc):
    class Meta:
        app_label = "testapp"
        db_table = "wa_restricted_doc"


class WaPublicDoc(WaDoc):
    class Meta:
        app_label = "testapp"
        db_table = "wa_public_doc"


class WaDraftDoc(WaDoc):
    class Meta:
        app_label = "testapp"
        proxy = True


class WaPin(models.Model):
    label = models.CharField(max_length=50)
    doc = models.ForeignKey(
        WaDoc, null=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wa_pin"


class WaNote(models.Model):
    title = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "wa_note"


class WaSealedNote(WaNote):
    base = models.OneToOneField(
        WaNote, parent_link=True, primary_key=True, on_delete=models.CASCADE
    )

    class Meta:
        app_label = "testapp"
        db_table = "wa_sealed_note"


MODELS = SimpleNamespace(
    Folder=WaFolder,
    Doc=WaDoc,
    SealedDoc=WaSealedDoc,
    RestrictedDoc=WaRestrictedDoc,
    PublicDoc=WaPublicDoc,
    Pin=WaPin,
)


@dataclass
class SubclassWriteDb(DjangoWriteDb):
    async def also_a(self, model: type, pk: int) -> None:
        def add() -> None:
            row = model(**{model._meta.pk.attname: pk})
            row.save_base(raw=True, force_insert=True)

        await _on_db_thread(add)


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (*vars(MODELS).values(), WaNote, WaSealedNote):
            if model._meta.db_table not in existing:
                editor.create_model(model)


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request, setup_tables):
    await _on_db_thread(_create_tables)
    return SubclassWriteDb(MODELS, async_hooks=False, sync=request.param == "sync")


class TestSubclassRowsReadAsTheBase(AbstractTestSubclassRowsReadAsTheBase):
    pass


async def test_a_proxy_row_is_written_by_the_proxys_repo_only_by_name(writes):
    """Nothing in the database says a row is a proxy's, so a write through
    the base asks the base's repo, and only one naming the proxy asks it."""
    await writes.create(WaDoc, id=1, title="plain")
    calls = []
    orm = writes.new_orm(
        repos={
            WaDoc: recorder(writes, calls, "doc", None),
            WaDraftDoc: recorder(writes, calls, "draft", []),
        }
    )
    by_proxy = orm.mutations.upsert_node_input(
        model=WaDraftDoc,
        project={
            "_meta": {
                "update": ["title"],
                "upsert": {"where": ["id"], "onMatch": "UPDATE"},
            }
        },
        name="WaDraftDocById",
    )
    tests = AbstractTestSubclassRowsReadAsTheBase
    with pytest.raises(UpsertNotFoundError):
        await writes.upsert(orm, tests.edit(by_proxy, "pwned", id="1"))
    by_base = tests.upsert_by(orm, writes, "id")
    await writes.upsert(orm, tests.edit(by_base, "edited", id="1"))
    assert calls == [("doc", "can_update", 1)]
    assert (await tests.titles(writes))[1] == "edited"


async def test_a_parent_link_named_by_the_app_holds_its_rows_only(writes):
    """``WaSealedNote`` reaches its parent's key through ``base``, not the
    ``wanote_ptr`` Django would name the link."""
    await writes.create(WaNote, id=1, title="plain")
    await writes.create(WaSealedNote, id=2, title="sealed")
    calls = []
    orm = writes.new_orm(
        repos={
            WaNote: recorder(writes, calls, "note", None),
            WaSealedNote: recorder(writes, calls, "sealed", []),
        }
    )
    by_id, by_title = (
        orm.mutations.upsert_node_input(
            model=WaNote,
            project={
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": [field], "onMatch": "UPDATE"},
                }
            },
            name=f"WaNoteBy{field.title()}",
        )
        for field in ("id", "title")
    )
    edit = AbstractTestSubclassRowsReadAsTheBase.edit
    await writes.upsert(orm, edit(by_id, "edited", id="1"))
    await writes.upsert(orm, edit(by_title, "renamed", title="edited"))
    with pytest.raises(UpsertNotFoundError):
        await writes.upsert(orm, edit(by_id, "pwned", id="2"))
    assert calls == [("note", "can_update", 1), ("note", "can_update", 1)]
    notes = await writes.rows(WaNote)
    assert [note.title for note in notes] == ["renamed", "sealed"]
