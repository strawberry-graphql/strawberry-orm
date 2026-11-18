"""A row is told apart from every other by its whole primary key (SQLAlchemy)."""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)
from sqlalchemy.orm import Session as SASession

from strawberry_orm import StrawberryORM
from tests.abstract.whole_row_keys import (
    AbstractTestReadsTellRowsApart,
    AbstractTestWritesTellRowsApart,
    KeyedRows,
    resolve,
)


def _models(shape: str) -> SimpleNamespace:
    class Base(DeclarativeBase):
        pass

    class Order(Base):
        __tablename__ = "wr_order"

        id: Mapped[int] = mapped_column(primary_key=True)

    class Tag(Base):
        __tablename__ = "wr_tag"

        id: Mapped[int] = mapped_column(primary_key=True)
        name: Mapped[str]
        lines: Mapped[list["Line"]] = relationship(back_populates="tag")

    if shape == "one_column":

        class Line(Base):
            __tablename__ = "wr_line"

            id: Mapped[int] = mapped_column(primary_key=True)
            label: Mapped[str]
            tag_id: Mapped[int] = mapped_column(ForeignKey("wr_tag.id"))
            tag: Mapped[Tag] = relationship(back_populates="lines")

        class Note(Base):
            __tablename__ = "wr_note"

            id: Mapped[int] = mapped_column(primary_key=True)
            line_id: Mapped[int]
            text: Mapped[str]

    else:
        first = (
            mapped_column(ForeignKey("wr_order.id"), primary_key=True)
            if shape == "foreign_key_and_column"
            else mapped_column(primary_key=True)
        )

        class Line(Base):  # type: ignore[no-redef]
            __tablename__ = "wr_line"

            a: Mapped[int] = first
            b: Mapped[int] = mapped_column(primary_key=True)
            label: Mapped[str]
            tag_id: Mapped[int] = mapped_column(ForeignKey("wr_tag.id"))
            tag: Mapped[Tag] = relationship(back_populates="lines")
            if shape == "foreign_key_and_column":
                order: Mapped[Order] = relationship()

        class Note(Base):  # type: ignore[no-redef]
            __tablename__ = "wr_note"

            id: Mapped[int] = mapped_column(primary_key=True)
            line_a: Mapped[int]
            line_b: Mapped[int]
            text: Mapped[str]

    return SimpleNamespace(Base=Base, Order=Order, Tag=Tag, Line=Line, Note=Note)


SHAPES = {
    shape: _models(shape)
    for shape in ("two_columns", "foreign_key_and_column", "one_column")
}


def _seed(m: SimpleNamespace, keys: dict[str, Any]) -> list[Any]:
    def line(label: str, tag: int) -> Any:
        key = keys[label]
        named = (
            {"id": key} if isinstance(key, int) else dict(zip("ab", key, strict=True))
        )
        return m.Line(**named, label=label, tag_id=tag)

    def note(id: int, label: str, text: str) -> Any:
        key = keys[label]
        if isinstance(key, int):
            return m.Note(id=id, line_id=key, text=text)
        return m.Note(id=id, line_a=key[0], line_b=key[1], text=text)

    return [
        m.Order(id=1),
        m.Order(id=2),
        m.Tag(id=1, name="plain"),
        m.Tag(id=2, name="hush"),
        m.Tag(id=3, name="other"),
        line("shown", 1),
        line("secret", 2),
        line("other", 3),
        note(1, "shown", "first"),
        note(2, "secret", "second"),
    ]


@dataclass
class SQLAlchemyKeyedRows(KeyedRows):
    session: Any = None
    sessions: Any = None
    is_async: bool = False

    @property
    def context(self) -> dict[str, Any]:
        return {"session": self.session}

    async def run(self, schema: Any, query: str, variables: Any = None) -> Any:
        if self.is_async:
            return await super().run(schema, query, variables)
        return schema.execute_sync(
            query, variable_values=variables, context_value=self.context
        )

    def orm(self, **kwargs: Any) -> Any:
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **kwargs
        )

    def hide_secret(self, query: Any) -> Any:
        Line = self.m.Line
        # A join keeps the scope a subquery rather than a WHERE clause.
        return query.join(Line.tag).where(Line.label != "secret")

    def is_first(self, query: Any, value: bool) -> Any:
        Line = self.m.Line
        last = Line.id != 2 if "id" in self.key_names else Line.b == 1
        return query.where(last == value)

    def notes_of(self, line: Any) -> Any:
        Note = self.m.Note
        if "id" in self.key_names:
            matched = [Note.line_id == line.id]
        else:
            matched = [Note.line_a == line.a, Note.line_b == line.b]
        return select(Note).where(*matched).order_by(Note.id)

    async def held(self) -> dict[str, Any]:
        async with self._apart() as session:
            rows = await resolve(session.scalars(select(self.m.Line)))
            rows = list(rows)
            session.expunge_all()
        return {row.label: row for row in rows}

    async def table(self) -> dict[Any, str]:
        async with self._apart() as session:
            rows = list(await resolve(session.scalars(select(self.m.Line))))
        return {_key(row): row.label for row in rows}

    def _apart(self) -> Any:
        """A session of its own, read through and closed."""
        session = self.sessions()

        class Apart:
            async def __aenter__(self):
                return session

            async def __aexit__(self, *exc):
                await resolve(session.close())

        return Apart()


def _key(row: Any) -> Any:
    from sqlalchemy import inspect

    identity = inspect(row).identity
    return identity[0] if len(identity) == 1 else identity


@pytest_asyncio.fixture(
    params=[(shape, mode) for shape in SHAPES for mode in ("sync", "async")],
    ids=lambda param: f"{param[0]}-{param[1]}",
)
async def keyed(request):
    shape, mode = request.param
    m = SHAPES[shape]
    if shape == "one_column":
        keys: dict[str, Any] = {"shown": 1, "secret": 2, "other": 3}
        ids = {label: str(key) for label, key in keys.items()}
        part_id, key_names, moved = None, {"id"}, ("id", 2)
    else:
        keys = {"shown": (1, 1), "secret": (1, 2), "other": (2, 1)}
        ids, part_id, moved = None, "1", ("b", 2)
        key_names = {"a", "b"} | (
            {"order"} if shape == "foreign_key_and_column" else set()
        )
    values = dict(
        m=m, keys=keys, ids=ids, part_id=part_id, key_names=key_names, moved=moved
    )
    if mode == "sync":
        engine = create_engine("sqlite:///:memory:")
        m.Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with SASession(engine) as seeding:
            seeding.add_all(_seed(m, keys))
            seeding.commit()
        session = sessions()
        yield SQLAlchemyKeyedRows(**values, session=session, sessions=sessions)
        session.close()
        engine.dispose()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(m.Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as seeding:
        seeding.add_all(_seed(m, keys))
        await seeding.commit()
    async with sessions() as session:
        yield SQLAlchemyKeyedRows(
            **values, session=session, sessions=sessions, is_async=True
        )
    await engine.dispose()


class TestReadsTellRowsApart(AbstractTestReadsTellRowsApart):
    pass


class TestWritesTellRowsApart(AbstractTestWritesTellRowsApart):
    pass
