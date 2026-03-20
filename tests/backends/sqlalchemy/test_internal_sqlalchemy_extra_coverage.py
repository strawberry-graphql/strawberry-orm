"""SQLAlchemy-specific exact branch coverage."""

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import Boolean, Integer, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from strawberry_orm import Ordering, StrawberryORM
from strawberry_orm.backends.sqlalchemy import (
    SQLAlchemyBackend,
    _build_lookup_clauses,
    _build_sa_ordering,
    _introspect_sa_model,
)


class Base(DeclarativeBase):
    pass


class FlagModel(Base):
    __tablename__ = "flag_model"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


@pytest_asyncio.fixture
async def async_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@strawberry.input
class RegexLookup:
    i_regex: str | None = strawberry.UNSET


@strawberry.input
class MissingOrder:
    missing: Ordering | None = Ordering.ASC


class TestInternalSqlalchemyExtraCoverage:
    def test_boolean_impl_and_missing_relation_type_paths(self):
        backend = StrawberryORM("sqlalchemy", dialect="sqlite").backend
        fields = _introspect_sa_model(FlagModel)
        assert ("enabled", bool, False, None) in fields

        filt = backend.filter(FlagModel)
        order = backend.order(FlagModel)

        @backend.type(FlagModel, filters=filt, order=order)
        class FlagType:
            id: strawberry.auto
            bogus: list["FlagType"]

        assert FlagType is not None

    @pytest.mark.asyncio
    async def test_async_execute_stmt_invalid_expression_is_sanitized(
        self, async_session
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        with pytest.raises(ValueError, match="Invalid filter expression"):
            await backend._execute_stmt_async(
                async_session, text("select * from missing_table")
            )

    def test_ordering_and_regex_helper_edges(self):
        clause = _build_lookup_clauses(
            FlagModel.enabled, RegexLookup(i_regex="x"), enable_regex=True
        )
        assert len(clause) == 1
        assert _build_sa_ordering(MissingOrder(), FlagModel) == ([], [])
