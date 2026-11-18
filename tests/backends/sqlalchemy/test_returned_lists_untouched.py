from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from strawberry_orm import StrawberryORM
from tests.abstract.returned_lists_untouched import (
    AbstractTestReturnedListsUntouched,
)
from tests.backends.sqlalchemy.models import Post, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(User=User, Post=Post)


class TestReturnedListsUntouched(AbstractTestReturnedListsUntouched):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self.session = sa_session

    async def run(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self.session})

    async def load_users(self):
        self.session.expunge_all()
        return list(
            self.session.scalars(
                select(User).options(selectinload(User.posts)).order_by(User.id)
            )
        )

    @staticmethod
    def collection(user):
        return user.posts

    @staticmethod
    def published_only(qs):
        return qs.where(Post.is_published.is_(True))

    async def written(self):
        self.session.flush()
        return dict(self.session.execute(select(Post.title, Post.author_id)).all())
