"""A subclass row its base reads as its own is still written by the subclass's repo.

A Django multi-table child read through its parent, and a SQLAlchemy
joined-table subclass with no discriminator read through its base, come back
as rows of the base. Nothing in the row says which subclass it is, only the
subclass table holding it, so a write reaching one through the base has to look
there before it chooses the repo that owns the row, as a polymorphic load lets
it do from the row itself.

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over models of
one shape - a ``WaFolder`` of ``WaDoc`` rows, ``WaSealedDoc`` and
``WaPublicDoc`` subclassing ``WaDoc``, ``WaRestrictedDoc`` subclassing
``WaSealedDoc``, and a ``WaPin`` linking one doc - whose ``also_a(model, pk)``
makes an existing row a row of another subclass as well.
"""

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import AbstractRepo, UpsertNotFoundError
from tests.abstract.write_path_authorization import item, member

#: The sealed docs ``WaSealedDoc``'s repo lets a caller see.
SEALED_VISIBLE = [3, 5, 6]


@strawberry.input
class DocUpdate:
    id: strawberry.ID
    title: str | None = strawberry.UNSET


def recorder(writes: Any, calls: list, name: str, visible: list[int] | None) -> type:
    """A repo showing the *visible* rows (all with ``None``) that records its checks.

    Only the sealed docs' repo refuses to let a link go of one of its rows.
    """

    class Recorder(AbstractRepo):
        def scope_query(self, query, info):
            if visible is None:
                return query
            return writes.only(query, self.model, visible)

        def can_update(self, instance, data, info):
            calls.append((name, "can_update", instance.id))
            return True

        def can_unlink(self, parent, field, instance, info):
            calls.append(
                (name, "can_unlink", f"{type(instance).__name__} {instance.id}")
            )
            return name != "sealed"

    return Recorder


class AbstractTestSubclassRowsReadAsTheBase:
    """Doc 1 is plain; 2 and 3 are sealed, and only 3 is visible as one.

    Doc 4 shares sealed doc 2's title; doc 5 is a sealed and a public doc at
    once; doc 6 is restricted, which its sealed repo would show and its own
    hides. Pin 1 holds doc 3 and pin 2 doc 5.
    """

    @pytest_asyncio.fixture
    async def docs(self, writes):
        m = writes.m
        folder = await writes.create(m.Folder, id=1)

        async def doc(model: type, pk: int, title: str) -> Any:
            return await writes.create(model, id=pk, title=title, folder=folder)

        await doc(m.Doc, 1, "plain")
        await doc(m.SealedDoc, 2, "sealed")
        open_doc = await doc(m.SealedDoc, 3, "open")
        await doc(m.Doc, 4, "sealed")
        both = await doc(m.SealedDoc, 5, "both")
        await writes.also_a(m.PublicDoc, 5)
        await doc(m.RestrictedDoc, 6, "restricted")
        await writes.create(m.Pin, id=1, label="open pin", doc=open_doc)
        await writes.create(m.Pin, id=2, label="both pin", doc=both)
        return SimpleNamespace(folder=folder)

    @pytest.fixture
    def calls(self):
        return []

    @pytest.fixture
    def orm(self, writes, calls):
        m = writes.m
        return writes.new_orm(
            repos={
                m.Doc: recorder(writes, calls, "doc", None),
                m.SealedDoc: recorder(writes, calls, "sealed", SEALED_VISIBLE),
                m.PublicDoc: recorder(writes, calls, "public", None),
                m.RestrictedDoc: recorder(writes, calls, "restricted", []),
            }
        )

    @staticmethod
    def upsert_by(orm: Any, writes: Any, field: str) -> type:
        return orm.mutations.upsert_node_input(
            model=writes.m.Doc,
            project={
                "_meta": {
                    "update": ["title"],
                    "upsert": {"where": [field], "onMatch": "UPDATE"},
                }
            },
            name=f"WaDocBy{field.title()}",
        )

    @pytest.fixture
    def doc_upsert(self, orm, writes):
        return self.upsert_by(orm, writes, "id")

    @staticmethod
    def edit(upsert: type, to: str, **where: Any) -> Any:
        return upsert(
            where=member(upsert, "where")(**where),
            update=member(upsert, "update")(title=to),
        )

    @staticmethod
    async def titles(writes: Any) -> dict[int, str]:
        return {row.id: row.title for row in await writes.rows(writes.m.Doc)}

    @pytest.fixture
    def ref(self, orm, writes):
        return orm.ref(writes.m.Doc, update=DocUpdate, delete=True)

    async def test_an_upsert_through_the_base_cannot_reach_a_row_its_own_repo_hides(
        self, writes, docs, orm, calls, doc_upsert
    ):
        with pytest.raises(UpsertNotFoundError):
            await writes.upsert(orm, self.edit(doc_upsert, "pwned", id="2"))
        assert (await self.titles(writes))[2] == "sealed"
        assert calls == []

    async def test_an_upsert_through_the_base_is_checked_by_the_rows_own_repo(
        self, writes, docs, orm, calls, doc_upsert
    ):
        seen = []

        def authorize(action, model, pk, info):
            seen.append((action, model.__name__, pk))
            return True

        payload = self.edit(doc_upsert, "edited", id="3")
        row = await writes.upsert(orm, payload, authorize=authorize)
        assert type(row) is writes.m.SealedDoc
        assert calls == [("sealed", "can_update", 3)]
        assert seen == [("upsert", "WaSealedDoc", 3)]
        assert (await self.titles(writes))[3] == "edited"

    async def test_an_upsert_match_leaves_out_a_row_its_own_repo_hides(
        self, writes, docs, orm, calls
    ):
        """Counted, hidden doc 2 would make the match ambiguous, and so tell
        the caller a hidden row has the title."""
        by_title = self.upsert_by(orm, writes, "title")
        row = await writes.upsert(orm, self.edit(by_title, "renamed", title="sealed"))
        assert row.id == 4
        assert calls == [("doc", "can_update", 4)]

    async def test_a_ref_list_through_the_base_cannot_reach_a_row_its_own_repo_hides(
        self, writes, docs, orm, calls, ref
    ):
        folder = docs.folder
        update = item(ref, "update", id="2", title="pwned")
        await writes.refs(orm, folder, "docs", [update])
        with pytest.raises(UpsertNotFoundError):
            await writes.refs(orm, folder, "docs", [item(ref, "delete", id="2")])
        assert (await self.titles(writes))[2] == "sealed"
        assert calls == []

    async def test_the_deepest_subclass_with_a_repo_of_its_own_decides(
        self, writes, docs, orm, calls, doc_upsert, ref
    ):
        with pytest.raises(UpsertNotFoundError):
            await writes.upsert(orm, self.edit(doc_upsert, "pwned", id="6"))
        with pytest.raises(UpsertNotFoundError):
            await writes.refs(orm, docs.folder, "docs", [item(ref, "delete", id="6")])
        assert (await self.titles(writes))[6] == "restricted"
        assert calls == []

    async def test_a_row_two_subclasses_hold_is_written_by_neither(
        self, writes, docs, orm, calls, doc_upsert, ref
    ):
        """No one repo answers for doc 5, so it is out of reach through the base."""
        with pytest.raises(UpsertNotFoundError):
            await writes.upsert(orm, self.edit(doc_upsert, "pwned", id="5"))
        await writes.refs(
            orm, docs.folder, "docs", [item(ref, "update", id="5", title="pwned")]
        )
        with pytest.raises(UpsertNotFoundError):
            await writes.refs(orm, docs.folder, "docs", [item(ref, "delete", id="5")])
        assert (await self.titles(writes))[5] == "both"
        assert calls == []

    @pytest.fixture
    def pin_upsert(self, orm, writes):
        return orm.mutations.upsert_node_input(
            model=writes.m.Pin,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["label"], "onMatch": "UPDATE"},
                },
                "doc": {"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
            },
            name="WaPinUpsert",
        )

    @staticmethod
    def re_point(upsert: type, label: str, pk: str) -> Any:
        link = member(upsert, "update", "doc")
        target = member(link, "upsert")
        return upsert(
            where=member(upsert, "where")(label=label),
            update=member(upsert, "update")(
                doc=link(upsert=target(where=member(target, "where")(id=pk)))
            ),
        )

    async def test_re_pointing_a_link_asks_the_previous_rows_own_repo(
        self, writes, docs, orm, calls, pin_upsert
    ):
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await writes.upsert(orm, self.re_point(pin_upsert, "open pin", "1"))
        assert calls == [("sealed", "can_unlink", "WaSealedDoc 3")]
        pins = await writes.rows(writes.m.Pin)
        assert [pin.doc_id for pin in pins] == [3, 5]

    async def test_re_pointing_a_link_off_a_row_two_subclasses_hold_is_refused(
        self, writes, docs, orm, calls, pin_upsert
    ):
        with pytest.raises(PermissionError, match="more than one subclass"):
            await writes.upsert(orm, self.re_point(pin_upsert, "both pin", "1"))
        assert calls == []
        pins = await writes.rows(writes.m.Pin)
        assert [pin.doc_id for pin in pins] == [3, 5]
