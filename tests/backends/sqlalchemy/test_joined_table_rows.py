"""Joined-table subclasses narrowed in SQL the library writes (SQLAlchemy).

Each parent's ``open_rows`` (``titled_rows`` on its base's column) leaves out
row 2, as its ``primaryjoin`` says. ``JtTask``'s base tells its rows apart by
a discriminator in its own table; ``JtJob``'s has none, and ``JtJob`` keeps
the ``id`` attribute of its base, which reads the base's table alone.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKey, String, and_, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    aliased,
    mapped_column,
    relationship,
    sessionmaker,
)

from tests.abstract.joined_table_rows import (
    AbstractTestJoinedTableRows,
    Shape,
    data,
    schema_for,
    seed,
    titles,
)
from tests.backends.sqlalchemy.test_write_path_authorization import SQLAlchemyWriteDb


class Base(DeclarativeBase):
    pass


# -- A joined subclass, keyed by an attribute of its own -----------------------


class JtTaskBoard(Base):
    __tablename__ = "jt_task_board"

    id: Mapped[int] = mapped_column(primary_key=True)
    rows: Mapped[list[JtTask]] = relationship(
        back_populates="owner", overlaps="open_rows,titled_rows"
    )
    open_rows: Mapped[list[JtTask]] = relationship(
        primaryjoin=lambda: and_(
            JtTaskBoard.id == JtTask.owner_id, JtTask.done.is_(False)
        ),
        overlaps="rows,owner,titled_rows",
    )
    titled_rows: Mapped[list[JtTask]] = relationship(
        primaryjoin=lambda: and_(
            JtTaskBoard.id == JtTask.owner_id, JtTask.title != "two"
        ),
        overlaps="rows,owner,open_rows",
    )


class JtItem(Base):
    __tablename__ = "jt_item"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    color: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "item"}


class JtTask(JtItem):
    __tablename__ = "jt_task"
    __mapper_args__ = {"polymorphic_identity": "task"}

    id: Mapped[int] = mapped_column(ForeignKey("jt_item.id"), primary_key=True)
    done: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("jt_task_board.id"))
    owner: Mapped[JtTaskBoard | None] = relationship(
        back_populates="rows", overlaps="open_rows,titled_rows"
    )
    rows: Mapped[list[JtStep]] = relationship(
        back_populates="owner", overlaps="open_rows"
    )
    open_rows: Mapped[list[JtStep]] = relationship(
        primaryjoin=lambda: and_(JtTask.id == JtStep.owner_id, JtStep.done.is_(False)),
        overlaps="rows,owner",
    )


class JtStep(Base):
    __tablename__ = "jt_step"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    color: Mapped[str] = mapped_column(String(50))
    done: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("jt_task.id"))
    owner: Mapped[JtTask | None] = relationship(
        back_populates="rows", overlaps="open_rows"
    )


# -- A joined subclass whose ``id`` is its base's ------------------------------


class JtJobBoard(Base):
    __tablename__ = "jt_job_board"

    id: Mapped[int] = mapped_column(primary_key=True)
    rows: Mapped[list[JtJob]] = relationship(
        back_populates="owner", overlaps="open_rows"
    )
    open_rows: Mapped[list[JtJob]] = relationship(
        primaryjoin=lambda: and_(
            JtJobBoard.id == JtJob.owner_id, JtJob.done.is_(False)
        ),
        overlaps="rows,owner",
    )


class JtEntry(Base):
    __tablename__ = "jt_entry"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    color: Mapped[str] = mapped_column(String(50))


class JtJob(JtEntry):
    __tablename__ = "jt_job"

    job_id: Mapped[int] = mapped_column(ForeignKey("jt_entry.id"), primary_key=True)
    done: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("jt_job_board.id"))
    owner: Mapped[JtJobBoard | None] = relationship(
        back_populates="rows", overlaps="open_rows"
    )


# -- A base holding the rows of its joined subclass ----------------------------


class JtDocBoard(Base):
    __tablename__ = "jt_doc_board"

    id: Mapped[int] = mapped_column(primary_key=True)
    rows: Mapped[list[JtDoc]] = relationship(
        back_populates="owner", overlaps="open_rows"
    )
    open_rows: Mapped[list[JtDoc]] = relationship(
        primaryjoin=lambda: and_(JtDocBoard.id == JtDoc.owner_id, JtDoc.title != "two"),
        overlaps="rows,owner",
    )


class JtDoc(Base):
    __tablename__ = "jt_doc"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    color: Mapped[str] = mapped_column(String(50))
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("jt_doc_board.id"))
    owner: Mapped[JtDocBoard | None] = relationship(
        back_populates="rows", overlaps="open_rows"
    )


class JtNote(JtDoc):
    __tablename__ = "jt_note"

    id: Mapped[int] = mapped_column(ForeignKey("jt_doc.id"), primary_key=True)
    done: Mapped[bool] = mapped_column(default=False)


# -- A single-table subclass ---------------------------------------------------


class JtChoreBoard(Base):
    __tablename__ = "jt_chore_board"

    id: Mapped[int] = mapped_column(primary_key=True)
    rows: Mapped[list[JtChore]] = relationship(
        back_populates="owner", overlaps="open_rows"
    )
    open_rows: Mapped[list[JtChore]] = relationship(
        primaryjoin=lambda: and_(
            JtChoreBoard.id == JtChore.owner_id, JtChore.done.is_(False)
        ),
        overlaps="rows,owner",
    )


class JtThing(Base):
    __tablename__ = "jt_thing"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    color: Mapped[str] = mapped_column(String(50))
    kind: Mapped[str] = mapped_column(String(20))

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "thing"}


class JtChore(JtThing):
    done: Mapped[bool | None] = mapped_column(default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("jt_chore_board.id"))
    owner: Mapped[JtChoreBoard | None] = relationship(
        back_populates="rows", overlaps="open_rows"
    )

    __mapper_args__ = {"polymorphic_identity": "chore"}


# -- A model of one table ------------------------------------------------------


class JtPlainBoard(Base):
    __tablename__ = "jt_plain_board"

    id: Mapped[int] = mapped_column(primary_key=True)
    rows: Mapped[list[JtPlain]] = relationship(
        back_populates="owner", overlaps="open_rows"
    )
    open_rows: Mapped[list[JtPlain]] = relationship(
        primaryjoin=lambda: and_(
            JtPlainBoard.id == JtPlain.owner_id, JtPlain.done.is_(False)
        ),
        overlaps="rows,owner",
    )


class JtPlain(Base):
    __tablename__ = "jt_plain"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    color: Mapped[str] = mapped_column(String(50))
    done: Mapped[bool] = mapped_column(default=False)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("jt_plain_board.id"))
    owner: Mapped[JtPlainBoard | None] = relationship(
        back_populates="rows", overlaps="open_rows"
    )


SHAPES = {
    "joined-own-column": Shape(
        JtTaskBoard, JtTask, "open_rows", 2, lambda q: q.where(JtTask.done.is_(False))
    ),
    "joined-base-column": Shape(
        JtTaskBoard, JtTask, "titled_rows", 2, lambda q: q.where(JtTask.title != "two")
    ),
    "joined-keyed-by-base": Shape(
        JtJobBoard, JtJob, "open_rows", 2, lambda q: q.where(JtJob.done.is_(False))
    ),
    "joined-parent": Shape(
        JtTask,
        JtStep,
        "open_rows",
        2,
        lambda q: q.where(JtStep.done.is_(False)),
        parent_values={"title": "parent", "color": "none"},
    ),
    "base-of-joined-rows": Shape(
        JtDocBoard,
        JtDoc,
        "open_rows",
        2,
        lambda q: q.where(JtDoc.title != "two"),
        made=JtNote,
    ),
    "single-table": Shape(
        JtChoreBoard,
        JtChore,
        "open_rows",
        2,
        lambda q: q.where(JtChore.done.is_(False)),
    ),
    "plain": Shape(
        JtPlainBoard,
        JtPlain,
        "open_rows",
        2,
        lambda q: q.where(JtPlain.done.is_(False)),
    ),
}


@pytest.fixture(params=list(SHAPES))
def shape(request):
    return SHAPES[request.param]


@pytest_asyncio.fixture(params=["sync", "async"])
async def writes(request):
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


class TestJoinedTableRows(AbstractTestJoinedTableRows):
    pass


# -- Read through an alias -------------------------------------------------------
# ``aliased()`` is SQLAlchemy's own, so these have no counterpart on the others.


def _through_an_alias(writes, shape):
    """The schema, its ``rows`` reading an ``aliased()`` copy of the rows' model."""
    return schema_for(
        writes.new_orm(), shape, scoped=True, read=lambda: select(aliased(shape.target))
    )


async def test_a_connection_through_an_alias_reads_only_the_rows_the_scope_shows(
    writes, shape
):
    """The scope names the model's own tables, so the alias keeps the keys of
    the rows it narrows them to; a key the model inherits reads its base's
    table alone."""
    await seed(writes, shape)
    query = "{ rows(first: 10) { totalCount edges { node { title } } } }"
    rows = data(await writes.execute(_through_an_alias(writes, shape), query))["rows"]
    assert rows["totalCount"] == 3
    assert titles(edge["node"] for edge in rows["edges"]) == ["four", "one", "three"]


async def test_groups_through_an_alias_count_only_the_rows_the_scope_shows(
    writes, shape
):
    """Each group is read over the model, by the keys of the rows the alias
    reads; read off the model, those keys would be tied to the group's own row
    and take in every row of the table."""
    await seed(writes, shape)
    query = (
        "{ rows(first: 10, groupBy: [{ field: { color: true } }]) { "
        "groups { key { color } aggregates { count } "
        "items(first: 5) { edges { node { title } } } } } }"
    )
    rows = data(await writes.execute(_through_an_alias(writes, shape), query))["rows"]
    groups = [
        (
            group["key"]["color"],
            group["aggregates"]["count"],
            titles(edge["node"] for edge in group["items"]["edges"]),
        )
        for group in rows["groups"]
    ]
    assert groups == [("blue", 2, ["four", "three"]), ("red", 1, ["one"])]
