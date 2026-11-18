"""Rows of a model mapped over more than one table, narrowed in SQL the library writes.

A SQLAlchemy joined-table subclass reads its base's table and its own. Its key
columns are its base's, and the key attribute it can inherit reads the base's
table alone, as a bare ``EXISTS`` reads only the tables its criteria name. A
statement put together from either, narrowed on the subclass's own columns,
reads one table without the other: the criteria then hold for the whole set
as soon as one row of the other table meets them, so a membership check admits
a row its relation leaves out, a custom filter matches every row or none, and a
scope lets hidden rows into a relation filter, a group or the rows a resolver
returns. Joined back through the entity it selects, each reads what the same
statement over a model with one table does.

Every shape below is seeded alike, so each case expects one answer of all of
them: parents 1 and 2, and rows 1 ("one", red), 2 ("two", red), 3 ("three",
blue) and 4 ("four", blue), the first three parent 1's and the last parent
2's. Row 2 alone is ``done``, and alone titled "two"; the shape's narrowing
column keeps the other three.

Each backend supplies ``writes``, a :class:`WriteDb` in every mode it runs
in, and ``shape``, a :class:`Shape` for each shape of model it maps.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay

from strawberry_orm import UpsertNotFoundError, filter_field
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.write_path_authorization import item, member

ROWS = [
    (1, "one", "red", False, 1),
    (2, "two", "red", True, 1),
    (3, "three", "blue", False, 1),
    (4, "four", "blue", False, 2),
]

#: Every row's ``(id, owner_id)``, as seeded.
SEEDED = [(1, 1), (2, 1), (3, 1), (4, 2)]


@dataclass
class Shape:
    """One shape of model, over the seeded rows."""

    #: The model of parents 1 and 2, whose relation ``rows`` holds every row.
    parent: type
    #: The model the rows are read as.
    target: type
    #: Parent 1's relation that leaves out row *outside*.
    relation: str
    outside: int
    #: *query*, over *target*, narrowed by the shape's column to rows 1, 3 and 4.
    kept: Callable[[Any], Any]
    #: The model the rows are made as.
    made: type | None = None
    #: What else a parent needs to be made.
    parent_values: dict[str, Any] = field(default_factory=dict)


async def seed(writes: Any, shape: Shape) -> None:
    for pk in (1, 2):
        await writes.create(shape.parent, id=pk, **shape.parent_values)
    for pk, title, color, done, owner in ROWS:
        await writes.create(
            shape.made or shape.target,
            id=pk,
            title=title,
            color=color,
            done=done,
            owner_id=owner,
        )


def schema_for(
    orm: Any,
    shape: Shape,
    *,
    scoped: bool,
    returned: tuple[Any, ...] = (),
    read: Callable[[], Any] | None = None,
) -> Any:
    """``rows`` and ``listed`` read the rows, ``parents`` the parents.

    With *scoped*, the rows' type shows only those the shape keeps.
    ``listed`` hands back *returned*, rows already read, and ``rows`` reads
    what *read* returns, when given.
    """
    target = shape.target

    @orm.filter_type(target)
    class RowFilter:
        title: auto

        @filter_field
        def kept(self, value: bool, query):
            return shape.kept(query) if value else query

    @orm.type(target, filters=RowFilter, group=orm.group(target), name="Row")
    class Row(relay.Node):
        id: relay.NodeID[int]
        title: auto
        color: auto

        if scoped:

            @classmethod
            def scope_rows(cls, query, info):
                return shape.kept(query)

    @orm.type(shape.parent, filters=orm.filter(shape.parent), name="Parent")
    class Parent:
        id: auto
        rows: list[Row]

    def read_rows(info: strawberry.Info) -> list[Row]:
        return read()

    @strawberry.type
    class Query:
        rows: ORMListConnection[Row] = orm.connection(
            ORMListConnection[Row], resolver=None if read is None else read_rows
        )
        parents: list[Parent] = orm.field.auto()

        @strawberry.field
        def listed(self) -> list[Row]:
            return list(returned)

    return orm.schema(query=Query)


def data(result: Any) -> Any:
    assert result.errors is None, f"errors: {result.errors}"
    return result.data


def titles(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(row["title"] for row in rows)


def parent_ids(result: Any) -> list[int]:
    return sorted(int(row["id"]) for row in data(result)["parents"])


class AbstractTestJoinedTableRows:
    @pytest_asyncio.fixture
    async def seeded(self, writes, shape):
        await seed(writes, shape)

    @pytest.fixture
    def orm(self, writes):
        return writes.new_orm()

    async def run(self, writes, shape, orm, query, *, scoped=True, returned=()):
        return await writes.execute(
            schema_for(orm, shape, scoped=scoped, returned=returned), query
        )

    async def owners(self, writes, shape):
        return [(row.id, row.owner_id) for row in await writes.rows(shape.target)]

    async def first_parent(self, writes, shape):
        return (await writes.rows(shape.parent))[0]

    # -- Membership ---------------------------------------------------------

    @pytest.mark.parametrize("operation", ["delete", "unlink"])
    async def test_a_ref_list_cannot_reach_a_row_outside_the_relation(
        self, writes, shape, seeded, orm, operation
    ):
        ref = orm.ref(shape.target, delete=True, unlink=True)
        parent = await self.first_parent(writes, shape)
        refused = rf"^No row {shape.outside} in {shape.relation}$"
        with pytest.raises(UpsertNotFoundError, match=refused):
            await writes.refs(
                orm, parent, shape.relation, [item(ref, operation, id=shape.outside)]
            )
        owners = await self.owners(writes, shape)
        assert owners == SEEDED, f"{operation} wrote rows: {owners}"

    async def test_a_nested_list_cannot_reach_a_row_outside_the_relation(
        self, writes, shape, seeded, orm
    ):
        upsert = orm.mutations.upsert_node_input(
            model=shape.parent,
            project={
                "_meta": {
                    "update": [],
                    "upsert": {"where": ["id"], "onMatch": "UPDATE"},
                },
                shape.relation: {"_meta": {"delete": True}},
            },
            name="ParentUpsert",
        )
        rows = member(upsert, "update", shape.relation)
        payload = upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(
                **{shape.relation: [item(rows, "delete", id=str(shape.outside))]}
            ),
        )
        refused = rf"^No row {shape.outside} in {shape.relation}$"
        with pytest.raises(UpsertNotFoundError, match=refused):
            await writes.upsert(orm, payload)
        owners = await self.owners(writes, shape)
        assert owners == SEEDED, f"the nested delete wrote rows: {owners}"

    async def test_rows_in_the_relation_can_still_be_unlinked_and_deleted(
        self, writes, shape, seeded, orm
    ):
        ref = orm.ref(shape.target, delete=True, unlink=True)
        parent = await self.first_parent(writes, shape)
        refs = [item(ref, "unlink", id="1"), item(ref, "delete", id="3")]
        await writes.refs(orm, parent, shape.relation, refs)
        owners = await self.owners(writes, shape)
        assert owners == [(1, None), (2, 1), (4, 2)], f"rows left: {owners}"

    # -- Custom filters -----------------------------------------------------

    async def test_a_negated_custom_filter_keeps_the_rows_it_does_not_match(
        self, writes, shape, seeded, orm
    ):
        query = "{ rows(filter: { not: { kept: true } }) { edges { node { title } } } }"
        result = await self.run(writes, shape, orm, query, scoped=False)
        found = titles(edge["node"] for edge in data(result)["rows"]["edges"])
        assert found == ["two"], f"not kept: {found}"

    async def test_a_custom_filter_in_any_is_one_alternative(
        self, writes, shape, seeded, orm
    ):
        query = (
            "{ rows(filter: { any: [{ not: { kept: true } }, "
            '{ field: { title: { exact: "four" } } }] }) '
            "{ edges { node { title } } } }"
        )
        result = await self.run(writes, shape, orm, query, scoped=False)
        found = titles(edge["node"] for edge in data(result)["rows"]["edges"])
        assert found == ["four", "two"], f"not kept, or four: {found}"

    async def test_a_custom_filter_on_a_related_row(self, writes, shape, seeded, orm):
        query = (
            "{ parents(filter: { object: { rows: { not: { kept: true } } } }) { id } }"
        )
        result = await self.run(writes, shape, orm, query, scoped=False)
        assert parent_ids(result) == [1], f"parents: {data(result)}"

    # -- The rows' scope ----------------------------------------------------

    async def test_a_relation_filter_holds_the_related_rows_to_their_scope(
        self, writes, shape, seeded, orm
    ):
        query = (
            "{ parents(filter: { object: { rows: { field: { title: "
            '{ exact: "%s" } } } } }) { id } }'
        )
        schema = schema_for(orm, shape, scoped=True)
        hidden = await writes.execute(schema, query % "two")
        shown = await writes.execute(schema, query % "three")
        assert parent_ids(hidden) == [], f"through hidden two: {data(hidden)}"
        assert parent_ids(shown) == [1], f"through three: {data(shown)}"

    async def test_a_negated_relation_filter_holds_the_related_rows_to_their_scope(
        self, writes, shape, seeded, orm
    ):
        query = (
            "{ parents(filter: { not: { object: { rows: { field: { title: "
            '{ exact: "two" } } } } } }) { id } }'
        )
        result = await self.run(writes, shape, orm, query)
        assert parent_ids(result) == [1, 2], f"parents: {data(result)}"

    async def test_a_relation_reads_only_the_rows_the_scope_shows(
        self, writes, shape, seeded, orm
    ):
        query = "{ parents { id rows { title } } }"
        result = await self.run(writes, shape, orm, query)
        read = {int(row["id"]): titles(row["rows"]) for row in data(result)["parents"]}
        assert read == {1: ["one", "three"], 2: ["four"]}, f"read: {read}"

    async def test_groups_count_and_list_only_the_rows_the_scope_shows(
        self, writes, shape, seeded, orm
    ):
        query = (
            "{ rows(first: 10, groupBy: [{ field: { color: true } }]) { totalCount "
            "groups { key { color } aggregates { count } "
            "items(first: 5) { edges { node { title } } } } } }"
        )
        result = await self.run(writes, shape, orm, query)
        rows = data(result)["rows"]
        groups = [
            (
                group["key"]["color"],
                group["aggregates"]["count"],
                titles(edge["node"] for edge in group["items"]["edges"]),
            )
            for group in rows["groups"]
        ]
        assert rows["totalCount"] == 3, f"total: {rows['totalCount']}"
        assert groups == [
            ("blue", 2, ["four", "three"]),
            ("red", 1, ["one"]),
        ], f"groups: {groups}"

    async def test_rows_a_resolver_returns_are_held_to_the_scope(
        self, writes, shape, seeded, orm
    ):
        every = tuple(await writes.rows(shape.target))
        query = "{ listed { title } }"
        result = await self.run(writes, shape, orm, query, returned=every)
        found = titles(data(result)["listed"])
        assert found == ["four", "one", "three"], f"listed: {found}"
