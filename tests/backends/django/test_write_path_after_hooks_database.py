"""Django creates foreign keys deferred on SQLite and PostgreSQL.

A key naming no row is refused only as the outermost transaction commits:
after ``orm.apply_ref_list()``'s hooks had run, and inside a transaction of
the app's own (``ATOMIC_REQUESTS``), after the resolver had returned. The
write's foreign keys are now checked before its ``on_after_*`` hooks run.
"""

from unittest.mock import ANY, patch

import pytest
import pytest_asyncio
import strawberry
from django.db import connection, connections, models, transaction

from strawberry_orm import AbstractRepo
from tests.abstract.write_path_after_hooks_database import (
    DROP_REFUSING_TRIGGERS,
    REFUSED,
    AbstractTestAfterHooksWaitForTheDatabase,
    AbstractTestAfterHooksWaitForTheKeys,
    announcing_repo,
    refusing_triggers,
)
from tests.abstract.write_path_authorization import item
from tests.backends.django.test_write_path_authorization import (  # noqa: F401
    MODELS,
    DjangoWriteDb,
    _on_db_thread,
    writes,
)


def _execute(statements):
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)


@pytest_asyncio.fixture
async def refusing(writes, blog):  # noqa: F811
    await _on_db_thread(lambda: _execute(refusing_triggers("post_tags")))
    yield
    await _on_db_thread(lambda: _execute(DROP_REFUSING_TRIGGERS))


@pytest.fixture
def keys_enforced():
    """Django turns SQLite's foreign keys on, and defers them to the commit."""


class TestAfterHooksWaitForTheDatabase(AbstractTestAfterHooksWaitForTheDatabase):
    pass


class TestAfterHooksWaitForTheKeys(AbstractTestAfterHooksWaitForTheKeys):
    pass


# -- Deferred keys, wherever the write runs ------------------------------------------


class WbBoard(models.Model):
    name = models.CharField(max_length=20)

    class Meta:
        app_label = "testapp"
        db_table = "wb_board"


class WbCard(models.Model):
    title = models.CharField(max_length=20)
    board = models.ForeignKey(WbBoard, on_delete=models.CASCADE, related_name="cards")
    copied_from = models.ForeignKey(
        WbBoard, null=True, on_delete=models.DO_NOTHING, related_name="+"
    )

    class Meta:
        app_label = "testapp"
        db_table = "wb_card"


class WbStamp(models.Model):
    """Left in place when its card is deleted, still holding the card's key."""

    card = models.ForeignKey(WbCard, on_delete=models.DO_NOTHING, related_name="+")

    class Meta:
        app_label = "testapp"
        db_table = "wb_stamp"


@strawberry.input
class CardCreate:
    title: str
    copied_from_id: int | None = None


@pytest.fixture
def sync_writes(setup_tables):
    return DjangoWriteDb(MODELS, async_hooks=False, sync=True)


@pytest.fixture
def board(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (WbBoard, WbCard, WbStamp):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    board = WbBoard.objects.create(id=1, name="b")
    WbStamp.objects.create(card=WbCard.objects.create(id=1, title="old", board=board))
    yield board
    WbStamp.objects.all().delete()
    WbCard.objects.all().delete()
    WbBoard.objects.all().delete()


def _cards(sync_writes, repo):
    orm = sync_writes.new_orm(repos={WbCard: repo})
    return orm, orm.ref(WbCard, create=CardCreate, delete=True)


async def test_a_key_naming_no_row_is_refused_inside_an_outer_transaction(
    sync_writes, board
):
    outbox = []
    orm, ref = _cards(sync_writes, announcing_repo(outbox))
    refs = [item(ref, "create", title="fresh", copied_from_id=999)]

    def in_a_request():
        with transaction.atomic(), pytest.raises(ValueError, match=REFUSED):
            orm.apply_ref_list(board, "cards", refs, sync_writes.info)

    await _on_db_thread(in_a_request)
    assert outbox == []
    assert await names_of_cards() == ["old"]


async def test_a_delete_leaving_a_key_behind_sends_nothing(sync_writes, board):
    outbox = []
    orm, ref = _cards(sync_writes, announcing_repo(outbox))
    refs = [item(ref, "create", title="fresh"), item(ref, "delete", id="1")]
    with pytest.raises(ValueError, match=REFUSED):
        await sync_writes.refs(orm, board, "cards", refs)
    assert outbox == []
    assert await names_of_cards() == ["old"]


async def test_only_the_tables_written_are_checked(sync_writes, board):
    outbox = []
    orm, ref = _cards(sync_writes, announcing_repo(outbox))
    wrapper = _wrapper()
    with patch.object(
        wrapper,
        "check_constraints",
        autospec=True,
        side_effect=wrapper.check_constraints,
    ) as checked:
        await sync_writes.refs(orm, board, "cards", [item(ref, "create", title="new")])
    checked.assert_called_once_with(ANY, ["wb_card"])
    assert outbox == [("created", "new")]
    assert await names_of_cards() == ["old", "new"]


async def test_a_write_without_after_hooks_checks_nothing(sync_writes, board):
    orm, ref = _cards(sync_writes, AbstractRepo)
    with patch.object(_wrapper(), "check_constraints") as checked:
        await sync_writes.refs(orm, board, "cards", [item(ref, "create", title="new")])
    checked.assert_not_called()
    assert await names_of_cards() == ["old", "new"]


def _wrapper() -> type:
    """The default database's wrapper class, shared by every thread's connection."""
    return type(connections["default"])


async def names_of_cards():
    return await _on_db_thread(
        lambda: list(WbCard.objects.order_by("pk").values_list("title", flat=True))
    )
