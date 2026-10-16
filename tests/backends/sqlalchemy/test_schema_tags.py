from types import SimpleNamespace

import pytest

from strawberry_orm import StrawberryORM
from tests.abstract.schema_tags import AbstractTestSchemaTags
from tests.backends.sqlalchemy.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class TestSchemaTags(AbstractTestSchemaTags):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session):
        self.session = sa_session

    async def run(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self.session})
