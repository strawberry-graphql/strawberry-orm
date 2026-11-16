"""A one-to-one relation is a relation everywhere a client can name it (SQLAlchemy)."""

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from strawberry_orm import StrawberryORM
from tests.abstract.one_to_one_relations import (
    AbstractTestForwardOneToOne,
    AbstractTestOneToOneNodeInputs,
    AbstractTestReverseOneToOne,
)
from tests.backends.sqlalchemy.one_to_one_models import Member, OneToOneBase, Profile


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(Member=Member, Profile=Profile)


@pytest.fixture
def narrow():
    return SimpleNamespace(
        exclude=lambda query, model, field, value: query.where(
            getattr(model, field) != value
        ),
    )


@pytest.fixture
def run():
    engine = create_engine("sqlite:///:memory:")
    OneToOneBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    ann, ben = Member(id=1, name="Ann"), Member(id=2, name="Ben")
    session.add_all([ann, ben, Member(id=3, name="Cy")])
    session.add_all(
        [
            Profile(id=1, bio="Ann's", is_public=True, member=ann),
            Profile(id=2, bio="Ben's", is_public=False, member=ben),
        ]
    )
    session.commit()

    async def _run(schema, query, variables=None):
        return schema.execute_sync(
            query, variable_values=variables, context_value={"session": session}
        )

    yield _run
    session.close()
    engine.dispose()


class TestForwardOneToOne(AbstractTestForwardOneToOne):
    pass


class TestReverseOneToOne(AbstractTestReverseOneToOne):
    pass


class TestOneToOneNodeInputs(AbstractTestOneToOneNodeInputs):
    pass
