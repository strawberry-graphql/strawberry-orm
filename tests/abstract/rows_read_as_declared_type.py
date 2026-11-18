"""Every row a field hands out is held to the scope of each type it is read as.

A field typed with ``DocumentType`` can hand back rows of ``Memo``, a subclass
of ``Document``: a Django proxy or multi-table child, an inherited SQLAlchemy
mapping, a Tortoise model inheriting another. Such a row is read as a
``DocumentType``, so ``DocumentType.scope_rows`` holds it, and it is a memo,
so ``MemoType.scope_rows`` holds it too. That has to hold whatever kind of
field hands it out, whether the rows come as a query, as rows already read or
as one row, under an interface, with the optimizer or without.

A backend provides ``orm`` (a fresh ORM with a payload policy) and ``models``
(``Document``, ``Memo``, and ``Folder``, whose ``memos`` relation reads
``Memo`` rows), and on the test class:

- ``asynchronous``: whether ``run`` executes asynchronously;
- ``run(schema, query)``: executing against the seeded rows;
- ``published_only(query)``: the scope of ``Document``'s type;
- ``no_secrets(query)``: the scope of ``Memo``'s type;
- ``memos()``: an unexecuted query for every memo, in id order;
- ``documents()``: an unexecuted query for every document, in id order;
- ``memos_of(folder)``: an unexecuted query for the folder's memos, in id order;
- ``read(query)``: the rows of *query*, or an awaitable of them;
- ``documents_read``: the titles ``documents()`` shows under ``DocumentType``;
- ``own_read``: the titles ``memos()`` shows under ``MemoType`` alone, which
  holds a memo to ``DocumentType``'s scope as well where ``Document``'s table
  holds its row.

Seed: the first of two folders holds the memos "public" (id 1), "secret"
(id 2: published, but hidden by ``Memo``'s type) and "draft" (id 3: not
published). Only "public" passes both scopes. Where a memo's row is kept apart
from the base's, a published document shares the draft's key.
"""

from inspect import isawaitable

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

PUBLIC = [{"title": "public"}]
#: Matches "public" and "secret", so only the scopes can tell them apart.
FILTERED = (
    '(filter: {field: {title: {contains: "c"}}}, order: [{field: {title: DESC}}])'
)


def _then(value, then):
    if isawaitable(value):

        async def _awaited():
            result = then(await value)
            return await result if isawaitable(result) else result

        return _awaited()
    return then(value)


def _gid(pk):
    return str(relay.GlobalID("DocumentType", str(pk)))


class AbstractTestRowsReadAsDeclaredType:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def needs_async(self):
        if not self.asynchronous:
            pytest.skip("an async resolver needs async execution")

    def schema(self, orm, models, *, optimizer):
        suite = self
        DocumentFilter = orm.filter(models.Document)
        DocumentOrder = orm.order(models.Document)
        MemoFilter = orm.filter(models.Memo)

        @orm.type(models.Document, filters=DocumentFilter, order=DocumentOrder)
        class DocumentType(relay.Node):
            id: relay.NodeID[int]
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return suite.published_only(query)

            @classmethod
            def resolve_nodes(cls, *, info, node_ids, required=False):
                wanted = [int(pk) for pk in node_ids]

                def pick(rows):
                    by_id = {row.id: row for row in rows}
                    return [by_id.get(pk) for pk in wanted]

                return _then(suite.read(suite.memos()), pick)

        @orm.type(models.Memo)
        class MemoType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return suite.no_secrets(query)

        @orm.type(models.Folder)
        class FolderType:
            id: auto
            memos: list[DocumentType]

            @orm.field.lazy
            def lazy_memos(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos_of(self)

        @strawberry.type
        class Query:
            @orm.field.eager()
            def eager(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @orm.field.lazy
            def lazy(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @orm.field.lazy(filters=DocumentFilter, order=DocumentOrder)
            def lazy_filtered(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @orm.field.lazy(filters=MemoFilter)
            def lazy_memo_filter(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @orm.field.lazy
            async def lazy_async(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @strawberry.field
            def plain(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @strawberry.field
            async def plain_async(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @strawberry.field
            def rows(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.read(suite.memos())

            @strawberry.field
            async def rows_async(self, info: strawberry.Info) -> list[DocumentType]:
                rows = suite.read(suite.memos())
                return await rows if isawaitable(rows) else rows

            @strawberry.field
            def one(self, info: strawberry.Info, title: str) -> DocumentType | None:
                return _then(
                    suite.read(suite.memos()),
                    lambda rows: next(row for row in rows if row.title == title),
                )

            @strawberry.field
            def optimized(self, info: strawberry.Info) -> list[DocumentType]:
                return _then(
                    suite.read(suite.memos()), lambda rows: orm.optimize(rows, info)
                )

            @orm.connection.lazy(ORMListConnection[DocumentType])
            def page(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @strawberry.relay.connection(ORMListConnection[DocumentType])
            def relay_page(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @strawberry.relay.connection(ORMListConnection[DocumentType])
            def relay_rows(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.read(suite.memos())

            @orm.payload.query
            def payload(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            @orm.payload.query
            def payload_rows(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.read(suite.memos())

            @orm.payload.connection()
            def payload_page(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.memos()

            node: relay.Node | None = relay.node()
            nodes: list[relay.Node | None] = relay.node()

            @strawberry.field
            def feed(self, info: strawberry.Info) -> list[relay.Node]:
                return suite.memos()

            @strawberry.field
            def feed_rows(self, info: strawberry.Info) -> list[relay.Node]:
                return suite.read(suite.memos())

            # Controls: the subclass's own type, and a read of the base.
            @orm.field.lazy
            def own(self, info: strawberry.Info) -> list[MemoType]:
                return suite.memos()

            @orm.field.lazy
            def documents(self, info: strawberry.Info) -> list[DocumentType]:
                return suite.documents()

            folders: list[FolderType] = orm.field.eager()

        return orm.schema(query=Query, optimizer=optimizer, types=[MemoType])

    async def titles(self, orm, models, field, *, optimizer, args=""):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(schema, f"{{ {field}{args} {{ title }} }}")
        return data[field]

    # -- lists the library reads ---------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize(
        "field", ["eager", "lazy", "lazyFiltered", "lazyMemoFilter", "plain"]
    )
    async def test_a_query_returned_as_the_base_type_is_held_to_both_scopes(
        self, orm, models, field, optimizer
    ):
        titles = await self.titles(orm, models, field, optimizer=optimizer)
        assert titles == PUBLIC, titles

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("field", ["eager", "lazyFiltered"])
    async def test_a_filtered_query_is_held_to_both_scopes(
        self, orm, models, field, optimizer
    ):
        titles = await self.titles(
            orm, models, field, optimizer=optimizer, args=FILTERED
        )
        assert titles == PUBLIC, titles

    async def test_the_subclass_filter_is_held_to_the_base_scope(self, orm, models):
        titles = await self.titles(
            orm,
            models,
            "lazyMemoFilter",
            optimizer=True,
            args='(filter: {field: {title: {contains: "a"}}})',
        )
        assert titles == [], titles

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("field", ["lazyAsync", "plainAsync", "rowsAsync"])
    async def test_an_async_resolver_is_held_to_both_scopes(
        self, orm, models, field, optimizer
    ):
        self.needs_async()
        titles = await self.titles(orm, models, field, optimizer=optimizer)
        assert titles == PUBLIC, titles

    # -- rows the resolver read ----------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("field", ["rows", "optimized"])
    async def test_rows_returned_as_the_base_type_are_held_to_both_scopes(
        self, orm, models, field, optimizer
    ):
        titles = await self.titles(orm, models, field, optimizer=optimizer)
        assert titles == PUBLIC, titles

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_one_row_returned_as_the_base_type_is_held_to_both_scopes(
        self, orm, models, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema,
            '{ public: one(title: "public") { title } '
            'secret: one(title: "secret") { title } '
            'draft: one(title: "draft") { title } }',
        )
        assert data == {
            "public": {"title": "public"},
            "secret": None,
            "draft": None,
        }, data

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_rows_inheriting_the_base_scope_are_checked_as_themselves(
        self, orm, models, optimizer
    ):
        """A memo type with no scope of its own holds memos to the base's, and
        they are looked up among memos, which may be keyed apart."""
        suite = self

        @orm.type(models.Document)
        class DocumentType:
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return suite.published_only(query)

        @orm.type(models.Memo)
        class MemoType:
            title: auto

        @strawberry.type
        class Query:
            @strawberry.field
            def rows(self, info: strawberry.Info) -> list[MemoType]:
                return suite.read(suite.memos())

        schema = orm.schema(query=Query, optimizer=optimizer, types=[DocumentType])
        assert orm._backend.scope_owner(models.Memo) is models.Document
        data = await self.execute(schema, "{ rows { title } }")
        assert data == {"rows": [{"title": "public"}, {"title": "secret"}]}, data

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_row_read_as_both_types_in_one_execution_is_held_to_each(
        self, orm, models, optimizer
    ):
        """Passing ``MemoType``'s scope says nothing of ``DocumentType``'s."""
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(schema, "{ own { title } rows { title } }")
        assert data == {
            "own": [{"title": title} for title in self.own_read],
            "rows": PUBLIC,
        }, data

    # -- connections ----------------------------------------------------------

    @pytest.mark.parametrize(
        ("field", "optimizer"),
        [
            ("page", True),
            ("page", False),
            ("relayPage", True),
            ("relayPage", False),
            ("relayRows", True),
            ("relayRows", False),
        ],
    )
    async def test_a_connection_neither_lists_nor_counts_them(
        self, orm, models, field, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema,
            f"{{ all: {field} {{ totalCount edges {{ node {{ title }} }} }} "
            f"first: {field}(first: 1) {{ totalCount pageInfo {{ hasNextPage }} }} }}",
        )
        assert data == {
            "all": {"totalCount": 1, "edges": [{"node": {"title": "public"}}]},
            "first": {"totalCount": 1, "pageInfo": {"hasNextPage": False}},
        }, data

    # -- payloads -------------------------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("field", ["payload", "payloadRows"])
    async def test_a_payload_is_held_to_both_scopes(
        self, orm, models, field, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(schema, f"{{ {field} {{ data {{ title }} }} }}")
        assert data == {field: {"data": PUBLIC}}, data

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_payload_connection_neither_lists_nor_counts_them(
        self, orm, models, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema,
            "{ payloadPage { data { totalCount edges { node { title } } } } }",
        )
        assert data == {
            "payloadPage": {
                "data": {"totalCount": 1, "edges": [{"node": {"title": "public"}}]}
            }
        }, data

    # -- Relay nodes and interfaces -------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_node_by_the_base_types_id_is_held_to_both_scopes(
        self, orm, models, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        selection = "{ ... on DocumentType { title } }"
        data = await self.execute(
            schema,
            f'{{ public: node(id: "{_gid(1)}") {selection} '
            f'secret: node(id: "{_gid(2)}") {selection} '
            f'draft: node(id: "{_gid(3)}") {selection} '
            f'nodes(ids: ["{_gid(3)}", "{_gid(1)}", "{_gid(2)}"]) {selection} }}',
        )
        assert data == {
            "public": {"title": "public"},
            "secret": None,
            "draft": None,
            "nodes": [None, {"title": "public"}, None],
        }, data

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("field", ["feed", "feedRows"])
    async def test_an_interface_reading_them_as_the_base_type_holds_both_scopes(
        self, orm, models, field, optimizer
    ):
        """``MemoType`` is not a ``Node``, so a memo under ``Node`` is a ``DocumentType``."""
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema, f"{{ {field} {{ ... on DocumentType {{ title }} }} }}"
        )
        assert data == {field: PUBLIC}, data

    # -- relations ------------------------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    @pytest.mark.parametrize("field", ["memos", "lazyMemos"])
    async def test_a_relation_typed_as_the_base_type_is_held_to_both_scopes(
        self, orm, models, field, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(schema, f"{{ folders {{ {field} {{ title }} }} }}")
        assert data == {"folders": [{field: PUBLIC}, {field: []}]}, data

    # -- controls -------------------------------------------------------------

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_the_subclass_type_alone_is_held_to_each_scope_over_its_tables(
        self, orm, models, optimizer
    ):
        titles = await self.titles(orm, models, "own", optimizer=optimizer)
        assert titles == [{"title": title} for title in self.own_read], titles

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_read_of_the_base_is_held_to_what_its_rows_are(
        self, orm, models, optimizer
    ):
        titles = await self.titles(orm, models, "documents", optimizer=optimizer)
        expected = [{"title": title} for title in self.documents_read]
        assert titles == expected, titles
