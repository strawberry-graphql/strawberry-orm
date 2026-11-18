"""A write is checked against every model the row it writes belongs to.

A row of a subclass is a row of its base too, so ``authorize=`` and the repos'
``can_*`` hooks are asked about the input's model, the row's own model and each
model between, whichever of them the write reaches the row through. A check
keyed on the base refuses a subclass row, and one keyed on the subclass refuses
the row when a base-typed input reaches it.

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over a ``Shelf``
of ``Doc`` rows, ``Contract`` subclassing ``Doc`` (the same model on Tortoise,
whose subclasses keep rows of their own), and a ``Pin`` linking one doc.
"""

from typing import Any

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import AbstractRepo
from strawberry_orm.mutations import RelationRemovalPolicy
from tests.abstract.write_path_authorization import item, member

#: The rows a keyed check refuses; ``None`` is a create.
DENIED = {2, 3, None}
UNCHANGED = {1: ("plain", 1), 2: ("contract", 1), 3: ("third", 1)}


@strawberry.input
class TgDocUpdate:
    id: strawberry.ID
    title: str | None = strawberry.UNSET


def keyed_repo(keyed: type) -> type:
    """A repo whose ``can_*`` refuse the denied rows while it answers for *keyed*."""

    class Keyed(AbstractRepo):
        def refuses(self, row: Any) -> bool:
            return self.model is keyed and (row is None or row.id in DENIED)

        def can_create(self, data, info):
            return not self.refuses(None)

        def can_update(self, instance, data, info):
            return not self.refuses(instance)

        def can_upsert(self, instance, info):
            return not self.refuses(instance)

        def can_delete(self, instance, info):
            return not self.refuses(instance)

        def can_link(self, parent, field, instance, info):
            return not self.refuses(instance)

        def can_unlink(self, parent, field, instance, info):
            return not self.refuses(instance)

    return Keyed


class AbstractTestEveryModelOfTheRowIsAsked:
    """Shelf 1 holds doc 1, a plain doc, and contracts 2 and 3; pin 1 links 3."""

    @pytest_asyncio.fixture
    async def shelf(self, writes):
        m = writes.m
        shelf = await writes.create(m.Shelf, id=1, label="shelf")
        await writes.create(m.Doc, id=1, title="plain", shelf=shelf)
        await writes.create(m.Contract, id=2, title="contract", shelf=shelf)
        third = await writes.create(m.Contract, id=3, title="third", shelf=shelf)
        await writes.create(m.Pin, id=1, label="pin", doc=third)
        return shelf

    @pytest.fixture(params=["base", "subclass"])
    def keyed(self, request, writes):
        return writes.m.Doc if request.param == "base" else writes.m.Contract

    @pytest.fixture(params=["authorize", "repo"])
    def checker(self, request):
        return request.param

    @pytest.fixture
    def orm(self, writes, keyed, checker):
        if checker == "authorize":
            return writes.new_orm()
        return writes.new_orm(repos={writes.m.Doc: keyed_repo(keyed)})

    @pytest.fixture
    def check(self, keyed, checker):
        """The ``authorize=`` keyword the write takes."""
        if checker == "repo":
            return {}

        def authorize(action, model, pk, info):
            return not (model is keyed and pk in DENIED)

        return {"authorize": authorize}

    @staticmethod
    async def docs(writes: Any) -> dict[int, tuple[str, int | None]]:
        return {
            row.id: (row.title, row.shelf_id) for row in await writes.rows(writes.m.Doc)
        }

    @staticmethod
    async def refused(write: Any, action: str, checker: str, skips: bool) -> None:
        """Run *write*: a plain item ``authorize`` denies is skipped, else it raises."""
        if checker == "authorize" and skips:
            await write
            return
        check = f"authorize denied the {action}" if checker == "authorize" else "can_"
        with pytest.raises(PermissionError, match=check):
            await write

    @staticmethod
    def doc_upsert(orm: Any, model: type, name: str) -> type:
        return orm.mutations.upsert_node_input(
            model=model,
            project={
                "_meta": {
                    "create": ["title"],
                    "update": ["title"],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                }
            },
            name=name,
        )

    @staticmethod
    def shelf_upsert(orm: Any, writes: Any) -> type:
        return orm.mutations.upsert_node_input(
            model=writes.m.Shelf,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                },
                "docs": {
                    "_meta": {"update": ["title"], "unlink": True, "delete": True}
                },
            },
            name="TgShelfUpsert",
        )

    @staticmethod
    def pin_upsert(orm: Any, writes: Any) -> type:
        return orm.mutations.upsert_node_input(
            model=writes.m.Pin,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["label"], "onMatch": "UPDATE"},
                },
                "doc": {
                    "_meta": {
                        "update": ["title"],
                        "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                        "onReplace": ["DISCONNECT", "DELETE"],
                    }
                },
            },
            name="TgPinUpsert",
        )

    @staticmethod
    def on_pin(upsert: type, link: Any) -> Any:
        return upsert(
            where=member(upsert, "where")(label="pin"),
            update=member(upsert, "update")(doc=link),
        )

    @pytest.mark.parametrize("operation", ["update", "unlink", "delete"])
    async def test_a_ref_list_item(self, writes, shelf, orm, check, checker, operation):
        ref = orm.ref(writes.m.Doc, update=TgDocUpdate, unlink=True, delete=True)
        values = {"title": "pwned"} if operation == "update" else {}
        refs = [item(ref, operation, id="2", **values)]
        write = writes.refs(orm, shelf, "docs", refs, **check)
        await self.refused(write, operation, checker, skips=True)
        assert await self.docs(writes) == UNCHANGED

    @pytest.mark.parametrize("operation", ["update", "unlink", "delete"])
    async def test_a_nested_list_item(
        self, writes, shelf, orm, check, checker, operation
    ):
        upsert = self.shelf_upsert(orm, writes)
        docs = member(upsert, "update", "docs")
        values = {"title": "pwned"} if operation == "update" else {}
        payload = upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(
                docs=[item(docs, operation, id="2", **values)]
            ),
        )
        write = writes.upsert(orm, payload, **check)
        await self.refused(write, operation, checker, skips=False)
        assert await self.docs(writes) == UNCHANGED

    async def test_an_upsert_matching_through_the_base(
        self, writes, shelf, orm, check, checker
    ):
        upsert = self.doc_upsert(orm, writes.m.Doc, "TgDocUpsert")
        payload = upsert(
            where=member(upsert, "where")(id="2"),
            update=member(upsert, "update")(title="pwned"),
        )
        write = writes.upsert(orm, payload, **check)
        await self.refused(write, "upsert", checker, skips=False)
        assert await self.docs(writes) == UNCHANGED

    async def test_an_upsert_creating_a_subclass_row(
        self, writes, shelf, orm, check, checker
    ):
        upsert = self.doc_upsert(orm, writes.m.Contract, "TgContractUpsert")
        payload = upsert(
            where=member(upsert, "where")(id="9"),
            create=member(upsert, "create")(title="new"),
        )
        write = writes.upsert(orm, payload, **check)
        await self.refused(write, "create", checker, skips=False)
        assert await self.docs(writes) == UNCHANGED

    @pytest.mark.parametrize("arm", ["update", "upsert"])
    async def test_a_link_target(self, writes, shelf, orm, check, checker, arm):
        upsert = self.pin_upsert(orm, writes)
        link = member(upsert, "update", "doc")
        target = member(link, arm)
        if arm == "update":
            sent = link(update=target(id="2", title="pwned"))
        else:
            sent = link(
                upsert=target(
                    where=member(target, "where")(id="2"),
                    update=member(target, "update")(title="pwned"),
                )
            )
        write = writes.upsert(orm, self.on_pin(upsert, sent), **check)
        await self.refused(write, arm, checker, skips=False)
        assert [pin.doc_id for pin in await writes.rows(writes.m.Pin)] == [3]
        assert await self.docs(writes) == UNCHANGED

    async def test_a_replaced_link_target(self, writes, shelf, orm, check, checker):
        """The link cannot let go of a target the check denies, to delete or not."""
        upsert = self.pin_upsert(orm, writes)
        link = member(upsert, "update", "doc")
        sent = link(update=member(link, "update")(id="1"))
        sent.on_replace = RelationRemovalPolicy.DELETE
        write = writes.upsert(orm, self.on_pin(upsert, sent), **check)
        await self.refused(write, "unlink", checker, skips=False)
        assert [pin.doc_id for pin in await writes.rows(writes.m.Pin)] == [3]
        assert await self.docs(writes) == UNCHANGED

    async def test_a_plain_doc_is_written_as_before(
        self, writes, shelf, orm, check, keyed
    ):
        """The control: doc 1 is no contract, so a check keyed on one lets it be."""
        if keyed is writes.m.Doc:
            pytest.skip("doc 1 is a Doc")
        ref = orm.ref(writes.m.Doc, update=TgDocUpdate, unlink=True, delete=True)
        refs = [item(ref, "update", id="1", title="edited")]
        await writes.refs(orm, shelf, "docs", refs, **check)
        assert (await self.docs(writes))[1] == ("edited", 1)

    async def test_each_model_of_the_row_is_asked_once(self, writes, shelf):
        m = writes.m
        asked = []

        def authorize(action, model, pk, info):
            asked.append((action, model.__name__, pk))
            return True

        orm = writes.new_orm()
        upsert = self.doc_upsert(orm, m.Doc, "TgDocUpsert")
        payload = upsert(
            where=member(upsert, "where")(id="2"),
            update=member(upsert, "update")(title="edited"),
        )
        await writes.upsert(orm, payload, authorize=authorize)
        expected = [("upsert", m.Doc.__name__, 2)]
        if m.Contract is not m.Doc:
            expected.append(("upsert", m.Contract.__name__, 2))
        assert asked == expected
        assert {row.id: row.title for row in await writes.rows(m.Doc)}[2] == "edited"
