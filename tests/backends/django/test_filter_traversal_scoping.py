"""Filter/order traversal scoping for the Django backend."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.filter_traversal_scoping import (
    AbstractTestFilterTraversalScoping,
    AbstractTestJoinScopedTraversal,
    AbstractTestScopedOrderTraversal,
)
from tests.backends.django.models import Comment as DjangoComment
from tests.backends.django.models import Post as DjangoPost
from tests.backends.django.models import User as DjangoUser


@pytest.mark.django_db
class TestFilterTraversalScoping(AbstractTestFilterTraversalScoping):
    @pytest.fixture(autouse=True)
    def _schema(self, seed):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(DjangoUser)
        orm.filter(DjangoComment)  # lets PostFilter traverse post -> comments
        post_filter = orm.filter(DjangoPost)

        @orm.order_type(DjangoPost)
        class PostOrder:
            title: auto

        @orm.type(DjangoUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @orm.type(DjangoPost, filters=post_filter, order=PostOrder)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        self._built = orm.schema(query=Query)

    def execute(self, query):
        return self._built.execute_sync(query)

    def hidden_user_id(self):
        return DjangoUser.objects.get(name="Bob").pk

    def visible_user_id(self):
        return DjangoUser.objects.get(name="Alice").pk


@pytest.mark.django_db
class TestJoinScopedTraversal(AbstractTestJoinScopedTraversal):
    """``scope_rows`` restricts users through a related table, not a column."""

    @pytest.fixture(autouse=True)
    def _schema(self, seed):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(DjangoUser)
        post_filter = orm.filter(DjangoPost)

        @orm.type(DjangoUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(posts__title="Hello World").distinct()

        @orm.type(DjangoPost, filters=post_filter)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        self._built = orm.schema(query=Query)

    def execute(self, query):
        return self._built.execute_sync(query)


@pytest.mark.django_db
class TestScopedOrderTraversal(AbstractTestScopedOrderTraversal):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def build(self, allow=None, checked=True):
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(DjangoUser)
        post_filter = orm.filter(DjangoPost)

        @orm.order_type(DjangoUser)
        class UserOrder:
            name: auto

        @orm.order_type(DjangoPost, allow_scoped_ordering=allow)
        class PostOrder:
            title: auto
            author: auto

        @orm.type(DjangoUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @orm.type(DjangoPost, filters=post_filter, order=PostOrder)
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
        return schema.execute_sync(query)

    def build_with_permissive_sibling(self):
        """``PostOrder`` captures the strict comment order; a permissive one is
        registered for the same model afterwards."""
        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
        )
        user_filter = orm.filter(DjangoUser)

        @orm.order_type(DjangoUser)
        class UserOrder:
            name: auto

        @orm.order_type(DjangoComment)
        class StrictCommentOrder:
            author: auto

        @orm.order_type(DjangoPost)
        class PostOrder:
            comments: auto

        @orm.order_type(DjangoComment, allow_scoped_ordering=["author"])
        class PermissiveCommentOrder:
            author: auto

        @orm.type(DjangoUser, filters=user_filter)
        class UT:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return qs.filter(name="Alice")

        @orm.type(DjangoPost, order=PostOrder)
        class PT:
            id: auto
            title: auto

        @strawberry.type
        class Query:
            users: list[UT] = orm.field.auto()
            posts: list[PT] = orm.field.auto()

        return orm.schema(query=Query)
