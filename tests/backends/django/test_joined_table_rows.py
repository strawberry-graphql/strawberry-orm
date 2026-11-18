"""Multi-table children narrowed in SQL the library writes (Django).

Django joins a child's parent tables wherever it reads the child, and a
relation holds every row whose key points at the parent, so row 4, parent 2's,
is the one parent 1's ``rows`` leaves out. A child's key is always its own
parent link, as a SQLAlchemy subclass keyed apart from its base is; nothing
maps more than one model to a table.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from django.db import connection, models

from tests.abstract.joined_table_rows import AbstractTestJoinedTableRows, Shape
from tests.backends.django.test_write_path_authorization import (
    DjangoWriteDb,
    _on_db_thread,
)


def _owner(to: type) -> models.ForeignKey:
    return models.ForeignKey(
        to, null=True, on_delete=models.SET_NULL, related_name="rows"
    )


class JtTaskBoard(models.Model):
    class Meta:
        app_label = "testapp"
        db_table = "jt_task_board"


class JtItem(models.Model):
    title = models.CharField(max_length=50)
    color = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "jt_item"


class JtTask(JtItem):
    done = models.BooleanField(default=False)
    owner = _owner(JtTaskBoard)

    class Meta:
        app_label = "testapp"
        db_table = "jt_task"


class JtStep(models.Model):
    title = models.CharField(max_length=50)
    color = models.CharField(max_length=50)
    done = models.BooleanField(default=False)
    owner = _owner(JtTask)

    class Meta:
        app_label = "testapp"
        db_table = "jt_step"


class JtDocBoard(models.Model):
    class Meta:
        app_label = "testapp"
        db_table = "jt_doc_board"


class JtDoc(models.Model):
    title = models.CharField(max_length=50)
    color = models.CharField(max_length=50)
    owner = _owner(JtDocBoard)

    class Meta:
        app_label = "testapp"
        db_table = "jt_doc"


class JtNote(JtDoc):
    done = models.BooleanField(default=False)

    class Meta:
        app_label = "testapp"
        db_table = "jt_note"


class JtPlainBoard(models.Model):
    class Meta:
        app_label = "testapp"
        db_table = "jt_plain_board"


class JtPlain(models.Model):
    title = models.CharField(max_length=50)
    color = models.CharField(max_length=50)
    done = models.BooleanField(default=False)
    owner = _owner(JtPlainBoard)

    class Meta:
        app_label = "testapp"
        db_table = "jt_plain"


SHAPES = {
    "joined-own-column": Shape(
        JtTaskBoard, JtTask, "rows", 4, lambda q: q.filter(done=False)
    ),
    "joined-base-column": Shape(
        JtTaskBoard, JtTask, "rows", 4, lambda q: q.exclude(title="two")
    ),
    "joined-parent": Shape(
        JtTask,
        JtStep,
        "rows",
        4,
        lambda q: q.filter(done=False),
        parent_values={"title": "parent", "color": "none"},
    ),
    "base-of-joined-rows": Shape(
        JtDocBoard, JtDoc, "rows", 4, lambda q: q.exclude(title="two"), made=JtNote
    ),
    "plain": Shape(JtPlainBoard, JtPlain, "rows", 4, lambda q: q.filter(done=False)),
}

MODELS = (
    JtTaskBoard,
    JtItem,
    JtTask,
    JtStep,
    JtDocBoard,
    JtDoc,
    JtNote,
    JtPlainBoard,
    JtPlain,
)


def _create_tables() -> None:
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in MODELS:
            if model._meta.db_table not in existing:
                editor.create_model(model)


@pytest.fixture(params=list(SHAPES))
def shape(request):
    return SHAPES[request.param]


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request, setup_tables):
    await _on_db_thread(_create_tables)
    return DjangoWriteDb(None, async_hooks=False, sync=request.param == "sync")


class TestJoinedTableRows(AbstractTestJoinedTableRows):
    pass
