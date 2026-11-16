from types import SimpleNamespace

import pytest
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from tests.abstract.batching_parent_identity import AbstractTestBatchingParentIdentity
from tests.backends.sqlalchemy.models import Comment, Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post, Comment=Comment)


class TestBatchingParentIdentity(AbstractTestBatchingParentIdentity):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self.session})

    @staticmethod
    def comments_on(post):
        return select(Comment).where(Comment.post_id == post.id).order_by(Comment.id)
