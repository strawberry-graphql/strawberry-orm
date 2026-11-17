"""Rows of a subclass, returned by a field typed with its base model's type.

A field over ``DocumentType`` may hand back rows of ``Memo``, a subclass of
``Document``: a Django proxy or multi-table child, an inherited SQLAlchemy
mapping, a Tortoise model inheriting another. A type over ``Memo`` with a
``scope_rows`` of its own decides which memos a caller sees, so a field that
filters, orders or pages them must not let the base type's scope stand in for
it, with the optimizer or without.

A backend provides ``orm`` (a fresh ORM) and ``models`` (``Document`` and
``Memo``), and on the test class:

- ``run(schema, query)``, executing against the seeded rows;
- ``published_only(query)``, the scope of ``Document``'s type;
- ``no_secrets(query)``, what ``Memo``'s type hides besides;
- ``memos()``, an unexecuted query for every memo, in id order.

Seed: the memos "public", "secret" (published, but hidden by ``Memo``'s type)
and "draft" (not published).
"""

import pytest
import strawberry

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

#: Matches "public" and "secret", so only the scope can tell them apart.
FILTERED = (
    '(filter: {field: {title: {contains: "c"}}}, order: [{field: {title: DESC}}])'
)


class AbstractTestSubclassRowsOfBaseFields:
    async def execute(self, schema, query):
        result = await self.run(schema, query)
        assert result.errors is None, result.errors
        return result.data

    def schema(self, orm, models, *, optimizer):
        published_only, no_secrets, memos = (
            self.published_only,
            self.no_secrets,
            self.memos,
        )
        DocumentFilter = orm.filter(models.Document)
        DocumentOrder = orm.order(models.Document)

        @orm.type(models.Document, filters=DocumentFilter, order=DocumentOrder)
        class DocumentType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return published_only(query)

        @orm.type(models.Memo)
        class MemoType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return no_secrets(published_only(query))

        @strawberry.type
        class Query:
            @orm.field.lazy(filters=DocumentFilter, order=DocumentOrder)
            def filtered(self, info: strawberry.Info) -> list[DocumentType]:
                return memos()

            @orm.field.eager()
            def decorated(self, info: strawberry.Info) -> list[DocumentType]:
                return memos()

            @orm.connection.lazy(ORMListConnection[DocumentType])
            def page(self, info: strawberry.Info) -> list[DocumentType]:
                return memos()

            @orm.field.lazy
            def own(self, info: strawberry.Info) -> list[MemoType]:
                return memos()

        return orm.schema(query=Query, optimizer=optimizer)

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_filtered_field_hides_what_the_subclass_type_hides(
        self, orm, models, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema,
            f"{{ filtered {{ title }} narrowed: filtered{FILTERED} {{ title }} }}",
        )
        assert data == {
            "filtered": [{"title": "public"}],
            "narrowed": [{"title": "public"}],
        }

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_decorated_field_hides_what_the_subclass_type_hides(
        self, orm, models, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema,
            f"{{ decorated {{ title }} narrowed: decorated{FILTERED} {{ title }} }}",
        )
        assert data == {
            "decorated": [{"title": "public"}],
            "narrowed": [{"title": "public"}],
        }

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_a_connection_neither_lists_nor_counts_them(
        self, orm, models, optimizer
    ):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(
            schema,
            "{ page { totalCount edges { node { title } } } "
            "first: page(first: 1) { totalCount pageInfo { hasNextPage } } }",
        )
        assert data == {
            "page": {"totalCount": 1, "edges": [{"node": {"title": "public"}}]},
            "first": {"totalCount": 1, "pageInfo": {"hasNextPage": False}},
        }

    @pytest.mark.parametrize("optimizer", [True, False])
    async def test_the_subclass_type_reads_them_alike(self, orm, models, optimizer):
        schema = self.schema(orm, models, optimizer=optimizer)
        data = await self.execute(schema, "{ own { title } }")
        assert data == {"own": [{"title": "public"}]}
