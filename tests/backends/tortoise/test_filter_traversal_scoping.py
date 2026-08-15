"""Filter/order traversal scoping for the Tortoise backend."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.filter_traversal_scoping import (
    AbstractTestFilterTraversalScopingAsync,
    AbstractTestJoinScopedTraversalAsync,
    AbstractTestScopedOrderTraversalAsync,
)
from tests.backends.tortoise.models import Comment as TortoiseComment
from tests.backends.tortoise.models import Post as TortoisePost
from tests.backends.tortoise.models import User as TortoiseUser


class TestFilterTraversalScoping(AbstractTestFilterTraversalScopingAsync):
    @pytest.fixture(autouse=True)
    def _schema(self, seed):
        orm = StrawberryORM.for_tortoise(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(TortoiseUser)
        orm.filter(TortoiseComment)  # lets PostFilter traverse post -> comments
        post_filter = orm.filter(TortoisePost)

        @orm.order_type(TortoisePost)
        class PostOrder:
            title: auto

        @orm.type(TortoiseUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @orm.type(TortoisePost, filters=post_filter, order=PostOrder)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        self._built = orm.schema(query=Query)

    async def execute(self, query):
        return await self._built.execute(query)

    async def hidden_user_id(self):
        return (await TortoiseUser.get(name="Bob")).pk

    async def visible_user_id(self):
        return (await TortoiseUser.get(name="Alice")).pk


class TestJoinScopedTraversal(AbstractTestJoinScopedTraversalAsync):
    """``scope_rows`` restricts users through a related table, not a column."""

    @pytest.fixture(autouse=True)
    def _schema(self, seed):
        orm = StrawberryORM.for_tortoise(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(TortoiseUser)
        post_filter = orm.filter(TortoisePost)

        @orm.type(TortoiseUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(posts__title="Hello World").distinct()

        @orm.type(TortoisePost, filters=post_filter)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        self._built = orm.schema(query=Query)

    async def execute(self, query):
        return await self._built.execute(query)


class TestScopedOrderTraversal(AbstractTestScopedOrderTraversalAsync):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def build(self, allow=None, checked=True):
        orm = StrawberryORM.for_tortoise(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(TortoiseUser)
        post_filter = orm.filter(TortoisePost)

        @orm.order_type(TortoiseUser)
        class UserOrder:
            name: auto

        @orm.order_type(TortoisePost, allow_scoped_ordering=allow)
        class PostOrder:
            title: auto
            author: auto

        @orm.type(TortoiseUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @orm.type(TortoisePost, filters=post_filter, order=PostOrder)
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

    async def execute(self, schema, query):
        return await schema.execute(query)

    def build_with_permissive_sibling(self):
        """``PostOrder`` captures the strict comment order; a permissive one is
        registered for the same model afterwards."""
        orm = StrawberryORM.for_tortoise(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(TortoiseUser)

        @orm.order_type(TortoiseUser)
        class UserOrder:
            name: auto

        @orm.order_type(TortoiseComment)
        class StrictCommentOrder:
            author: auto

        @orm.order_type(TortoisePost)
        class PostOrder:
            comments: auto

        @orm.order_type(TortoiseComment, allow_scoped_ordering=["author"])
        class PermissiveCommentOrder:
            author: auto

        @orm.type(TortoiseUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @orm.type(TortoisePost, order=PostOrder)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        return orm.schema(query=Query)
