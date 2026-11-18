"""``authorize`` is told the key a row stores, not the caller's spelling of it.

A database can match keys regardless of case - SQLite's ``NOCASE``, MySQL's
``_ci`` collations, SQL Server's default ones - and there ``"SECRET"`` names the
row stored as ``"secret"``. Asked about the text sent, an ``authorize`` that
protects ``"secret"`` would let the write through. It is asked once the row is
read, about the key the row stores, and never about a row the repo hides, so a
hidden row fares as a missing one whatever ``authorize`` says.

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over a
``WaShelf`` of ``WaFile`` rows, keyed by text the database compares without
regard to case, and a ``WaTag`` that can link one file.
"""

from typing import Any

import pytest
import pytest_asyncio
import strawberry

from strawberry_orm import AbstractRepo, UpsertNotFoundError
from tests.abstract.write_path_authorization import item, member

#: Every file, by key: its title and its shelf.
UNCHANGED = {"hidden": ("hidden", 1), "plain": ("plain", 1), "secret": ("secret", 1)}


@strawberry.input
class FileUpdate:
    id: strawberry.ID
    title: str | None = strawberry.UNSET


class AbstractTestAuthorizeIsToldTheStoredKey:
    """Shelf 1 holds files ``secret``, ``plain`` and ``hidden``; tag 1 links none.

    The files' repo hides ``hidden``, and ``protect`` denies ``secret``.
    """

    @pytest_asyncio.fixture
    async def shelf(self, writes):
        m = writes.m
        shelf = await writes.create(m.Shelf, id=1, label="shelf")
        for key in UNCHANGED:
            await writes.create(m.File, id=key, title=key, shelf=shelf)
        await writes.create(m.Tag, id=1, label="tag")
        return shelf

    @pytest.fixture
    def orm(self, writes):
        class Files(AbstractRepo):
            def scope_query(self, query, info):
                return writes.only(query, self.model, ["plain", "secret"])

        return writes.new_orm(repos={writes.m.File: Files})

    @pytest.fixture
    def asked(self):
        return []

    @pytest.fixture
    def protect(self, asked):
        def protect(action, model, pk, info):
            asked.append((action, pk))
            return pk != "secret"

        return protect

    @pytest.fixture
    def ref(self, orm, writes):
        return orm.ref(writes.m.File, update=FileUpdate, unlink=True, delete=True)

    @pytest.fixture
    def shelf_upsert(self, orm, writes):
        return orm.mutations.upsert_node_input(
            model=writes.m.Shelf,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                },
                "files": {
                    "_meta": {"update": ["title"], "unlink": True, "delete": True}
                },
            },
            name="WaShelfUpsert",
        )

    @staticmethod
    def on_files(upsert: type, operation: str, key: str) -> Any:
        """An upsert of shelf 1 that applies *operation* to file *key* in it."""
        files = member(upsert, "update", "files")
        values = {"title": "pwned"} if operation == "update" else {}
        return upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(
                files=[item(files, operation, id=key, **values)]
            ),
        )

    @staticmethod
    async def files(writes: Any) -> dict[str, tuple[str, int | None]]:
        rows = await writes.rows(writes.m.File)
        return {row.id: (row.title, row.shelf_id) for row in rows}

    @pytest.mark.parametrize("through", ["ref list", "nested upsert"])
    async def test_a_key_no_row_has_is_quoted_cut_short(
        self, writes, shelf, orm, ref, shelf_upsert, through
    ):
        """The error goes back to the client and into the log, on every request."""
        key = "k" * 50_000
        if through == "ref list":
            write = writes.refs(orm, shelf, "files", [item(ref, "unlink", id=key)])
        else:
            write = writes.upsert(orm, self.on_files(shelf_upsert, "update", key))
        with pytest.raises((LookupError, ValueError)) as refused:
            await write
        message = str(refused.value)
        assert len(message) <= 300 and key[:100] not in message, message[:600]

    @pytest.mark.parametrize("operation", ["update", "unlink", "delete"])
    async def test_a_ref_list_item_is_authorized_by_the_key_the_row_stores(
        self, writes, shelf, orm, asked, protect, ref, operation
    ):
        values = {"title": "pwned"} if operation == "update" else {}
        refs = [item(ref, operation, id="SECRET", **values)]
        await writes.refs(orm, shelf, "files", refs, authorize=protect)
        assert asked == [(operation, "secret")]
        assert await self.files(writes) == UNCHANGED

    @pytest.mark.parametrize("operation", ["update", "unlink", "delete"])
    async def test_a_nested_item_is_authorized_by_the_key_the_row_stores(
        self, writes, shelf, orm, asked, protect, shelf_upsert, operation
    ):
        payload = self.on_files(shelf_upsert, operation, "SECRET")
        with pytest.raises(PermissionError, match=f"authorize denied the {operation}"):
            await writes.upsert(orm, payload, authorize=protect)
        assert asked == [("upsert", 1), (operation, "secret")]
        assert await self.files(writes) == UNCHANGED

    async def test_a_link_target_is_authorized_by_the_key_the_row_stores(
        self, writes, shelf, orm, asked, protect
    ):
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Tag,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                },
                "file": {"_meta": {"update": ["title"]}},
            },
            name="WaTagUpsert",
        )
        link = member(upsert, "update", "file")
        target = member(link, "update")(id="SECRET", title="pwned")
        payload = upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(file=link(update=target)),
        )
        with pytest.raises(PermissionError, match="authorize denied the update"):
            await writes.upsert(orm, payload, authorize=protect)
        assert asked == [("upsert", 1), ("update", "secret")]
        assert await self.files(writes) == UNCHANGED

    @pytest.mark.parametrize("allow", [True, False])
    async def test_a_hidden_row_fares_as_a_missing_one_whatever_authorize_says(
        self, writes, shelf, orm, asked, ref, shelf_upsert, allow
    ):
        def authorize(action, model, pk, info):
            asked.append((action, pk))
            return allow or model is not writes.m.File

        for key in ("hidden", "missing"):
            update = item(ref, "update", id=key, title="pwned")
            await writes.refs(orm, shelf, "files", [update], authorize=authorize)
            for operation in ("unlink", "delete"):
                with pytest.raises(UpsertNotFoundError, match=f"'{key}' in"):
                    await writes.refs(
                        orm,
                        shelf,
                        "files",
                        [item(ref, operation, id=key)],
                        authorize=authorize,
                    )
            for operation in ("update", "delete"):
                with pytest.raises(UpsertNotFoundError, match=f"'{key}'"):
                    await writes.upsert(
                        orm,
                        self.on_files(shelf_upsert, operation, key),
                        authorize=authorize,
                    )
        assert asked == [("upsert", 1)] * 4
        assert await self.files(writes) == UNCHANGED
