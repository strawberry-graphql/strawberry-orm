"""Filter/order traversal scoping for the SQLAlchemy backend."""

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.filter_traversal_scoping import (
    AbstractTestFilterTraversalScoping,
    AbstractTestJoinScopedTraversal,
    AbstractTestScopedOrderTraversal,
)
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class TestFilterTraversalScoping(AbstractTestFilterTraversalScoping):
    @pytest.fixture(autouse=True)
    def _schema(self, sa_session, seed):
        self._sa_session = sa_session
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(SAUser)
        orm.filter(SAComment)  # lets PostFilter traverse post -> comments
        post_filter = orm.filter(SAPost)

        @orm.order_type(SAPost)
        class post_order:
            title: auto

        @orm.type(SAUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.where(SAUser.name == "Alice")

        @orm.type(SAPost, filters=post_filter, order=post_order)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        self._built = orm.schema(query=Query)

    def execute(self, query):
        return self._built.execute_sync(
            query, context_value={"session": self._sa_session}
        )

    def hidden_user_id(self):
        return self._sa_session.scalars(
            select(SAUser.id).where(SAUser.name == "Bob")
        ).one()

    def visible_user_id(self):
        return self._sa_session.scalars(
            select(SAUser.id).where(SAUser.name == "Alice")
        ).one()


class TestJoinScopedTraversal(AbstractTestJoinScopedTraversal):
    """``scope_rows`` restricts users with an explicit join, not a WHERE."""

    @pytest.fixture(autouse=True)
    def _schema(self, sa_session, seed):
        self._sa_session = sa_session
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(SAUser)
        post_filter = orm.filter(SAPost)

        @orm.type(SAUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, query, info):
                return query.join(SAPost, SAPost.author_id == SAUser.id).where(
                    SAPost.title == "Hello World"
                )

        @orm.type(SAPost, filters=post_filter)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        self._built = orm.schema(query=Query)

    def execute(self, query):
        return self._built.execute_sync(
            query, context_value={"session": self._sa_session}
        )


class TestScopedOrderTraversal(AbstractTestScopedOrderTraversal):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._sa_session = sa_session

    def build(self, allow=None, checked=True):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(SAUser)
        post_filter = orm.filter(SAPost)

        @orm.order_type(SAUser)
        class UserOrder:
            name: auto

        @orm.order_type(SAPost, allow_scoped_ordering=allow)
        class PostOrder:
            title: auto
            author: auto

        @orm.type(SAUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.where(SAUser.name == "Alice")

        @orm.type(SAPost, filters=post_filter, order=PostOrder)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        if not checked:
            return strawberry.Schema(
                query=Query, extensions=[orm.optimizer_extension()]
            )
        return orm.schema(query=Query)

    def execute(self, schema, query):
        return schema.execute_sync(query, context_value={"session": self._sa_session})

    def build_with_permissive_sibling(self):
        """``PostOrder`` captures the strict comment order; a permissive one is
        registered for the same model afterwards."""
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(SAUser)

        @orm.order_type(SAUser)
        class UserOrder:
            name: auto

        @orm.order_type(SAComment)
        class StrictCommentOrder:
            author: auto

        @orm.order_type(SAPost)
        class PostOrder:
            comments: auto

        @orm.order_type(SAComment, allow_scoped_ordering=["author"])
        class PermissiveCommentOrder:
            author: auto

        @orm.type(SAUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.where(SAUser.name == "Alice")

        @orm.type(SAPost, order=PostOrder)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        return orm.schema(query=Query)
