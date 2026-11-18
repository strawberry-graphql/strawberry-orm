"""A write decides on the rows of the database it writes to.

An app may read from a replica: its router sends reads one way and writes
another. A write that looked its rows up there, or let its repos' scopes and
its hooks' own queries run there, would decide on rows as they were rather
than as they are: a row given away since would still be the caller's to
write, and a grant revoked since would still let the write through. While a
write decides, every read goes where the write goes.

Both databases hold Ann's doc 2, holding note 1; note 2, held by no doc; doc
1, titled "plan"; doc 3, which Ann's grant lets her update; and Ann's tag 1,
which a link row tags doc 2 with. In ``taken`` the primary has given doc 1,
both notes and the link row to Bob and revoked the grant, and the replica
has not caught up: Ann's writes of them change nothing on the primary. Nor
has the replica caught up with the link row: a write reading the links
there would find none to ask about. In ``given`` it is the other way round,
and Ann's writes land.

A backend provides a ``db`` fixture: a write harness of
:mod:`tests.abstract.write_path_authorization` that is also a
:class:`ReplicaDb`, whose app reads every model from the replica and writes
it to the primary, in every mode it writes in. Doc's ``tags`` go through
Tagging, a model of the app's.
"""

from contextlib import suppress
from typing import Any

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import AbstractRepo
from tests.abstract.write_path_authorization import item, member

ME = "ann"

#: Each write, as Ann makes it.
WRITES = (
    "upsert update",
    "upsert match",
    "nested update",
    "ref-list update",
    "ref-list delete",
    "ref-list link",
    "ref-list unlink",
    "hook read",
)


@strawberry.input
class RwNoteUpdate:
    id: strawberry.ID
    text: str | None = strawberry.UNSET


class ReplicaDb:
    """What a write harness adds to seed and read either database."""

    def owned(self, query: Any, model: type) -> Any:
        """*query* narrowed to the rows of *model* Ann owns."""
        raise NotImplementedError

    def granted(self, doc_id: int) -> Any:
        """Whether Ann holds a grant on doc *doc_id*, as the app's own query
        reads it, or an awaitable of that."""
        raise NotImplementedError

    async def create_in(self, database: str, model: type, **values: Any) -> None:
        """Create a row of *model* in *database*: ``primary`` or ``replica``."""
        raise NotImplementedError

    async def primary_rows(self, model: type) -> list[Any]:
        """Every row of *model* on the primary, by key."""
        raise NotImplementedError

    async def fetch(self, model: type, pk: int) -> Any:
        """Row *pk* of *model*, read as a resolver reads it: from the replica."""
        raise NotImplementedError


def repos(db: Any) -> dict[type, type]:
    class RwDocRepo(AbstractRepo):
        def scope_query(self, query, info):
            return db.owned(query, db.m.Doc)

        def can_update(self, instance, data, info):
            return db.granted(instance.id)

    class RwNoteRepo(AbstractRepo):
        def scope_query(self, query, info):
            return db.owned(query, db.m.Note)

    class RwTaggingRepo(AbstractRepo):
        def scope_query(self, query, info):
            return db.owned(query, db.m.Tagging)

    return {db.m.Doc: RwDocRepo, db.m.Note: RwNoteRepo, db.m.Tagging: RwTaggingRepo}


async def seed(db: Any, database: str, taken: bool) -> None:
    m = db.m
    owner = "bob" if taken else ME
    await db.create_in(database, m.Doc, id=1, owner=owner, title="plan")
    await db.create_in(database, m.Doc, id=2, owner=ME, title="mine")
    await db.create_in(database, m.Doc, id=3, owner=ME, title="shared")
    await db.create_in(database, m.Note, id=1, doc_id=2, owner=owner, text="note")
    await db.create_in(database, m.Note, id=2, doc_id=None, owner=owner, text="loose")
    for doc in (1, 2) if taken else (1, 2, 3):
        await db.create_in(database, m.Grant, id=doc, doc_id=doc, user=ME)
    await db.create_in(database, m.Tag, id=1, owner=ME, name="red")
    if database == "primary" or taken:
        await db.create_in(database, m.Tagging, id=1, doc_id=2, tag_id=1, owner=owner)


async def state(db: Any) -> tuple[list, list, list]:
    docs = [(row.id, row.owner, row.title) for row in await db.primary_rows(db.m.Doc)]
    notes = [
        (row.id, row.doc_id, row.owner, row.text)
        for row in await db.primary_rows(db.m.Note)
    ]
    taggings = [
        (row.id, row.doc_id, row.tag_id, row.owner)
        for row in await db.primary_rows(db.m.Tagging)
    ]
    return docs, notes, taggings


async def write(db: Any, orm: Any, entry: str) -> None:
    m = db.m
    if entry in ("upsert update", "upsert match", "hook read"):
        key = "title" if entry == "upsert match" else "id"
        upsert = orm.mutations.upsert_node_input(
            model=m.Doc,
            project={
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": [key], "onMatch": "UPDATE"},
                }
            },
            name=f"RwDocBy{key.title()}",
        )
        if key == "title":
            where = {"title": "plan"}
        else:
            where = {"id": "3" if entry == "hook read" else "1"}
        payload = upsert(
            where=member(upsert, "where")(**where),
            update=member(upsert, "update")(title="edited"),
        )
        await db.upsert(orm, payload)
    elif entry == "nested update":
        upsert = orm.mutations.upsert_node_input(
            model=m.Doc,
            project={
                "_meta": {"update": True, "upsert": {"where": ["id"]}},
                "notes": {"_meta": {"update": True}},
            },
            name="RwDocNotes",
        )
        notes = member(upsert, "update", "notes")
        payload = upsert(
            where=member(upsert, "where")(id="2"),
            update=member(upsert, "update")(
                notes=[item(notes, "update", id="1", text="edited")]
            ),
        )
        await db.upsert(orm, payload)
    elif entry == "ref-list unlink":
        ref = orm.ref(m.Tag, unlink=True)
        parent = await db.fetch(m.Doc, 2)
        await db.refs(orm, parent, "tags", [item(ref, "unlink", id="1")])
    else:
        ref = orm.ref(m.Note, update=RwNoteUpdate, delete=True)
        operation, pk = {
            "ref-list update": ("update", "1"),
            "ref-list delete": ("delete", "1"),
            "ref-list link": ("update", "2"),
        }[entry]
        values = {"text": "edited"} if operation == "update" else {}
        parent = await db.fetch(m.Doc, 2)
        await db.refs(orm, parent, "notes", [item(ref, operation, id=pk, **values)])


class AbstractTestAWriteReadsWhereItWrites:
    @pytest_asyncio.fixture(params=["taken", "given"])
    async def direction(self, request, db):
        taken = request.param == "taken"
        await seed(db, "primary", taken)
        await seed(db, "replica", not taken)
        return request.param

    @pytest.mark.parametrize("entry", WRITES)
    async def test_a_write_decides_on_the_rows_it_writes(self, db, direction, entry):
        before = await state(db)
        orm = db.new_orm(repos=repos(db))
        with suppress(PermissionError, LookupError):
            await write(db, orm, entry)
        after = await state(db)
        if direction == "taken":
            assert after == before, (
                f"the {entry} wrote {after} over {before}, deciding on the replica"
            )
        else:
            assert after != before, (
                f"the {entry} changed nothing, deciding on the replica"
            )
        replica_owner = ME if direction == "taken" else "bob"
        assert (await db.fetch(db.m.Doc, 1)).owner == replica_owner, (
            "the write left the app's reads on the primary"
        )
