import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from strawberry_orm.backends.sqlalchemy import _database_errors
from tests.abstract.write_path_key_range import AbstractTestKeysPastAnyColumn
from tests.backends.sqlalchemy.models import Base, Tag
from tests.backends.sqlalchemy.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestKeysPastAnyColumn(AbstractTestKeysPastAnyColumn):
    pass


def test_an_integer_the_driver_cannot_bind_is_a_refused_read():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    refusal = pytest.raises(ValueError, match="Invalid filter expression")
    with Session(engine) as session, refusal as refused, _database_errors():
        session.execute(select(Tag).where(Tag.id == 2**63)).all()
    assert isinstance(refused.value.__cause__, OverflowError)
