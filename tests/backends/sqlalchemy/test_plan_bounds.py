from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from tests.abstract.plan_bounds import AbstractTestPlanBounds
from tests.backends.sqlalchemy.models import Base, Comment, Post, Tag, User


@pytest.fixture
def sa_session():
    # A connection with the optimizer off reads in a worker thread.
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Tag=Tag, Comment=Comment)


def _loads(level):
    return sum(1 + _loads(load.children) for load in level.values())


class TestPlanBounds(AbstractTestPlanBounds):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query, variables):
        # Each run reads its rows afresh, not what an earlier one left loaded.
        self.session.expunge_all()
        return await schema.execute(
            query, variable_values=variables, context_value={"session": self.session}
        )

    @staticmethod
    def watch_plans(backend, monkeypatch, sink):
        plan_relation_loads = backend._plan_relation_loads

        def watched(*args, **kwargs):
            plan, edges = plan_relation_loads(*args, **kwargs)
            sink.append(_loads(plan))
            return plan, edges

        monkeypatch.setattr(backend, "_plan_relation_loads", watched)

    @staticmethod
    def hide(qs, model, field, value):
        return qs.where(getattr(model, field) != value)
