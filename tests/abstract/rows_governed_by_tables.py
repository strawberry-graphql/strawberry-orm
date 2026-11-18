"""A subclass's row is governed by the models whose tables hold it.

A model inheriting another may share its rows: a Django proxy or multi-table
child, a SQLAlchemy joined-table or single-table subclass. Or it keeps rows of
its own in a table of its own: a SQLAlchemy concrete mapping, under a base with
a table, with a polymorphic union over it, or under an abstract base, and a
Tortoise model inheriting another. A base whose table does not hold the
subclass's rows has no say over them: by their key its table finds another
row, and a scope or repo of its names columns of a table they are not in.

A subclass with no repo or row scope of its own takes its base's, where the
base's governs its rows (see ``BaseBackend.governs``): as rows of the base
where its table holds them; where Django and Tortoise apply the base's to the
subclass's query by field name, as the subclass's own; but not where a
SQLAlchemy base's names the columns of a table that does not hold them. A
model with no table holds no rows, so what is registered for it is the
subclass's own.

Each shape a backend binds is crossed with each read and write that reaches a
subclass row, in :data:`READS` and :data:`WRITES`, and with which of the base
and the subclass hide it, in :data:`HIDERS`. Where the two tables are apart,
the base's table holds a row with the subclass row's key, which only a policy
read against the wrong table would see.

A backend supplies a ``writes`` fixture: a :class:`WriteDb` whose ``m`` holds
``Base``, ``Sub`` inheriting it, ``Shelf``, which lists subclass rows in
``docs``, and ``Pin``, which links one in ``doc``; whose ``shape`` is a
:class:`Shape`; and which gives ``docs()``, an unexecuted query for every
subclass row, ``shelves()``, one for every shelf, and ``read(query)``, the rows
of a query or an awaitable of them.
"""

from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay

from strawberry_orm import AbstractRepo, auto
from strawberry_orm.relay import ORMListConnection
from tests.abstract.write_path_authorization import item, member


@dataclass(frozen=True)
class Shape:
    #: Whether the base's table holds the subclass's rows.
    shares: bool
    #: Whether the base's repo and row scope have a say over the subclass's
    #: rows: a subclass with none of its own takes the base's, and a read as the
    #: base holds the row to the base's scope.
    inherits: bool
    #: Whether the base has a table of its own, apart from the subclass's.
    apart: bool = False
    #: Whether a GraphQL type can be declared over the base.
    typed: bool = True
    #: Whether a GraphQL type can be declared over the subclass.
    readable: bool = True


#: Which models' repos and types hide the subclass row. The other model's show
#: every row; "inherited" gives the subclass no repo or row scope of its own.
HIDERS = ("neither", "base", "sub", "both", "base, inherited")

READS = (
    "root list",
    "relation",
    "relation filter",
    "connection",
    "node",
    "base list",
    "scope check",
)
#: The reads that read the subclass row as the base.
AS_BASE = ("base list", "scope check")
WRITES = (
    "upsert update",
    "upsert create",
    "ref-list update",
    "ref-list delete",
    "nested update",
    "nested delete",
    "link target",
)

SHELF = {
    "_meta": {"update": ["name"], "upsert": {"where": ["id"], "onMatch": "UPDATE"}},
    "docs": {"_meta": {"update": ["title"], "delete": True}},
}
PIN = {
    "_meta": {"update": ["label"], "upsert": {"where": ["id"], "onMatch": "UPDATE"}},
    "doc": {
        "_meta": {"update": ["title"], "upsert": {"where": ["id"], "onMatch": "IGNORE"}}
    },
}


@strawberry.input
class GtDocUpdate:
    id: strawberry.ID
    title: str | None = strawberry.UNSET


def hidden(shape: Shape, hider: str, entry: str | None = None) -> bool:
    """Whether the subclass row reads as missing to *entry* when *hider* hides it.

    A write asks the repo of every model whose table holds the row, and a read
    holds it to the scope of each of their types, however it reads the row.
    A read as the base holds it to the base's scope wherever the base has a
    say over the row (see ``rows_read_as_declared_type``).
    """
    if hider == "base":
        return shape.shares or (entry in AS_BASE and shape.inherits)
    if hider == "base, inherited":
        return shape.inherits
    return hider in ("sub", "both")


def hiding(hider: str) -> tuple[bool, bool | None]:
    """Whether the base's policies hide every row, and the subclass's (``None``: it has none)."""
    base = hider in ("base", "both", "base, inherited")
    sub = None if hider == "base, inherited" else hider in ("sub", "both")
    return base, sub


def governing(writes: Any, model: type, hides: bool) -> type:
    """A repo for *model*, hiding and refusing every row if *hides*.

    Its scope names *model*'s own columns, as a repo bound to a model does.
    """

    class Governing(AbstractRepo):
        def scope_query(self, query, info):
            return writes.only(query, model, []) if hides else query

        def can_create(self, data, info):
            return not hides

        def can_update(self, instance, data, info):
            return not hides

        def can_upsert(self, instance, info):
            return not hides

        def can_delete(self, instance, info):
            return not hides

        def can_link(self, parent, field, instance, info):
            return not hides

        def can_unlink(self, parent, field, instance, info):
            return not hides

    Governing.model = model
    return Governing


def repos(writes: Any, hider: str) -> dict[type, type]:
    m = writes.m
    base, sub = hiding(hider)
    registered = {m.Base: governing(writes, m.Base, base)}
    if sub is not None:
        registered[m.Sub] = governing(writes, m.Sub, sub)
    return registered


def row_scope(writes: Any, model: type, hides: bool) -> Any:
    """A ``scope_rows`` naming *model*'s columns, hiding every row if *hides*."""

    def scope_rows(cls, query, info):
        return writes.only(query, model, []) if hides else query

    return classmethod(scope_rows)


def schema(writes: Any, hider: str) -> Any:
    m, shape = writes.m, writes.shape
    orm = writes.new_orm()
    base, sub = hiding(hider)
    if shape.typed:

        @orm.type(m.Base)
        class GtBaseType:
            id: auto
            title: auto
            scope_rows = row_scope(writes, m.Base, base)

    sub_scope = None if sub is None else row_scope(writes, m.Sub, sub)

    @orm.type(m.Sub, filters=orm.filter(m.Sub))
    class GtDocType(relay.Node):
        id: relay.NodeID[int]
        title: auto
        if sub_scope is not None:
            scope_rows = sub_scope

        @classmethod
        def resolve_nodes(cls, *, info, node_ids, required=False):
            wanted = [int(pk) for pk in node_ids]

            def pick(rows):
                by_id = {row.id: row for row in rows}
                return [by_id.get(pk) for pk in wanted]

            rows = writes.read(writes.docs())
            if isawaitable(rows):

                async def picked():
                    return pick(await rows)

                return picked()
            return pick(rows)

    @orm.type(m.Shelf)
    class GtShelfType:
        id: auto
        docs: list[GtDocType]

    @strawberry.type
    class Query:
        node: relay.Node | None = relay.node()

        @orm.field.lazy
        def docs(self, info: strawberry.Info) -> list[GtDocType]:
            return writes.docs()

        @orm.field.lazy(filters=orm.filter(m.Shelf))
        def shelves(self, info: strawberry.Info) -> list[GtShelfType]:
            return writes.shelves()

        @orm.connection.lazy(ORMListConnection[GtDocType])
        def page(self, info: strawberry.Info) -> list[GtDocType]:
            return writes.docs()

        if shape.typed:
            # Given an order, the field scopes its rows as the base's.
            @orm.field.lazy(order=orm.order(m.Base))
            def bases(self, info: strawberry.Info) -> list[GtBaseType]:
                return writes.docs()

    return orm, orm.schema(query=Query)


def declared(writes: Any, model: type) -> Exception | None:
    """Declare a type over *model* and a schema serving it; what refused it."""
    orm = writes.new_orm()
    try:

        @orm.type(model)
        class GtDeclaredType:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            @orm.field.lazy
            def rows(self, info: strawberry.Info) -> list[GtDeclaredType]:
                return []

        orm.schema(query=Query)
    except Exception as refused:
        return refused
    return None


#: Each read, and what it answers with the subclass row shown and hidden.
QUERIES = {
    "root list": ("{ docs { title } }", {"docs": [{"title": "sub"}]}, {"docs": []}),
    "relation": (
        "{ shelves { docs { title } } }",
        {"shelves": [{"docs": [{"title": "sub"}]}, {"docs": []}]},
        {"shelves": [{"docs": []}, {"docs": []}]},
    ),
    "relation filter": (
        '{ shelves(filter: {object: {docs: {field: {title: {exact: "sub"}}}}}) '
        "{ id } }",
        {"shelves": [{"id": 1}]},
        {"shelves": []},
    ),
    "connection": (
        "{ page { totalCount edges { node { title } } } }",
        {"page": {"totalCount": 1, "edges": [{"node": {"title": "sub"}}]}},
        {"page": {"totalCount": 0, "edges": []}},
    ),
    "node": (
        f'{{ node(id: "{relay.GlobalID("GtDocType", "1")}") '
        "{ ... on GtDocType { title } } }",
        {"node": {"title": "sub"}},
        {"node": None},
    ),
    "base list": ("{ bases { title } }", {"bases": [{"title": "sub"}]}, {"bases": []}),
}


async def state(writes: Any) -> tuple[list, list, list]:
    m = writes.m
    docs = [(row.id, row.title) for row in await writes.rows(m.Sub)]
    shelf = (await writes.rows(m.Shelf))[1]
    listed = [row.id for row in await writes.related(shelf, "docs")]
    pins = [row.doc_id for row in await writes.rows(m.Pin)]
    return docs, listed, pins


async def outcome(write: Any) -> tuple[type | None, str | None]:
    try:
        await write
    except (PermissionError, LookupError) as refused:
        return type(refused), str(refused)
    return None, None


async def write(
    writes: Any, orm: Any, entry: str, shelves: list[Any], pk: str = "1"
) -> None:
    """Write subclass row *pk* through *entry*.

    An update renames it, through shelf 2 where it adds it to a list; a
    delete takes it off shelf 1; a create makes another; a link target is
    linked by pin 1.
    """
    m = writes.m
    operation = entry.split()[-1]
    values = {"title": "edited"} if operation == "update" else {}
    if entry.startswith("upsert"):
        key = "title" if operation == "create" else "id"
        upsert = orm.mutations.upsert_node_input(
            model=m.Sub,
            project={
                "_meta": {
                    "create": ["title"],
                    "update": ["title"],
                    "upsert": {"where": [key], "onMatch": "UPDATE"},
                }
            },
            name="GtDocUpsert",
        )
        where = {"title": "new"} if operation == "create" else {"id": pk}
        payload = upsert(where=member(upsert, "where")(**where))
        if operation == "create":
            payload.create = member(upsert, "create")(title="new")
        else:
            payload.update = member(upsert, "update")(title="edited")
        await writes.upsert(orm, payload)
    elif entry.startswith("ref-list"):
        ref = orm.ref(m.Sub, update=GtDocUpdate, delete=True)
        shelf = shelves[1] if operation == "update" else shelves[0]
        await writes.refs(orm, shelf, "docs", [item(ref, operation, id=pk, **values)])
    elif entry.startswith("nested"):
        upsert = orm.mutations.upsert_node_input(
            model=m.Shelf, project=SHELF, name="GtShelfUpsert"
        )
        items = member(upsert, "update", "docs")
        shelf = "2" if operation == "update" else "1"
        payload = upsert(
            where=member(upsert, "where")(id=shelf),
            update=member(upsert, "update")(
                docs=[item(items, operation, id=pk, **values)]
            ),
        )
        await writes.upsert(orm, payload)
    else:
        upsert = orm.mutations.upsert_node_input(
            model=m.Pin, project=PIN, name="GtPinUpsert"
        )
        link = member(upsert, "update", "doc")
        payload = upsert(
            where=member(upsert, "where")(id="1"),
            update=member(upsert, "update")(
                doc=link(update=member(link, "update")(id=pk))
            ),
        )
        await writes.upsert(orm, payload)


class AbstractTestRowsGovernedByTables:
    """Subclass row 1, titled "sub", is on shelf 1; shelf 2 and pin 1 hold nothing.

    Where the base has a table apart, its own row 1 is titled "base".
    """

    @pytest_asyncio.fixture
    async def shelves(self, writes):
        m = writes.m
        shelves = [await writes.create(m.Shelf, id=pk, name="") for pk in (1, 2)]
        doc = await writes.create(m.Sub, id=1, title="sub")
        await writes.add_related(shelves[0], "docs", doc)
        await writes.create(m.Pin, id=1, label="")
        if writes.shape.apart:
            await writes.create(m.Base, id=1, title="base")
        return shelves

    @staticmethod
    async def check_hidden(
        writes: Any, orm: Any, entry: str, shelves: list[Any]
    ) -> None:
        """Write the subclass row through *entry*, and check it read as missing.

        A create, which looks for no row, is refused.
        """
        before = await state(writes)
        hidden_as = await outcome(write(writes, orm, entry, shelves))
        if entry == "upsert create":
            refused = hidden_as[0]
            assert refused is not None and issubclass(refused, PermissionError), (
                f"the create was not refused: {hidden_as}"
            )
        else:
            refused, message = await outcome(write(writes, orm, entry, shelves, "99"))
            missing = (refused, message and message.replace("99", "1"))
            assert hidden_as == missing, f"the {entry} met {hidden_as}, not {missing}"
        after = await state(writes)
        assert after == before, f"the {entry} left {after} from {before}"

    @pytest.mark.parametrize("hider", HIDERS)
    @pytest.mark.parametrize("entry", READS)
    async def test_a_read_shows_the_row_unless_a_governing_scope_hides_it(
        self, writes, shelves, entry, hider
    ):
        """A read needing a type its shape cannot have fails to declare it."""
        shape = writes.shape
        if not shape.readable:
            refused = declared(writes, writes.m.Sub)
            assert refused is not None, "a type over the subclass was declared"
            return
        if not shape.typed and (hiding(hider)[0] or entry == "base list"):
            refused = declared(writes, writes.m.Base)
            assert refused is not None, "a type over the base was declared"
            return
        orm, built = schema(writes, hider)
        expect_hidden = hidden(shape, hider, entry)
        if entry == "scope check":
            backend = orm._backend
            allowed = await writes.write(
                lambda: backend.filter_scoped_pks(
                    writes.m.Sub, [1], writes.info, read_as=(writes.m.Base,)
                )
            )
            expected = set() if expect_hidden else {1}
            assert set(allowed) == expected, (
                f"the scopes read as the base's allow {allowed}, not {expected}"
            )
            return
        query, shown, missing = QUERIES[entry]
        result = await writes.execute(built, query)
        assert result.errors is None, f"the read failed: {result.errors}"
        expected = missing if expect_hidden else shown
        assert result.data == expected, f"the read gave {result.data}, not {expected}"

    @pytest.mark.parametrize("hider", HIDERS)
    @pytest.mark.parametrize("entry", WRITES)
    async def test_a_write_reaches_the_row_unless_a_governing_repo_hides_it(
        self, writes, shelves, entry, hider
    ):
        orm = writes.new_orm(repos=repos(writes, hider))
        if hidden(writes.shape, hider):
            await self.check_hidden(writes, orm, entry, shelves)
            return
        before = await state(writes)
        await write(writes, orm, entry, shelves)
        after = await state(writes)
        assert after != before, f"the {entry} left {after} as it was"

    @pytest.mark.parametrize("entry", WRITES)
    async def test_a_repo_bound_to_no_model_answers_for_the_subclass(
        self, writes, shelves, entry
    ):
        """Registered for the base alone, it is the subclass's own, scoping its rows."""

        class Unbound(AbstractRepo):
            def scope_query(self, query, info):
                return writes.only(query, self.model, [])

            def can_create(self, data, info):
                return False

        orm = writes.new_orm(repos={writes.m.Base: Unbound})
        await self.check_hidden(writes, orm, entry, shelves)
