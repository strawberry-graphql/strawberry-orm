import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from tests.abstract.casts_per_execution import AbstractTestCastsPerExecution


class Base(DeclarativeBase):
    pass


class DtMember(Base):
    __tablename__ = "dt_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(100))


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(Member=DtMember)


class _SQLAlchemy(AbstractTestCastsPerExecution):
    @pytest.fixture
    def cached(self):
        """The session's identity map holds the row for every execution."""
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        with Session(engine, expire_on_commit=False) as session:
            session.add(DtMember(id=1, name="ada", email="ada@corp.example"))
            session.commit()
            self.session = session
            yield list(session.scalars(select(DtMember)))

    async def run(self, schema, query, context):
        context = {**context, "session": self.session}
        if self.asynchronous:
            return await schema.execute(query, context_value=context)
        return await asyncio.to_thread(
            schema.execute_sync, query, context_value=context
        )


class TestCastsPerExecutionSync(_SQLAlchemy):
    asynchronous = False


class TestCastsPerExecutionAsync(_SQLAlchemy):
    asynchronous = True
