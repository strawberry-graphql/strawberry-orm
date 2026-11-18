"""A row is told apart from every other by its whole primary key (Django)."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, models

from strawberry_orm import StrawberryORM
from tests.abstract.whole_row_keys import (
    AbstractTestReadsTellRowsApart,
    AbstractTestWritesTellRowsApart,
    KeyedRows,
)


def _model(prefix: str, called: str, **fields: Any) -> type:
    meta = type(
        "Meta",
        (),
        {"app_label": "testapp", "db_table": f"wr_{prefix}_{called}".lower()},
    )
    return type(
        f"Wr{prefix}{called}",
        (models.Model,),
        {"__module__": __name__, "Meta": meta, **fields},
    )


def _models(shape: str, prefix: str) -> SimpleNamespace:
    Order = _model(prefix, "Order")
    Tag = _model(prefix, "Tag", name=models.CharField(max_length=20))
    if shape == "one_column":
        keys: dict[str, Any] = {}
        note_keys = {"line_id": models.IntegerField()}
    else:
        first = (
            models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
            if shape == "foreign_key_and_column"
            else models.IntegerField()
        )
        keys = {
            "pk": models.CompositePrimaryKey("a", "b"),
            "a": first,
            "b": models.IntegerField(),
        }
        note_keys = {"line_a": models.IntegerField(), "line_b": models.IntegerField()}
    Line = _model(
        prefix,
        "Line",
        **keys,
        label=models.CharField(max_length=20),
        tag=models.ForeignKey(Tag, on_delete=models.CASCADE, related_name="lines"),
    )
    Note = _model(prefix, "Note", **note_keys, text=models.CharField(max_length=20))
    return SimpleNamespace(Order=Order, Tag=Tag, Line=Line, Note=Note)


SHAPES = {
    "two_columns": _models("two_columns", "Two"),
    "foreign_key_and_column": _models("foreign_key_and_column", "Linked"),
    "one_column": _models("one_column", "One"),
}


def _seed(m: SimpleNamespace, keys: dict[str, Any], first: str) -> None:
    for id in (1, 2):
        m.Order.objects.create(id=id)
    for id, name in enumerate(("plain", "hush", "other"), start=1):
        m.Tag.objects.create(id=id, name=name)
    for tag, label in enumerate(("shown", "secret", "other"), start=1):
        key = keys[label]
        named = {"id": key} if isinstance(key, int) else {first: key[0], "b": key[1]}
        m.Line.objects.create(**named, label=label, tag_id=tag)
    for id, (label, text) in enumerate((("shown", "first"), ("secret", "second")), 1):
        key = keys[label]
        if isinstance(key, int):
            m.Note.objects.create(id=id, line_id=key, text=text)
        else:
            m.Note.objects.create(id=id, line_a=key[0], line_b=key[1], text=text)


@dataclass
class DjangoKeyedRows(KeyedRows):
    async def run(self, schema: Any, query: str, variables: Any = None) -> Any:
        return await sync_to_async(
            lambda: schema.execute_sync(query, variable_values=variables),
            thread_sensitive=True,
        )()

    async def write(self, call: Any) -> Any:
        return await sync_to_async(call, thread_sensitive=True)()

    def orm(self, **kwargs: Any) -> Any:
        return StrawberryORM.for_django(
            lazy_resolution="off", warn_missing_scope=False, **kwargs
        )

    def hide_secret(self, query: Any) -> Any:
        return query.exclude(label="secret")

    def is_first(self, query: Any, value: bool) -> Any:
        if "id" in self.key_names:
            return query.exclude(id=2) if value else query.filter(id=2)
        return query.filter(b=1) if value else query.exclude(b=1)

    def notes_of(self, line: Any) -> Any:
        Note = self.m.Note
        if "id" in self.key_names:
            return Note.objects.filter(line_id=line.id).order_by("id")
        a, b = line.pk
        return Note.objects.filter(line_a=a, line_b=b).order_by("id")

    async def held(self) -> dict[str, Any]:
        rows = await sync_to_async(list, thread_sensitive=True)(
            self.m.Line.objects.all()
        )
        return {row.label: row for row in rows}

    async def table(self) -> dict[Any, str]:
        rows = await sync_to_async(list, thread_sensitive=True)(
            self.m.Line.objects.all()
        )
        return {row.pk: row.label for row in rows}


@pytest.fixture(params=list(SHAPES))
def keyed(request, setup_tables):
    shape = request.param
    m = SHAPES[shape]
    existing = set(connection.introspection.table_names())
    with connection.schema_editor() as editor:
        for model in (m.Order, m.Tag, m.Line, m.Note):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    if shape == "one_column":
        keys: dict[str, Any] = {"shown": 1, "secret": 2, "other": 3}
        ids = {label: str(key) for label, key in keys.items()}
        part_id, key_names, moved = None, {"id"}, ("id", 2)
    else:
        keys = {"shown": (1, 1), "secret": (1, 2), "other": (2, 1)}
        ids = {label: f"[{a}, {b}]" for label, (a, b) in keys.items()}
        part_id, moved = "1", ("b", 2)
        key_names = {"a", "b"} | (
            {"a_id"} if shape == "foreign_key_and_column" else set()
        )
    _seed(m, keys, "a_id" if "a_id" in key_names else "a")
    yield DjangoKeyedRows(
        m=m, keys=keys, ids=ids, part_id=part_id, key_names=key_names, moved=moved
    )
    for model in (m.Note, m.Line, m.Tag, m.Order):
        model.objects.all().delete()


class TestReadsTellRowsApart(AbstractTestReadsTellRowsApart):
    pass


class TestWritesTellRowsApart(AbstractTestWritesTellRowsApart):
    pass
