"""Rows a resolver reads under another name than its model's (SQLAlchemy).

The scope, a client's filters and orders, groups and aggregates are written
over the model's own tables. ``aliased()``, a subquery or CTE read as the
model, an aliased ``with_polymorphic()`` and a select of a base of the type's
model read them under another name or not at all; a clause written over the
model's tables would read a copy of them joined to nothing, and hold for every
row as soon as one row of the table met it, a row the scope hides as much as
any.

Each type hides row "two", the only one ranked 9, the only one on the "secret"
board, and one of the two blue rows. ``ArTask`` keeps its rank in its own
table, and ``ArItem``, its base, has none.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    aliased,
    mapped_column,
    relationship,
    sessionmaker,
    with_polymorphic,
)
from strawberry import relay

from strawberry_orm import Ordering, StringLookup, filter_field
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


class ArBoard(Base):
    __tablename__ = "ar_board"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(20))


class ArPlain(Base):
    __tablename__ = "ar_plain"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(20))
    color: Mapped[str] = mapped_column(String(20))
    rank: Mapped[int]
    done: Mapped[bool] = mapped_column(default=False)
    board_id: Mapped[int] = mapped_column(ForeignKey("ar_board.id"))
    board: Mapped[ArBoard] = relationship()


class ArItem(Base):
    __tablename__ = "ar_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(20))
    color: Mapped[str] = mapped_column(String(20))
    board_id: Mapped[int] = mapped_column(ForeignKey("ar_board.id"))
    board: Mapped[ArBoard] = relationship()

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "item"}


class ArTask(ArItem):
    __tablename__ = "ar_task"

    id: Mapped[int] = mapped_column(ForeignKey("ar_item.id"), primary_key=True)
    rank: Mapped[int]
    done: Mapped[bool] = mapped_column(default=False)

    __mapper_args__ = {"polymorphic_identity": "task"}


ROWS = [
    # title, color, rank, done, board
    ("one", "red", 3, False, 1),
    ("two", "blue", 9, True, 2),
    ("three", "blue", 1, False, 1),
    ("four", "red", 2, False, 1),
]


@dataclass(frozen=True)
class Read:
    """What a resolver returns for a type over *model*, hiding row "two"."""

    model: type
    read: Callable[[], Any]
    hides: Callable[[Any], Any]

    @property
    def ranked(self) -> bool:
        return hasattr(self.model, "rank")


def _hide_done(model: type) -> Callable[[Any], Any]:
    return lambda query: query.where(model.done.is_(False))


def _hide_two(query: Any) -> Any:
    return query.where(ArItem.title != "two")


def _ordered(entity: Any, *by: str) -> Any:
    return select(entity).order_by(*(getattr(entity, name) for name in by))


PLAIN = _hide_done(ArPlain)
TASK = _hide_done(ArTask)

READS = {
    "plain": Read(ArPlain, lambda: select(ArPlain), PLAIN),
    "plain-aliased": Read(ArPlain, lambda: select(aliased(ArPlain)), PLAIN),
    "plain-aliased-subquery": Read(
        ArPlain, lambda: select(aliased(ArPlain, select(ArPlain).subquery())), PLAIN
    ),
    "plain-aliased-cte": Read(
        ArPlain, lambda: select(aliased(ArPlain, select(ArPlain).cte())), PLAIN
    ),
    "task": Read(ArTask, lambda: select(ArTask), TASK),
    "task-aliased": Read(ArTask, lambda: select(aliased(ArTask)), TASK),
    "task-aliased-flat": Read(ArTask, lambda: select(aliased(ArTask, flat=True)), TASK),
    "task-from-base": Read(ArTask, lambda: select(ArItem), TASK),
    "item": Read(ArItem, lambda: select(ArItem), _hide_two),
    "item-aliased": Read(ArItem, lambda: select(aliased(ArItem)), _hide_two),
    "item-polymorphic": Read(
        ArItem, lambda: select(with_polymorphic(ArItem, [ArTask])), _hide_two
    ),
    "item-polymorphic-aliased": Read(
        ArItem,
        lambda: select(with_polymorphic(ArItem, [ArTask], aliased=True)),
        _hide_two,
    ),
    "item-polymorphic-flat": Read(
        ArItem,
        lambda: select(with_polymorphic(ArItem, [ArTask], aliased=True, flat=True)),
        _hide_two,
    ),
}
RANKED = {name: read for name, read in READS.items() if read.ranked}


def _aliases(by: str) -> dict[str, Read]:
    """Reads ordered by their own *by*, through each kind of alias, and the class."""
    return {
        "plain": Read(ArPlain, lambda: _ordered(ArPlain, by), PLAIN),
        "plain-aliased": Read(ArPlain, lambda: _ordered(aliased(ArPlain), by), PLAIN),
        "plain-aliased-subquery": Read(
            ArPlain,
            lambda: _ordered(aliased(ArPlain, select(ArPlain).subquery()), by),
            PLAIN,
        ),
        "task-aliased-flat": Read(
            ArTask, lambda: _ordered(aliased(ArTask, flat=True), by), TASK
        ),
        "item-polymorphic-aliased": Read(
            ArItem,
            lambda: _ordered(
                with_polymorphic(ArItem, [ArTask], aliased=True, flat=True), by
            ),
            _hide_two,
        ),
    }


def schema_for(orm: Any, case: Read) -> Any:
    """``rows`` and ``listed`` read what *case* reads, as ``Row``."""
    model = case.model

    @orm.type(ArBoard, filters=orm.filter(ArBoard), name="Board")
    class Board:
        id: auto
        name: auto

    @orm.filter_type(model)
    class RowFilter:
        title: auto
        color: auto
        board: auto
        if case.ranked:
            rank: auto

        @filter_field
        def color_is(self, value: str, query):
            return query.where(model.color == value)

    @orm.type(
        model,
        filters=RowFilter,
        order=orm.order(model),
        group=orm.group(model),
        name="Row",
    )
    class Row(relay.Node):
        id: relay.NodeID[int]
        title: auto
        color: auto
        board: Board
        if case.ranked:
            rank: auto

        @classmethod
        def scope_rows(cls, query, info):
            return case.hides(query)

    def read_rows(info: strawberry.Info) -> list[Row]:
        return case.read()

    @strawberry.type
    class Query:
        rows: ORMListConnection[Row] = orm.connection(
            ORMListConnection[Row], resolver=read_rows
        )

        @orm.field.lazy(filters=RowFilter, order=orm.order(model))
        def listed(self, info: strawberry.Info) -> list[Row]:
            return case.read()

    return orm.schema(query=Query)


async def seed(db: SQLAlchemyWriteDb) -> None:
    db.session.add_all([ArBoard(id=1, name="open"), ArBoard(id=2, name="secret")])
    for key, (title, color, rank, done, board) in enumerate(ROWS, 1):
        values = {"id": key, "title": title, "color": color, "board_id": board}
        db.session.add(ArPlain(rank=rank, done=done, **values))
        db.session.add(ArTask(rank=rank, done=done, **values))
    await db.create(ArBoard, id=3, name="spare")


async def run(db: SQLAlchemyWriteDb, case: Read, query: str) -> Any:
    await seed(db)
    result = await db.execute(schema_for(db.new_orm(), case), query)
    assert result.errors is None, f"errors: {result.errors}"
    return result.data


def titles(connection: dict[str, Any]) -> list[str]:
    return [edge["node"]["title"] for edge in connection["edges"]]


@pytest.fixture(params=list(READS))
def case(request) -> Read:
    return READS[request.param]


@pytest.fixture(params=list(RANKED))
def ranked(request) -> Read:
    return RANKED[request.param]


@pytest_asyncio.fixture(params=["sync", "async"])
async def db(request):
    if request.param == "sync":
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        yield SQLAlchemyWriteDb(None, async_hooks=False, session=session)
        session.close()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield SQLAlchemyWriteDb(None, async_hooks=True, session=session)
    await engine.dispose()


# -- A client's filters answer for each row ---------------------------------------


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        ('field: { title: { exact: "two" } }', []),
        ('field: { title: { exact: "one" } }', ["one"]),
        ('not: { field: { title: { exact: "one" } } }', ["four", "three"]),
        ('colorIs: "blue"', ["three"]),
        ('not: { colorIs: "red" }', ["three"]),
        ('object: { board: { field: { name: { exact: "secret" } } } }', []),
        (
            'any: [{ colorIs: "blue" }, { field: { title: { exact: "two" } } }]',
            ["three"],
        ),
    ],
)
async def test_a_filter_answers_for_the_rows_the_scope_shows(
    db, case, condition, expected
):
    """Row "two" alone meets the hidden-only conditions; read off a copy of the
    table joined to nothing, they would hold for every row."""
    query = (
        f"{{ rows(first: 10, filter: {{ {condition} }}) "
        "{ totalCount edges { node { title } } } "
        f"listed(filter: {{ {condition} }}) {{ title }} }}"
    )
    found = await run(db, case, query)
    assert sorted(titles(found["rows"])) == expected
    assert found["rows"]["totalCount"] == len(expected)
    assert sorted(row["title"] for row in found["listed"]) == expected


async def test_a_filter_on_a_column_of_the_subclass_table(db, ranked):
    """``ArTask`` keeps its rank in its own table, which an alias of it, or a
    select of its base, reads under another name or not at all."""
    query = (
        "{ hidden: rows(first: 10, filter: { field: { rank: { exact: 9 } } }) "
        "{ totalCount edges { node { title } } } "
        "shown: rows(first: 10, filter: { field: { rank: { lte: 2 } } }) "
        "{ totalCount edges { node { title } } } }"
    )
    found = await run(db, ranked, query)
    assert (found["hidden"]["totalCount"], titles(found["hidden"])) == (0, [])
    assert sorted(titles(found["shown"])) == ["four", "three"]


# -- Orders, pages, groups and aggregates -----------------------------------------


async def test_a_client_order_orders_the_rows(db, case):
    query = (
        "{ rows(first: 10, order: [{ field: { title: ASC } }]) "
        "{ edges { node { title } } } "
        "listed(order: [{ field: { title: DESC } }]) { title } }"
    )
    found = await run(db, case, query)
    assert titles(found["rows"]) == ["four", "one", "three"]
    assert [row["title"] for row in found["listed"]] == ["three", "one", "four"]


async def test_an_order_on_a_column_of_the_subclass_table(db, ranked):
    query = "{ rows(first: 10, order: [{ field: { rank: DESC } }]) { edges { node { title } } } }"
    assert titles((await run(db, ranked, query))["rows"]) == ["one", "four", "three"]


@pytest.mark.parametrize("name", list(_aliases("title")))
async def test_the_resolvers_own_order_holds(db, name):
    """Read as the model, the alias's rows keep the order it put them in."""
    case = _aliases("title")[name]
    query = "{ rows(first: 10) { edges { node { title } } } }"
    assert titles((await run(db, case, query))["rows"]) == ["four", "one", "three"]


@pytest.mark.parametrize("name", list(_aliases("title")))
async def test_the_resolvers_own_limit_holds(db, name):
    """The first two rows the scope shows by title are "four" and "one". The
    scope and a client's filter go on before the limit, as they do on a select
    of the class."""
    base = _aliases("title")[name]
    case = Read(base.model, lambda: base.read().limit(2), base.hides)
    query = (
        "{ all: rows(first: 10) { totalCount edges { node { title } } } "
        'red: rows(first: 10, filter: { colorIs: "red" }) { edges { node { title } } } '
        'blue: rows(first: 10, filter: { colorIs: "blue" }) { edges { node { title } } } }'
    )
    found = await run(db, case, query)
    assert (found["all"]["totalCount"], titles(found["all"])) == (2, ["four", "one"])
    assert titles(found["red"]) == ["four", "one"]
    assert titles(found["blue"]) == ["three"]


async def test_pages_follow_one_another(db, case):
    first = (
        "{ rows(first: 2, order: [{ field: { title: ASC } }]) "
        "{ totalCount pageInfo { hasNextPage endCursor } edges { node { title } } } }"
    )
    found = (await run(db, case, first))["rows"]
    assert (found["totalCount"], titles(found)) == (3, ["four", "one"])
    assert found["pageInfo"]["hasNextPage"] is True
    cursor = found["pageInfo"]["endCursor"]
    after = (
        f'{{ rows(first: 2, after: "{cursor}", order: [{{ field: {{ title: ASC }} }}]) '
        "{ edges { node { title } } } }"
    )
    result = await db.execute(schema_for(db.new_orm(), case), after)
    assert result.errors is None, f"errors: {result.errors}"
    assert titles(result.data["rows"]) == ["three"]


async def test_groups_count_only_the_rows_the_scope_shows(db, case):
    query = (
        "{ rows(first: 10, groupBy: [{ field: { color: true } }]) { "
        "groups { key { color } aggregates { count } "
        "items(first: 5) { edges { node { title } } } } } }"
    )
    groups = [
        (
            group["key"]["color"],
            group["aggregates"]["count"],
            sorted(titles(group["items"])),
        )
        for group in (await run(db, case, query))["rows"]["groups"]
    ]
    assert groups == [("blue", 1, ["three"]), ("red", 2, ["four", "one"])]


async def test_aggregates_count_only_the_rows_the_scope_shows(db, ranked):
    query = (
        '{ rows(first: 10, filter: { not: { colorIs: "green" } }) '
        "{ aggregates { count max { rank } } } }"
    )
    found = (await run(db, ranked, query))["rows"]["aggregates"]
    assert (found["count"], found["max"]["rank"]) == (3, 3)


# -- What the rows are read as ------------------------------------------------------


async def test_a_base_select_reads_only_the_rows_of_the_type(db):
    """A select of ``ArItem`` read for ``ArTask``'s type holds only the items
    that are tasks."""
    await db.create(ArItem, id=5, kind="item", title="five", color="red", board_id=1)
    query = (
        '{ rows(first: 10, filter: { colorIs: "red" }) '
        "{ totalCount edges { node { title } } } }"
    )
    found = await run(db, READS["task-from-base"], query)
    assert (found["rows"]["totalCount"], sorted(titles(found["rows"]))) == (
        2,
        ["four", "one"],
    )


async def test_rows_of_another_model_are_refused(db):
    """Not the type's model, nor one it inherits or is inherited by: its keys
    say nothing about the type's rows."""
    case = Read(ArPlain, lambda: select(ArBoard), PLAIN)
    await seed(db)
    result = await db.execute(
        schema_for(db.new_orm(), case),
        '{ rows(first: 10, filter: { colorIs: "red" }) { totalCount } }',
    )
    assert result.errors is not None
    assert "ArBoard" in result.errors[0].message
    assert "ArPlain" in result.errors[0].message


def test_apply_filters_and_ordering_read_an_alias_as_its_model():
    """``orm.apply_filters`` and ``orm.apply_ordering`` add a client's clauses
    the same way the fields do."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    db = SQLAlchemyWriteDb(None, async_hooks=False, session=session)
    orm = db.new_orm()
    session.add_all([ArBoard(id=1, name="open"), ArBoard(id=2, name="secret")])
    for key, (title, color, rank, done, board) in enumerate(ROWS, 1):
        session.add(
            ArPlain(
                id=key, title=title, color=color, rank=rank, done=done, board_id=board
            )
        )
    session.commit()
    Filter, Order = orm.filter(ArPlain), orm.order(ArPlain)
    only_two = Filter(field=Filter._field_type(title=StringLookup(exact="two")))
    alias = aliased(ArPlain)
    statement = select(alias).where(alias.done.is_(False))
    filtered = orm.apply_filters(statement, only_two, ArPlain)
    assert session.execute(filtered).scalars().all() == []
    by_title = [Order(field=Order._field_type(title=Ordering.DESC))]
    ordered = orm.apply_ordering(statement, by_title, ArPlain)
    found = [row.title for row in session.execute(ordered).scalars()]
    assert found == ["three", "one", "four"]
    # A select of the table reads the columns the class's clauses name.
    table = orm.apply_filters(select(ArPlain.__table__), only_two, ArPlain)
    assert [row.title for row in session.execute(table)] == ["two"]
    session.close()


def test_a_polymorphic_select_of_a_type_without_a_scope_is_left_as_it_is():
    """``with_polymorphic()`` without ``aliased=`` reads the model's own
    tables, and with no scope there is nothing to narrow them by."""
    orm = SQLAlchemyWriteDb(None, async_hooks=False, session=None).new_orm()

    @orm.type(ArItem)
    class Item:
        title: auto

    query = select(with_polymorphic(ArItem, [ArTask]))
    assert orm._backend.apply_type_scope(query, ArItem, None) is query
