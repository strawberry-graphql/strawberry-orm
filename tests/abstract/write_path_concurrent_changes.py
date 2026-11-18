"""A write saves the change it decided on, and nothing it only read.

``orm.apply_upsert()`` and ``orm.apply_ref_list()`` read each row they write,
ask ``authorize`` and the repos about the change, and then save it. Another
request can change or delete the row in between. A save that writes every
column puts back what was read in each column the write never set, scope
columns among them; and a save that matches no row must not insert one, which
would bring a deleted row back without ``can_create`` being asked.

Each cell of the matrix below races one write, reaching its row one way, with
one change to that row: made by the repo's ``can_update``, ``can_link``,
``can_unlink`` or ``can_delete`` the first time one is asked about the row, so
after the read and inside the write's own transaction. Every column the write
did not set keeps what the change left in it, and those it set hold what it
set; a row the change deleted stays deleted, and a write that set a column of
it is refused, rolling the delete back with it.

Each backend supplies a ``writes`` fixture: a :class:`RaceDb` over its ``Cw``
models, in every mode it writes in. An owner has docs; a doc has notes, at
most one badge, and an ``edited_at`` the ORM stamps on each save; a sealed
doc is a doc with a seal, a multi-table child where the backend has them.
"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import AbstractRepo
from strawberry_orm.backends._base import WRITE_REFUSED
from tests.abstract.write_path_authorization import WriteDb, item, member

MODELS = ("Owner", "Doc", "Note", "Badge", "SealedDoc")


@dataclass
class RaceDb(WriteDb):
    """A :class:`WriteDb` that can change rows under a running write."""

    #: Whether a repo overrides ``_save_async`` and ``_delete_async`` rather
    #: than their sync counterparts.
    saves_async = False

    def change(self, model: type, pk: int, **values: Any) -> Any:
        """Set *values*, by attribute, on the *model* row *pk*, past the ORM's objects.

        Returns an awaitable where the mode's hooks may be async.
        """
        raise NotImplementedError

    def remove(self, model: type, pk: int) -> Any:
        """Delete the *model* row *pk*, as :meth:`change` writes."""
        raise NotImplementedError

    async def find(self, model: type, pk: int) -> Any:
        """The *model* row *pk* as the database holds it now, or ``None``."""
        raise NotImplementedError

    async def save(self, repo: Any, row: Any) -> None:
        """Save *row* through *repo*'s ``_save``, as an app's own code would."""
        await repo._save_async(row, self.info)


async def seed(writes: RaceDb) -> SimpleNamespace:
    """Ann's docs Draft and Third, and sealed doc 4; Bob's doc Other.

    Draft has notes 1 and 2 and badge 1; Other has note 3.
    """
    m = writes.m
    ann = await writes.create(m.Owner, id=1, name="Ann", tier=1)
    bob = await writes.create(m.Owner, id=2, name="Bob", tier=2)
    draft = await writes.create(m.Doc, id=1, title="Draft", owner=ann)
    other = await writes.create(m.Doc, id=2, title="Other", owner=bob)
    await writes.create(m.Doc, id=3, title="Third", owner=ann)
    await writes.create(m.SealedDoc, id=4, title="Sealed", owner=ann, seal="wax")
    await writes.create(m.Note, id=1, text="first", doc=draft)
    await writes.create(m.Note, id=2, text="second", doc=draft)
    await writes.create(m.Note, id=3, text="elsewhere", doc=other)
    await writes.create(m.Badge, id=1, label="gold", doc=draft)
    return SimpleNamespace(draft=draft, other=other)


async def state(
    writes: RaceDb, model: type, pk: int, columns: tuple[str, ...]
) -> dict[str, Any] | None:
    """*columns* of the *model* row *pk* as the database holds them, or ``None``."""
    row = await writes.find(model, pk)
    return None if row is None else {column: getattr(row, column) for column in columns}


# -- The race --------------------------------------------------------------------


@dataclass
class Race:
    """One change to the *model* row *pk*, made the first time a check asks about it."""

    writes: RaceDb
    model: type
    pk: int
    #: ``"none"``, ``"deleted"``, or ``"changed"``, which sets *values*.
    event: str
    values: dict[str, Any] = field(default_factory=dict)
    fired: bool = False

    def at(self, row: Any) -> Any:
        if self.fired or not isinstance(row, self.model) or row.id != self.pk:
            return None
        self.fired = True
        if self.event == "deleted":
            return self.writes.remove(self.model, self.pk)
        if self.event == "changed":
            return self.writes.change(self.model, self.pk, **self.values)
        return None


def _allowing(pending: Any) -> Any:
    """``True``, once *pending*, the race's statement, has run."""
    if not inspect.isawaitable(pending):
        return True

    async def allow() -> bool:
        await pending
        return True

    return allow()


def racing_repo(race: Race) -> type:
    class RacingRepo(AbstractRepo):
        def can_update(self, instance, data, info):
            return _allowing(race.at(instance))

        def can_link(self, parent, field, instance, info):
            return _allowing(race.at(instance))

        def can_unlink(self, parent, field, instance, info):
            return _allowing(race.at(instance))

        def can_delete(self, instance, info):
            return _allowing(race.at(instance))

    return RacingRepo


def racing_orm(writes: RaceDb, race: Race, repo: type | None = None) -> Any:
    repo = repo or racing_repo(race)
    return writes.new_orm(repos={getattr(writes.m, name): repo for name in MODELS})


# -- The writes ------------------------------------------------------------------


@strawberry.input
class NoteUpdate:
    id: strawberry.ID
    text: str | None = strawberry.UNSET


DOC = {
    "_meta": {"update": ["title"], "upsert": {"where": ["id"], "onMatch": "UPDATE"}},
    "owner": {"_meta": {"update": ["name"]}},
    "notes": {
        "_meta": {"update": ["text"], "upsert": {"where": ["id"], "onMatch": "UPDATE"}}
    },
}

BADGE = {
    "_meta": {"update": ["label"], "upsert": {"where": ["id"], "onMatch": "UPDATE"}},
    "doc": {"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
}

SEALED_DOC = {
    "_meta": {"update": ["seal"], "upsert": {"where": ["id"], "onMatch": "UPDATE"}}
}


def edit(upsert: type, pk: int, **update: Any) -> Any:
    """An *upsert* payload updating the row *pk* with *update*."""
    return upsert(
        where=member(upsert, "where")(id=str(pk)),
        update=member(upsert, "update")(**update),
    )


def doc_upsert(orm: Any, writes: RaceDb) -> type:
    return orm.mutations.upsert_node_input(
        model=writes.m.Doc, project=DOC, name="CwDocUpsert"
    )


def note_ref(orm: Any, writes: RaceDb) -> type:
    return orm.ref(writes.m.Note, update=NoteUpdate, unlink=True)


Write = Callable[[Any, RaceDb, SimpleNamespace], Awaitable[None]]


async def root_update(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    await writes.upsert(orm, edit(doc_upsert(orm, writes), 1, title="Mine"))


async def nested_update(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    upsert = doc_upsert(orm, writes)
    note = item(member(upsert, "update", "notes"), "update", id="1", text="Mine")
    await writes.upsert(orm, edit(upsert, 1, notes=[note]))


async def nested_update_of_nothing(
    orm: Any, writes: RaceDb, docs: SimpleNamespace
) -> None:
    upsert = doc_upsert(orm, writes)
    note = item(member(upsert, "update", "notes"), "update", id="1")
    await writes.upsert(orm, edit(upsert, 1, notes=[note]))


async def nested_upsert(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    upsert = doc_upsert(orm, writes)
    notes = member(upsert, "update", "notes")
    target = member(notes, "upsert")
    note = notes(
        upsert=target(
            where=member(target, "where")(id="1"),
            update=member(target, "update")(text="Mine"),
        )
    )
    await writes.upsert(orm, edit(upsert, 1, notes=[note]))


async def nested_parent(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    upsert = doc_upsert(orm, writes)
    note = item(member(upsert, "update", "notes"), "update", id="2", text="Mine")
    await writes.upsert(orm, edit(upsert, 1, notes=[note]))


async def link_update(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    upsert = doc_upsert(orm, writes)
    owner = member(upsert, "update", "owner")
    link = owner(update=member(owner, "update")(id="1", name="Mine"))
    await writes.upsert(orm, edit(upsert, 1, owner=link))


async def ref_list_update(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    ref = note_ref(orm, writes)
    await writes.refs(
        orm, docs.draft, "notes", [item(ref, "update", id="1", text="Mine")]
    )


async def ref_list_link(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    ref = note_ref(orm, writes)
    await writes.refs(orm, docs.draft, "notes", [item(ref, "update", id="3")])


async def ref_list_unlink(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    ref = note_ref(orm, writes)
    await writes.refs(orm, docs.draft, "notes", [item(ref, "unlink", id="1")])


async def one_to_one_move(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    upsert = orm.mutations.upsert_node_input(
        model=writes.m.Badge, project=BADGE, name="CwBadgeUpsert"
    )
    doc = member(upsert, "update", "doc")
    target = member(doc, "upsert")
    link = doc(upsert=target(where=member(target, "where")(id="2")))
    await writes.upsert(orm, edit(upsert, 1, doc=link))


async def subclass_update(orm: Any, writes: RaceDb, docs: SimpleNamespace) -> None:
    upsert = orm.mutations.upsert_node_input(
        model=writes.m.SealedDoc, project=SEALED_DOC, name="CwSealedDocUpsert"
    )
    await writes.upsert(orm, edit(upsert, 4, seal="mine"))


DOC_COLUMNS = ("title", "is_published", "owner_id")
NOTE_COLUMNS = ("text", "flagged", "doc_id")


@dataclass(frozen=True)
class Entry:
    """One way a write reaches the *model* row *pk*, and what it sets there."""

    name: str
    write: Write
    model: str
    pk: int
    columns: tuple[str, ...]
    sets: dict[str, Any]
    #: A change to columns the write does not set.
    other: dict[str, Any]
    #: A change to the columns it sets, or ``None`` where that is no cell.
    same: dict[str, Any] | None
    #: Whether the write sets a column of the row, so a deleted row refuses it.
    #: Otherwise the write may pass, as long as the row stays deleted.
    sets_a_column: bool = True

    def __repr__(self) -> str:
        return self.name


ENTRIES = [
    Entry(
        "root_update",
        root_update,
        "Doc",
        1,
        DOC_COLUMNS,
        {"title": "Mine"},
        {"owner_id": 2, "is_published": True},
        {"title": "Theirs"},
    ),
    Entry(
        "nested_update",
        nested_update,
        "Note",
        1,
        NOTE_COLUMNS,
        {"text": "Mine"},
        {"flagged": True, "doc_id": 2},
        {"text": "Theirs"},
    ),
    Entry(
        "nested_upsert",
        nested_upsert,
        "Note",
        1,
        NOTE_COLUMNS,
        {"text": "Mine"},
        {"flagged": True, "doc_id": 2},
        {"text": "Theirs"},
    ),
    Entry(
        "nested_update_of_nothing",
        nested_update_of_nothing,
        "Note",
        1,
        NOTE_COLUMNS,
        {},
        {"flagged": True, "text": "Theirs", "doc_id": 2},
        None,
        sets_a_column=False,
    ),
    # The doc above a nested write is saved too, with nothing of its own set.
    Entry(
        "nested_parent",
        nested_parent,
        "Doc",
        1,
        DOC_COLUMNS,
        {},
        {"title": "Theirs", "owner_id": 2, "is_published": True},
        None,
        sets_a_column=False,
    ),
    Entry(
        "link_update",
        link_update,
        "Owner",
        1,
        ("name", "tier"),
        {"name": "Mine"},
        {"tier": 9},
        {"name": "Theirs"},
    ),
    Entry(
        "ref_list_update",
        ref_list_update,
        "Note",
        1,
        NOTE_COLUMNS,
        {"text": "Mine"},
        {"flagged": True, "doc_id": 2},
        {"text": "Theirs"},
    ),
    # Django links and unlinks with a statement of its own, which matches no
    # row once the note is gone, where the other two refuse.
    Entry(
        "ref_list_link",
        ref_list_link,
        "Note",
        3,
        NOTE_COLUMNS,
        {"doc_id": 1},
        {"flagged": True, "text": "Theirs"},
        {"doc_id": 3},
        sets_a_column=False,
    ),
    # Django unlinks only from the doc it was asked about, where the other two
    # clear the key wherever it points by then, so a note moved meanwhile is
    # no cell.
    Entry(
        "ref_list_unlink",
        ref_list_unlink,
        "Note",
        1,
        NOTE_COLUMNS,
        {"doc_id": None},
        {"flagged": True, "text": "Theirs"},
        None,
        sets_a_column=False,
    ),
    Entry(
        "one_to_one_move",
        one_to_one_move,
        "Badge",
        1,
        ("label", "doc_id"),
        {"doc_id": 2},
        {"label": "Theirs"},
        {"doc_id": 3},
    ),
    Entry(
        "subclass_update",
        subclass_update,
        "SealedDoc",
        4,
        (*DOC_COLUMNS, "seal"),
        {"seal": "mine"},
        {"title": "Theirs", "owner_id": 2, "is_published": True},
        {"seal": "theirs"},
    ),
]

EVENTS = ("none", "deleted", "other_changed", "same_changed")

CELLS = [
    pytest.param(entry, event, id=f"{entry.name}-{event}")
    for entry in ENTRIES
    for event in EVENTS
    if event != "same_changed" or entry.same is not None
]


class Docs:
    @pytest_asyncio.fixture
    async def docs(self, writes):
        return await seed(writes)


class AbstractTestAWriteSavesOnlyWhatItDecided(Docs):
    @pytest.mark.parametrize(("entry", "event"), CELLS)
    async def test_a_change_after_the_read(self, writes, docs, entry, event):
        model = getattr(writes.m, entry.model)
        values = {"other_changed": entry.other, "same_changed": entry.same}
        race = Race(
            writes,
            model,
            entry.pk,
            "changed" if event in values else event,
            values.get(event) or {},
        )
        orm = racing_orm(writes, race)
        before = await state(writes, model, entry.pk, entry.columns)
        refused = None
        try:
            await entry.write(orm, writes, docs)
        except ValueError as exc:
            refused = exc
        after = await state(writes, model, entry.pk, entry.columns)

        where = f"{entry.name}, {event}"
        assert race.fired, f"{where}: no check was asked about the row"
        if event == "deleted":
            if refused is None:
                assert not entry.sets_a_column, (
                    f"{where}: a write setting a column of a deleted row passed, "
                    f"leaving {after}"
                )
                assert after is None, f"{where}: the deleted row came back as {after}"
            else:
                assert str(refused) == WRITE_REFUSED, f"{where}: refused with {refused}"
                assert after == before, f"{where}: the refused write left {after}"
            return
        assert refused is None, f"{where}: refused with {refused!r}"
        expected = {**before, **(values.get(event) or {}), **entry.sets}
        assert after == expected, f"{where}: expected {expected}, the row holds {after}"


# -- What else a write saves -----------------------------------------------------


def saving_repo(writes: RaceDb, base: type) -> type:
    """*base*, with a ``_save`` that publishes the doc before saving it as before."""
    if writes.saves_async:

        class AsyncSavingRepo(base):
            async def _save_async(self, instance, info):
                instance.is_published = True
                await super()._save_async(instance, info)

        return AsyncSavingRepo

    class SavingRepo(base):
        def _save(self, instance, info):
            instance.is_published = True
            super()._save(instance, info)

    return SavingRepo


def soft_deleting_repo(writes: RaceDb, base: type) -> type:
    """*base*, with a ``_delete`` that flags the note and saves it instead."""
    if writes.saves_async:

        class AsyncSoftDeletingRepo(base):
            async def _delete_async(self, instance, info):
                instance.flagged = True
                await self._save_async(instance, info)

        return AsyncSoftDeletingRepo

    class SoftDeletingRepo(base):
        def _delete(self, instance, info):
            instance.flagged = True
            self._save(instance, info)

    return SoftDeletingRepo


class AbstractTestWhatAWriteSaves(Docs):
    """Doc 1's owner is changed to Bob after each write below reads it."""

    @pytest.fixture
    def race(self, writes):
        return Race(writes, writes.m.Doc, 1, "changed", {"owner_id": 2})

    async def doc(self, writes: RaceDb) -> dict[str, Any] | None:
        return await state(writes, writes.m.Doc, 1, DOC_COLUMNS)

    async def test_a_column_set_to_what_was_read_is_written(self, writes, docs):
        race = Race(writes, writes.m.Doc, 1, "changed", {"title": "Theirs"})
        orm = racing_orm(writes, race)
        await writes.upsert(orm, edit(doc_upsert(orm, writes), 1, title="Draft"))
        doc = await self.doc(writes)
        assert doc is not None, "doc 1 is gone"
        assert doc["title"] == "Draft", f"the write's title was lost: {doc}"

    async def test_a_value_on_before_update_returns_is_written(
        self, writes, docs, race
    ):
        class Unpublishing(racing_repo(race)):
            def on_before_update(self, instance, data, info):
                return {**data, "is_published": False}

        race.values["is_published"] = True
        orm = racing_orm(writes, race, Unpublishing)
        await root_update(orm, writes, docs)
        doc = await self.doc(writes)
        expected = {"title": "Mine", "is_published": False, "owner_id": 2}
        assert doc == expected, f"expected {expected}, doc 1 holds {doc}"

    async def test_a_column_a_hook_sets_on_the_row_is_written(self, writes, docs, race):
        class Publishing(racing_repo(race)):
            def on_before_update(self, instance, data, info):
                instance.is_published = True
                return data

        orm = racing_orm(writes, race, Publishing)
        await root_update(orm, writes, docs)
        doc = await self.doc(writes)
        expected = {"title": "Mine", "is_published": True, "owner_id": 2}
        assert doc == expected, f"expected {expected}, doc 1 holds {doc}"

    async def test_a_save_override_writes_what_it_sets(self, writes, docs, race):
        orm = racing_orm(writes, race, saving_repo(writes, racing_repo(race)))
        await root_update(orm, writes, docs)
        doc = await self.doc(writes)
        expected = {"title": "Mine", "is_published": True, "owner_id": 2}
        assert doc == expected, f"expected {expected}, doc 1 holds {doc}"

    async def test_a_soft_delete_through_save_writes_only_its_flag(self, writes, docs):
        race = Race(writes, writes.m.Note, 1, "changed", {"text": "Theirs"})
        orm = racing_orm(writes, race, soft_deleting_repo(writes, racing_repo(race)))
        ref = orm.ref(writes.m.Note, delete=True)
        await writes.refs(orm, docs.draft, "notes", [item(ref, "delete", id="1")])
        note = await state(writes, writes.m.Note, 1, NOTE_COLUMNS)
        expected = {"text": "Theirs", "flagged": True, "doc_id": 1}
        assert note == expected, f"expected {expected}, note 1 holds {note}"

    async def test_a_column_the_orm_stamps_on_each_save_is_stamped(
        self, writes, docs, race
    ):
        stamped = (await writes.find(writes.m.Doc, 1)).edited_at
        await root_update(racing_orm(writes, race), writes, docs)
        restamped = (await writes.find(writes.m.Doc, 1)).edited_at
        assert restamped > stamped, f"edited_at stayed {stamped}, now {restamped}"

    async def test_a_row_the_app_read_itself_is_saved_by_the_orm(self, writes, docs):
        orm = writes.new_orm(repos={writes.m.Doc: AbstractRepo})
        row = await writes.find(writes.m.Doc, 1)
        row.title = "Mine"
        await writes.save(orm.backend.get_repo(writes.m.Doc), row)
        doc = await self.doc(writes)
        expected = {"title": "Mine", "is_published": False, "owner_id": 1}
        assert doc == expected, f"expected {expected}, doc 1 holds {doc}"
