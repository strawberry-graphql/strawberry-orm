"""A many-to-many link through a model of the app's own writes rows of it.

Linking a row into such a list inserts a row of the ``through`` model, or of
the class SQLAlchemy maps onto ``secondary``, and unlinking or deleting a
member deletes one. Each is checked as a create or a delete of that model:
``authorize`` and the model's repos are asked, and a link row the repo hides
reads as no link at all. An auto-created join table is no model of the app's,
so there is nothing to ask about its rows.

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over an
``LrClub`` whose ``members`` go through ``LrMembership`` and whose ``fans`` go
through an auto-created table, both of ``LrPerson`` rows, which list the clubs
they are members of as ``clubs``.
"""

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import AbstractRepo, UpsertNotFoundError
from tests.abstract.write_path_authorization import item, member


@strawberry.input
class LrPersonCreate:
    name: str


@strawberry.input
class LrPersonUpdate:
    id: strawberry.ID
    name: str | None = strawberry.UNSET


PEOPLE = {"create": ["name"], "update": ["name"], "unlink": True, "delete": True}

CLUB = {
    "_meta": {
        "create": ["name"],
        "update": ["name"],
        "upsert": {"where": ["id"], "onMatch": "UPDATE"},
    },
    "members": {"_meta": {**PEOPLE, "upsert": {"where": ["id"], "onMatch": "UPDATE"}}},
    "fans": {"_meta": PEOPLE},
}

#: Alice is a member of both clubs; the first link row puts her on the board.
SEEDED = [(1, 2), (2, 2)]
PEOPLE_SEEDED = ["mallory", "alice", "carol"]

LINKS = [
    "ref-list update",
    "ref-list create",
    "ref-list upsert",
    "nested update",
    "nested create",
    "nested upsert",
    "nested upsert create",
    "new club",
    "from the person",
]
#: The ways to link a row the list may already hold.
RELINKS = [
    "ref-list update",
    "ref-list upsert",
    "nested update",
    "nested upsert",
    "from the person",
]
REMOVALS = ["ref-list unlink", "nested unlink", "ref-list delete", "nested delete"]


def membership_repo(
    writes: Any, calls: list, *, visible: list[int] | None = None, refuses: str = ""
) -> type:
    """The link model's repo: it shows the *visible* rows and records its checks."""

    class Memberships(AbstractRepo):
        def scope_query(self, query, info):
            if visible is None:
                return query
            return writes.only(query, self.model, visible)

        def can_create(self, data, info):
            values = {
                name.removesuffix("_id"): getattr(value, "pk", value)
                for name, value in data.items()
            }
            calls.append(("can_create", values))
            return refuses != "can_create"

        def can_delete(self, instance, info):
            calls.append(("can_delete", instance.id))
            return refuses != "can_delete"

    return Memberships


def denying(model: type, action: str, seen: list) -> Any:
    """An ``authorize`` denying *action* on *model* alone, recording what it is asked."""

    def authorize(action_asked, model_asked, pk, info):
        seen.append((action_asked, model_asked.__name__, pk))
        return (action_asked, model_asked) != (action, model)

    return authorize


async def memberships(writes: Any) -> list[tuple[int, int]]:
    rows = await writes.rows(writes.m.Membership)
    return sorted((row.club_id, row.person_id) for row in rows)


async def people(writes: Any) -> list[str]:
    return [person.name for person in await writes.rows(writes.m.Person)]


class AbstractTestLinkRowsAreWritesOfTheirModel:
    """The board (club 1) and the garden (club 2); Mallory, Alice and Carol.

    Alice is a member of both clubs, through link rows 1 and 2; Carol is a
    fan of the board.
    """

    @pytest_asyncio.fixture
    async def clubs(self, writes):
        m = writes.m
        board = await writes.create(m.Club, id=1, name="board")
        garden = await writes.create(m.Club, id=2, name="garden")
        mallory = await writes.create(m.Person, id=1, name="mallory")
        alice = await writes.create(m.Person, id=2, name="alice")
        carol = await writes.create(m.Person, id=3, name="carol")
        await writes.create(m.Membership, id=1, club_id=board.id, person_id=alice.id)
        await writes.create(m.Membership, id=2, club_id=garden.id, person_id=alice.id)
        await writes.add_related(board, "fans", carol)
        return SimpleNamespace(board=board, people={1: mallory, 2: alice, 3: carol})

    @pytest.fixture
    def calls(self):
        return []

    @staticmethod
    def orm(writes: Any, calls: list, **repo: Any) -> Any:
        m = writes.m
        return writes.new_orm(
            repos={m.Membership: membership_repo(writes, calls, **repo)}
        )

    @staticmethod
    async def link(
        writes: Any, orm: Any, clubs: Any, entry: str, pk: str, **check: Any
    ) -> None:
        """Link person *pk* (a new person, for a create) to the board through *entry*."""
        m = writes.m
        upsert = orm.mutations.upsert_node_input(
            model=m.Club, project=CLUB, name="LrClubUpsert"
        )
        items = member(upsert, "update", "members")
        target = member(items, "upsert")
        if entry == "from the person":
            ref = orm.ref(m.Club)
            person = clubs.people[int(pk)]
            await writes.refs(
                orm, person, "clubs", [item(ref, "update", id="1")], **check
            )
            return
        if entry.startswith("ref-list"):
            ref = orm.ref(m.Person, create=LrPersonCreate, update=LrPersonUpdate)
            sent = {
                "ref-list update": lambda: item(ref, "update", id=pk),
                "ref-list create": lambda: item(ref, "create", name="eve"),
                "ref-list upsert": lambda: items(
                    upsert=target(where=member(target, "where")(id=pk))
                ),
            }[entry]()
            await writes.refs(orm, clubs.board, "members", [sent], **check)
            return
        if entry == "new club":
            create = member(upsert, "create")
            created = member(create, "members")
            payload = upsert(
                where=member(upsert, "where")(id="9"),
                create=create(name="new", members=[item(created, "update", id=pk)]),
            )
            await writes.upsert(orm, payload, **check)
            return
        sent = {
            "nested update": lambda: item(items, "update", id=pk),
            "nested create": lambda: item(items, "create", name="eve"),
            "nested upsert": lambda: items(
                upsert=target(where=member(target, "where")(id=pk))
            ),
            "nested upsert create": lambda: items(
                upsert=target(
                    where=member(target, "where")(id="9"),
                    create=member(target, "create")(name="eve"),
                )
            ),
        }[entry]()
        payload = upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(members=[sent]),
        )
        await writes.upsert(orm, payload, **check)

    @staticmethod
    async def remove(
        writes: Any,
        orm: Any,
        clubs: Any,
        entry: str,
        relation: str,
        pk: str,
        **check: Any,
    ) -> None:
        """Unlink or delete person *pk* from the board's *relation* through *entry*."""
        operation = entry.split()[-1]
        if entry.startswith("ref-list"):
            ref = orm.ref(writes.m.Person, unlink=True, delete=True)
            refs = [item(ref, operation, id=pk)]
            await writes.refs(orm, clubs.board, relation, refs, **check)
            return
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Club, project=CLUB, name="LrClubUpsert"
        )
        items = member(upsert, "update", relation)
        payload = upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(
                **{relation: [item(items, operation, id=pk)]}
            ),
        )
        await writes.upsert(orm, payload, **check)

    # -- Linking writes a link row -------------------------------------------------

    @pytest.mark.parametrize("entry", LINKS)
    @pytest.mark.parametrize("denial", ["can_create", "authorize"])
    async def test_a_link_asks_to_create_its_row(
        self, writes, clubs, calls, entry, denial
    ):
        seen: list = []
        if denial == "can_create":
            orm, check = self.orm(writes, calls, refuses="can_create"), {}
        else:
            orm = self.orm(writes, calls)
            check = {"authorize": denying(writes.m.Membership, "create", seen)}
        with pytest.raises(PermissionError, match=f"{denial} denied the"):
            await self.link(writes, orm, clubs, entry, "1", **check)
        assert await memberships(writes) == SEEDED, "no link row is written"
        assert await people(writes) == PEOPLE_SEEDED, "nor the person it links"
        if denial == "authorize":
            asked = ("create", writes.m.Membership.__name__, None)
            assert asked in seen, f"authorize is asked {asked}, not only {seen}"

    @pytest.mark.parametrize("entry", LINKS)
    async def test_an_allowed_link_tells_can_create_the_row(
        self, writes, clubs, calls, entry
    ):
        await self.link(writes, self.orm(writes, calls), clubs, entry, "1")
        rows = await memberships(writes)
        added = [row for row in rows if row not in SEEDED]
        assert len(added) == 1, f"one link row is written, not {rows}"
        club, person = added[0]
        expected = [("can_create", {"club": club, "person": person})]
        assert calls == expected, f"can_create is told the row: {calls}"

    @pytest.mark.parametrize("entry", RELINKS)
    async def test_relinking_a_member_asks_nothing(self, writes, clubs, calls, entry):
        """Alice is on the board already: no link row is written, none is asked for."""
        seen: list = []
        check = {"authorize": denying(writes.m.Membership, "create", seen)}
        orm = self.orm(writes, calls, refuses="can_create")
        await self.link(writes, orm, clubs, entry, "2", **check)
        assert calls == [], f"the repo is asked nothing: {calls}"
        link_model = writes.m.Membership.__name__
        assert all(model != link_model for _a, model, _pk in seen), seen
        assert await memberships(writes) == SEEDED, "the link rows are as they were"

    async def test_a_link_row_the_repo_hides_is_no_link_to_keep(
        self, writes, clubs, calls
    ):
        """Relinking Alice past a link row the caller cannot see asks for one."""
        orm = self.orm(writes, calls, visible=[2], refuses="can_create")
        with pytest.raises(PermissionError, match="can_create denied the"):
            await self.link(writes, orm, clubs, "ref-list update", "2")
        expected = [("can_create", {"club": 1, "person": 2})]
        assert calls == expected, f"can_create is asked: {calls}"
        assert await memberships(writes) == SEEDED, "the link rows are as they were"

    # -- Unlinking and deleting delete link rows --------------------------------

    @pytest.mark.parametrize("entry", REMOVALS)
    async def test_a_link_row_the_repo_hides_reads_as_no_member(
        self, writes, clubs, calls, entry
    ):
        orm = self.orm(writes, calls, visible=[2])
        with pytest.raises(UpsertNotFoundError) as refused:
            await self.remove(writes, orm, clubs, entry, "members", "2")
        assert str(refused.value) == "No row 2 in members", str(refused.value)
        assert calls == [], f"the repo is asked nothing: {calls}"
        assert await memberships(writes) == SEEDED, "no link row is deleted"
        assert await people(writes) == PEOPLE_SEEDED, "nor any person"

    @pytest.mark.parametrize("entry", REMOVALS)
    @pytest.mark.parametrize("denial", ["can_delete", "authorize"])
    async def test_removing_a_member_asks_to_delete_its_rows(
        self, writes, clubs, calls, entry, denial
    ):
        seen: list = []
        if denial == "can_delete":
            orm, check = self.orm(writes, calls, refuses="can_delete"), {}
        else:
            orm = self.orm(writes, calls)
            check = {"authorize": denying(writes.m.Membership, "delete", seen)}
        with pytest.raises(PermissionError, match=f"{denial} denied the"):
            await self.remove(writes, orm, clubs, entry, "members", "2", **check)
        assert await memberships(writes) == SEEDED, "no link row is deleted"
        assert await people(writes) == PEOPLE_SEEDED, "nor any person"
        if denial == "authorize":
            asked = ("delete", writes.m.Membership.__name__, 1)
            assert asked in seen, f"authorize is asked {asked}, not only {seen}"

    @pytest.mark.parametrize("entry", REMOVALS)
    async def test_an_allowed_removal_deletes_its_rows(
        self, writes, clubs, calls, entry
    ):
        """A deleted member takes its other link rows, which are asked about too."""
        await self.remove(writes, self.orm(writes, calls), clubs, entry, "members", "2")
        if entry.endswith("unlink"):
            expected, kept, left = [("can_delete", 1)], [(2, 2)], PEOPLE_SEEDED
        else:
            expected, kept = [("can_delete", 1), ("can_delete", 2)], []
            left = ["mallory", "carol"]
        assert calls == expected, f"can_delete is asked about each row: {calls}"
        assert await memberships(writes) == kept, "the link rows asked about go"
        assert await people(writes) == left

    # -- An auto-created join table ---------------------------------------------

    @pytest.mark.parametrize("operation", ["link", "unlink", "delete"])
    async def test_an_auto_created_join_table_is_not_asked(
        self, writes, clubs, calls, operation
    ):
        """Its rows are no model's, so ``authorize`` is asked about the people alone."""
        seen: list = []
        check = {"authorize": denying(writes.m.Membership, operation, seen)}
        orm = self.orm(writes, calls)
        if operation == "link":
            ref = orm.ref(writes.m.Person)
            refs = [item(ref, "update", id="1")]
            await writes.refs(orm, clubs.board, "fans", refs, **check)
        else:
            entry = f"ref-list {operation}"
            await self.remove(writes, orm, clubs, entry, "fans", "3", **check)
        asked = {model for _action, model, _pk in seen}
        assert asked == {writes.m.Person.__name__}, f"only people are asked: {seen}"
        assert calls == [], f"the link model's repo is asked nothing: {calls}"
        fans = [fan.name for fan in await writes.related(clubs.board, "fans")]
        assert fans == {"link": ["mallory", "carol"]}.get(operation, []), fans
