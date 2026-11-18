"""A delete the library performs asks the repos of the rows it takes with it.

Deleting a ``Box`` deletes its ``Note`` rows, and their ``Stamp`` rows, with
it: through the ORM's cascade, or the foreign key's ``ON DELETE CASCADE``. A
note's repo hiding it, refusing ``can_delete``, ``authorize=`` refusing its
deletion, or a stamp's repo refusing, refuses the delete, whichever way the
write reaches the box: a ref-list delete, a nested list delete, or a single
link let go of with ``onReplace: DELETE`` (or, on SQLAlchemy, a
``delete-orphan`` link). A box the cascade takes nothing with is deleted as
before.

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over a
``Shelf`` of ``Box`` rows, each holding ``Note`` rows holding ``Stamp`` rows,
and a ``Pin`` linking one box; and, but on Tortoise, ``SealedNote``, a
subclass of ``Note`` that reads as a plain note.
"""

from typing import Any

import pytest
import pytest_asyncio

from strawberry_orm import AbstractRepo
from strawberry_orm.mutations import RelationRemovalPolicy
from tests.abstract.write_path_authorization import item, member

INTACT = {"boxes": [1, 2], "notes": [1], "stamps": [1], "pins": [1]}


def refusing(writes: Any, refusal: str) -> tuple[dict[type, type], dict[str, Any]]:
    """The ``repos=`` and the write's keywords that refuse note 1 (or stamp 1)."""
    m = writes.m

    class Notes(AbstractRepo):
        def scope_query(self, query, info):
            return writes.only(query, m.Note, []) if refusal == "hidden" else query

        def can_delete(self, instance, info):
            return refusal != "can_delete"

    class Stamps(AbstractRepo):
        def can_delete(self, instance, info):
            return refusal != "stamp"

    if refusal == "authorize":

        def authorize(action, model, pk, info):
            return model is not m.Note

        return {}, {"authorize": authorize}
    if refusal == "ungoverned":
        return {}, {}
    return {m.Note: Notes, m.Stamp: Stamps}, {}


class AbstractTestADeleteAsksTheRowsItCascadesTo:
    """Shelf 1 holds box 1, with note 1 and its stamp 1, and empty box 2; pin 1 links box 1."""

    @pytest_asyncio.fixture
    async def shelf(self, writes):
        m = writes.m
        shelf = await writes.create(m.Shelf, id=1, label="shelf")
        await writes.create(m.Box, id=1, label="full", shelf_id=1)
        await writes.create(m.Box, id=2, label="empty", shelf_id=1)
        await writes.create(m.Note, id=1, body="audit", box_id=1)
        await writes.create(m.Stamp, id=1, mark="legal", note_id=1)
        await writes.create(m.Pin, id=1, label="pin", box_id=1)
        return shelf

    @pytest.fixture(
        params=["hidden", "can_delete", "authorize", "stamp", "allowed", "ungoverned"]
    )
    def refusal(self, request):
        """What refuses note 1: ``allowed`` and ``ungoverned`` (no repo) are controls."""
        return request.param

    @staticmethod
    async def state(writes: Any) -> dict[str, list[Any]]:
        m = writes.m

        async def keys(model: type) -> list[int]:
            return [row.id for row in await writes.rows(model)]

        return {
            "boxes": await keys(m.Box),
            "notes": await keys(m.Note),
            "stamps": await keys(m.Stamp),
            "pins": [pin.box_id for pin in await writes.rows(m.Pin)],
        }

    async def deletes(self, writes: Any, write: Any, refusal: str, pin: Any) -> None:
        """Run *write*, deleting box 1: refused, or taking its note and stamp too."""
        if refusal in ("allowed", "ungoverned"):
            await write
            gone = {"boxes": [2], "notes": [], "stamps": [], "pins": [pin]}
            assert await self.state(writes) == gone
            return
        with pytest.raises(PermissionError):
            await write
        assert await self.state(writes) == INTACT

    @staticmethod
    def ref_list(orm: Any, writes: Any, shelf: Any, box: str, check: Any) -> Any:
        ref = orm.ref(writes.m.Box, delete=True)
        return writes.refs(orm, shelf, "boxes", [item(ref, "delete", id=box)], **check)

    @staticmethod
    def nested_list(orm: Any, writes: Any, box: str, check: Any) -> Any:
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Shelf,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                },
                "boxes": {"_meta": {"delete": True}},
            },
            name="TgcShelfUpsert",
        )
        boxes = member(upsert, "update", "boxes")
        payload = upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(boxes=[item(boxes, "delete", id=box)]),
        )
        return writes.upsert(orm, payload, **check)

    @staticmethod
    def replaced(orm: Any, writes: Any, check: Any) -> Any:
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Pin,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["label"], "onMatch": "UPDATE"},
                },
                "box": {
                    "_meta": {
                        "update": ["label"],
                        "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                        "onReplace": ["DISCONNECT", "DELETE"],
                    }
                },
            },
            name="TgcPinUpsert",
        )
        link = member(upsert, "update", "box")
        sent = link(update=member(link, "update")(id="2"))
        sent.on_replace = RelationRemovalPolicy.DELETE
        payload = upsert(
            where=member(upsert, "where")(label="pin"),
            update=member(upsert, "update")(box=sent),
        )
        return writes.upsert(orm, payload, **check)

    async def test_a_ref_list_delete(self, writes, shelf, refusal):
        repos, check = refusing(writes, refusal)
        orm = writes.new_orm(repos=repos)
        write = self.ref_list(orm, writes, shelf, "1", check)
        await self.deletes(writes, write, refusal, pin=None)

    async def test_a_nested_list_delete(self, writes, shelf, refusal):
        repos, check = refusing(writes, refusal)
        orm = writes.new_orm(repos=repos)
        write = self.nested_list(orm, writes, "1", check)
        await self.deletes(writes, write, refusal, pin=None)

    async def test_a_replaced_link_target(self, writes, shelf, refusal):
        repos, check = refusing(writes, refusal)
        orm = writes.new_orm(repos=repos)
        write = self.replaced(orm, writes, check)
        await self.deletes(writes, write, refusal, pin=2)

    async def test_a_subclass_row_is_asked_through_its_own_repo(self, writes, shelf):
        """Box 3's note is a sealed note, whose own repo refuses to delete it."""
        m = writes.m
        if not hasattr(m, "SealedNote"):
            pytest.skip("a Tortoise subclass keeps its rows in a table of its own")
        await writes.create(m.Box, id=3, label="sealed", shelf_id=1)
        await writes.create(m.SealedNote, id=5, body="sealed", box_id=3)

        class Sealed(AbstractRepo):
            def can_delete(self, instance, info):
                return False

        orm = writes.new_orm(repos={m.SealedNote: Sealed})
        with pytest.raises(PermissionError, match="can_delete denied"):
            await self.ref_list(orm, writes, shelf, "3", {})
        assert [box.id for box in await writes.rows(m.Box)] == [1, 2, 3]
        assert [note.id for note in await writes.rows(m.SealedNote)] == [5]

    @pytest.mark.parametrize("entry", ["ref list", "nested list"])
    async def test_a_box_with_nothing_to_take_is_deleted(
        self, writes, shelf, refusal, entry
    ):
        """The control: box 2 holds no notes, so no note's repo is in its way."""
        repos, check = refusing(writes, refusal)
        orm = writes.new_orm(repos=repos)
        if entry == "ref list":
            await self.ref_list(orm, writes, shelf, "2", check)
        else:
            await self.nested_list(orm, writes, "2", check)
        assert await self.state(writes) == {**INTACT, "boxes": [1]}
