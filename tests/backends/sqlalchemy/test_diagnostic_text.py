"""What a client sends never reaches the warnings registry (SQLAlchemy)."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.abstract.diagnostic_text import AbstractTestDiagnosticText
from tests.backends.sqlalchemy.test_bounded_reads import (  # noqa: F401
    Item,
    Owner,
    UbOwner,
    database,
    hide,
    make_orm,
    run,
    seeded,
)


@pytest.fixture
def owner_rows():
    def _owner_rows(info):
        session = info.context["session"]
        query = select(UbOwner).order_by(UbOwner.id)
        if isinstance(session, AsyncSession):

            async def read():
                return (await session.scalars(query)).all()

            return read()
        return session.scalars(query).all()

    return _owner_rows


class TestDiagnosticText(AbstractTestDiagnosticText):
    pass
