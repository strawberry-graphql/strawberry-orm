"""A subclass row read or written through its base is matched by its whole key.

``select(Doc)`` hands back a ``Contract`` for a contract row, held to the
contract type's scope and written through the contract repo. Both are said in
the statement over ``Doc`` as "not a contract, or a contract the scope keeps",
matching rows by key; under a key of several columns the first alone takes in
every row sharing it. Every row here shares it: the document ``shown``, and the
contracts ``secret``, which the contract type and repo hide, and ``other``.
"""

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import ForeignKey, ForeignKeyConstraint, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from strawberry_orm import AbstractRepo, StrawberryORM, UpsertNotFoundError
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from tests.abstract.whole_row_keys import resolve
from tests.abstract.write_path_authorization import member

KEYS = {
    "two_columns": {"shown": (1, 1), "secret": (1, 2), "other": (1, 3)},
    "one_column": {"shown": 1, "secret": 2, "other": 3},
}


def _models(shape: str) -> SimpleNamespace:
    class Base(DeclarativeBase):
        pass

    if shape == "joined_one_column":

        class Doc(Base):
            __tablename__ = "wr_doc"

            id: Mapped[int] = mapped_column(primary_key=True)
            kind: Mapped[str]
            label: Mapped[str]

            __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "doc"}

        class Contract(Doc):
            __tablename__ = "wr_contract"

            id: Mapped[int] = mapped_column(ForeignKey("wr_doc.id"), primary_key=True)

            __mapper_args__ = {"polymorphic_identity": "contract"}

        return SimpleNamespace(Base=Base, Doc=Doc, Contract=Contract)

    class Doc(Base):  # type: ignore[no-redef]
        __tablename__ = "wr_doc"

        a: Mapped[int] = mapped_column(primary_key=True)
        b: Mapped[int] = mapped_column(primary_key=True)
        kind: Mapped[str]
        label: Mapped[str]

        __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "doc"}

    if shape == "joined":

        class Contract(Doc):  # type: ignore[no-redef]
            __tablename__ = "wr_contract"
            __table_args__ = (
                ForeignKeyConstraint(["a", "b"], ["wr_doc.a", "wr_doc.b"]),
            )

            a: Mapped[int] = mapped_column(primary_key=True)
            b: Mapped[int] = mapped_column(primary_key=True)

            __mapper_args__ = {"polymorphic_identity": "contract"}

    else:

        class Contract(Doc):  # type: ignore[no-redef]
            __mapper_args__ = {"polymorphic_identity": "contract"}

    return SimpleNamespace(Base=Base, Doc=Doc, Contract=Contract)


SHAPES = {
    shape: _models(shape) for shape in ("joined", "single_table", "joined_one_column")
}


def _keys(shape: str) -> dict[str, Any]:
    return KEYS["one_column" if shape == "joined_one_column" else "two_columns"]


def _rows(m: SimpleNamespace, keys: dict[str, Any]) -> list[Any]:
    def named(label: str) -> dict[str, Any]:
        key = keys[label]
        return (
            {"id": key} if isinstance(key, int) else dict(zip("ab", key, strict=True))
        )

    return [
        m.Doc(**named("shown"), label="shown"),
        m.Contract(**named("secret"), label="secret"),
        m.Contract(**named("other"), label="other"),
    ]


def _key(row: Any) -> Any:
    from sqlalchemy import inspect

    identity = inspect(row).identity
    return identity[0] if len(identity) == 1 else identity


@pytest_asyncio.fixture(
    params=[(shape, mode) for shape in SHAPES for mode in ("sync", "async")],
    ids=lambda param: f"{param[0]}-{param[1]}",
)
async def db(request):
    shape, mode = request.param
    m = SHAPES[shape]
    keys = _keys(shape)
    if mode == "sync":
        engine = create_engine("sqlite:///:memory:")
        m.Base.metadata.create_all(engine)
        sessions = sessionmaker(bind=engine, expire_on_commit=False)
        with sessions() as seeding:
            seeding.add_all(_rows(m, keys))
            seeding.commit()
        with sessions() as session:
            yield SimpleNamespace(
                m=m, keys=keys, session=session, sessions=sessions, is_async=False
            )
        engine.dispose()
        return

    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(m.Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as seeding:
        seeding.add_all(_rows(m, keys))
        await seeding.commit()
    async with sessions() as session:
        yield SimpleNamespace(
            m=m, keys=keys, session=session, sessions=sessions, is_async=True
        )
    await engine.dispose()


async def _run(db: Any, schema: Any, query: str) -> Any:
    context = {"session": db.session}
    if db.is_async:
        result = await schema.execute(query, context_value=context)
    else:
        result = schema.execute_sync(query, context_value=context)
    assert result.errors is None, f"{query} failed: {result.errors}"
    return result.data


async def _table(db: Any) -> dict[Any, str]:
    session = db.sessions()
    try:
        rows = list(await resolve(session.scalars(select(db.m.Doc))))
        return {_key(row): row.label for row in rows}
    finally:
        await resolve(session.close())


def _orm(**kwargs: Any) -> Any:
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False, **kwargs
    )


def _hidden(m: SimpleNamespace, query: Any) -> Any:
    return query.where(m.Contract.label != "secret")


def _schema(m: SimpleNamespace) -> Any:
    orm = _orm()

    @orm.type(m.Doc)
    class DocType:
        label: auto

    @orm.type(m.Contract)
    class ContractType:
        label: auto

        @classmethod
        def scope_rows(cls, query, info):
            return _hidden(m, query)

    @strawberry.type
    class Query:
        docs: list[DocType] = orm.field.eager()
        doc_page: ORMListConnection[DocType] = orm.connection.eager()

    return orm.schema(query=Query, types=[DocType, ContractType])


class TestSubclassRowsTellRowsApart:
    async def test_a_count_through_the_base_leaves_out_hidden_subclass_rows(self, db):
        data = await _run(db, _schema(db.m), "{ docPage { totalCount } }")
        count = data["docPage"]["totalCount"]
        assert count == 2, f"the secret contract should not be counted, got {count}"

    async def test_a_subclass_row_read_through_its_base_is_held_to_its_scope(self, db):
        data = await _run(
            db,
            _schema(db.m),
            "{ docs { label } docPage { totalCount edges { node { label } } } }",
        )
        found = {
            "docs": sorted(doc["label"] for doc in data["docs"]),
            "page": sorted(edge["node"]["label"] for edge in data["docPage"]["edges"]),
            "count": data["docPage"]["totalCount"],
        }
        assert found == {
            "docs": ["other", "shown"],
            "page": ["other", "shown"],
            "count": 2,
        }, f"the contract scope should hide the secret contract alone, got {found}"

    async def test_an_upsert_through_the_base_skips_a_row_its_repo_hides(self, db):
        m = db.m

        class Contracts(AbstractRepo):
            def scope_query(self, query, info):
                return _hidden(m, query)

        orm = _orm(repos={m.Contract: Contracts})

        @orm.type(m.Doc)
        class DocType:
            label: auto

        upsert = orm.mutations.upsert_node_input(
            model=m.Doc,
            project={
                "_meta": {
                    "update": ["label"],
                    "upsert": {"where": ["label"], "onMatch": "UPDATE"},
                }
            },
            name="DocByLabel",
        )
        info = SimpleNamespace(context={"session": db.session})

        async def write(label: str) -> None:
            payload = upsert(
                where=member(upsert, "where")(label=label),
                update=member(upsert, "update")(label="seen"),
            )
            await resolve(orm.apply_upsert(payload, info))
            await resolve(db.session.commit())

        with pytest.raises(UpsertNotFoundError):
            await write("secret")
        await write("other")
        found = await _table(db)
        expected = {
            db.keys["shown"]: "shown",
            db.keys["secret"]: "secret",
            db.keys["other"]: "seen",
        }
        assert found == expected, (
            f"only the contract the repo shows should be written, got {found}"
        )
