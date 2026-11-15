"""A row keyed by more than one column is matched by its whole key.

A custom filter below the top level, a filter through a scoped relation, and
``groupBy`` each match rows by key against a subquery of the rows they keep.
Matching on the first key column alone takes in every row that shares it.

The seed: order 1 has lines 1 ("public", note "first") and 2 ("secret",
note "second"), order 2 line 1 ("public"). The line type hides "secret" in
the tests that scope it.
"""

import pytest
import strawberry
from sqlalchemy import ForeignKey, ForeignKeyConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.orm import Session as SASession

from strawberry_orm import StrawberryORM, filter_field
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "ck_order"

    id: Mapped[int] = mapped_column(primary_key=True)
    lines: Mapped[list["Line"]] = relationship(back_populates="order")


class Line(Base):
    __tablename__ = "ck_line"

    order_id: Mapped[int] = mapped_column(ForeignKey("ck_order.id"), primary_key=True)
    number: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]
    order: Mapped[Order] = relationship(back_populates="lines")
    notes: Mapped[list["Note"]] = relationship(back_populates="line")


class Note(Base):
    __tablename__ = "ck_note"
    __table_args__ = (
        ForeignKeyConstraint(
            ["order_id", "number"], ["ck_line.order_id", "ck_line.number"]
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int]
    number: Mapped[int]
    text: Mapped[str]
    line: Mapped[Line] = relationship(back_populates="notes")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with SASession(engine) as session:
        session.add_all(
            [
                Order(id=1),
                Order(id=2),
                Line(order_id=1, number=1, label="public"),
                Line(order_id=1, number=2, label="secret"),
                Line(order_id=2, number=1, label="public"),
                Note(id=1, order_id=1, number=1, text="first"),
                Note(id=2, order_id=1, number=2, text="second"),
            ]
        )
        session.commit()
        yield session
    engine.dispose()


def _schema(*, scoped: bool):
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

    @orm.filter_type(Line)
    class LineFilter:
        label: auto

        @filter_field
        def first(self, value: bool, query):
            return query.where((Line.number == 1) == value)

    @orm.type(Note, group=orm.group(Note))
    class NoteType:
        text: auto

    @orm.type(Line, filters=LineFilter, group=orm.group(Line))
    class LineType:
        order_id: auto
        number: auto
        label: auto
        notes: list[NoteType]

        if scoped:

            @classmethod
            def scope_rows(cls, query, info):
                # A join keeps the scope a subquery rather than a WHERE clause.
                return query.join(Line.order).where(Line.label != "secret")

    @orm.type(Order, filters=orm.filter(Order))
    class OrderType:
        id: auto
        lines: list[LineType]

    @strawberry.type
    class Query:
        lines: list[LineType] = orm.field.eager()
        line_page: ORMListConnection[LineType] = orm.connection.eager()
        orders: list[OrderType] = orm.field.eager()

    return orm.schema(query=Query)


def _run(session, schema, query):
    result = schema.execute_sync(query, context_value={"session": session})
    assert result.errors is None, result.errors
    return result.data


@pytest.mark.parametrize("condition", ["not: {first: true}", "any: [{first: false}]"])
def test_a_nested_custom_filter_matches_the_rows_it_kept(session, condition):
    data = _run(
        session,
        _schema(scoped=False),
        "{ lines(filter: {" + condition + "}) { orderId number } }",
    )
    assert data["lines"] == [{"orderId": 1, "number": 2}], data


def test_groups_count_only_the_rows_the_scope_shows(session):
    data = _run(
        session,
        _schema(scoped=True),
        "{ linePage(groupBy: [{field: {label: true}}]) "
        "{ groups { key { label } aggregates { count } } } }",
    )
    groups = data["linePage"]["groups"]
    assert groups == [{"key": {"label": "public"}, "aggregates": {"count": 2}}], groups


def test_groups_across_a_to_many_relation_keep_each_row_to_its_own(session):
    data = _run(
        session,
        _schema(scoped=False),
        "{ linePage(groupBy: [{object: {notes: {field: {text: true}}}}]) "
        "{ groups { key { notes { text } } aggregates { count } } } }",
    )
    found = {
        group["key"]["notes"]["text"]: group["aggregates"]["count"]
        for group in data["linePage"]["groups"]
    }
    assert found == {None: 1, "first": 1, "second": 1}, found


def test_a_filter_through_a_relation_skips_the_rows_its_scope_hides(session):
    data = _run(
        session,
        _schema(scoped=True),
        '{ orders(filter: {object: {lines: {field: {label: {exact: "secret"}}}}}) '
        "{ id } }",
    )
    assert data["orders"] == [], data
