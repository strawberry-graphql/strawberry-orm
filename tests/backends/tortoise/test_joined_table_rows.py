"""A Tortoise subclass of a concrete model is a table of its own.

Tortoise copies the base's fields into the subclass's table, so a subclass
reads one table as any model does, and the cases hold for it as they do for a
plain model. A relation holds every row whose key points at the parent, so row
4, parent 2's, is the one parent 1's ``rows`` leaves out.
"""

from typing import Any

import pytest
import pytest_asyncio
from tortoise import Tortoise, fields
from tortoise.models import Model

from tests.abstract.joined_table_rows import AbstractTestJoinedTableRows, Shape
from tests.backends.tortoise.test_write_path_authorization import TortoiseWriteDb


def _owner(to: str) -> Any:
    return fields.ForeignKeyField(
        f"jt_rows.{to}", related_name="rows", null=True, on_delete=fields.SET_NULL
    )


class JtTaskBoard(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "jt_task_board"


class JtItem(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    color = fields.CharField(max_length=50)

    class Meta:
        table = "jt_item"


class JtTask(JtItem):
    done = fields.BooleanField(default=False)
    owner = _owner("JtTaskBoard")

    class Meta:
        table = "jt_task"


class JtStep(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    color = fields.CharField(max_length=50)
    done = fields.BooleanField(default=False)
    owner = _owner("JtTask")

    class Meta:
        table = "jt_step"


class JtPlainBoard(Model):
    id = fields.IntField(primary_key=True)

    class Meta:
        table = "jt_plain_board"


class JtPlain(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    color = fields.CharField(max_length=50)
    done = fields.BooleanField(default=False)
    owner = _owner("JtPlainBoard")

    class Meta:
        table = "jt_plain"


SHAPES = {
    "subclass": Shape(JtTaskBoard, JtTask, "rows", 4, lambda q: q.filter(done=False)),
    "subclass-parent": Shape(
        JtTask,
        JtStep,
        "rows",
        4,
        lambda q: q.filter(done=False),
        parent_values={"title": "parent", "color": "none"},
    ),
    "plain": Shape(JtPlainBoard, JtPlain, "rows", 4, lambda q: q.filter(done=False)),
}


@pytest.fixture(params=list(SHAPES))
def shape(request):
    return SHAPES[request.param]


@pytest_asyncio.fixture
async def writes():
    await Tortoise.init(db_url="sqlite://:memory:", modules={"jt_rows": [__name__]})
    await Tortoise.generate_schemas()
    yield TortoiseWriteDb(None, async_hooks=True)
    await Tortoise.close_connections()


class TestJoinedTableRows(AbstractTestJoinedTableRows):
    pass
