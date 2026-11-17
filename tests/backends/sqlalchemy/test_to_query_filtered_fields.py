from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.to_query_filtered_fields import (
    AbstractTestToQueryFilteredFields,
    to_query,
)
from tests.backends.sqlalchemy.models import Base, Post, User


@pytest.fixture
def sa_session():
    # Without the optimizer, the read runs in a worker thread.
    engine = create_engine(
        "sqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as session:
        yield session
    engine.dispose()


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=policy(),
        to_query=to_query,
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestToQueryFilteredFields(AbstractTestToQueryFilteredFields):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        return await schema.execute(query, context_value={"session": self.session})

    @staticmethod
    def published_only(qs):
        return qs.where(Post.is_published.is_(True))

    @staticmethod
    def all_posts():
        return select(Post)

    async def load_posts(self):
        return list(self.session.scalars(select(Post).order_by(Post.id)))
