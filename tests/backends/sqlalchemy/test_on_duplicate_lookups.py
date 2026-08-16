"""One relation reached twice must not collide in the prefetch.

An interface query carries a sibling fragment per implementing type, so the
same field is walked more than once and each pass builds its own queryset.
Django compares prefetch querysets by identity and rejects a repeated target,
which surfaced as ``'x' lookup was already seen with a different queryset``.
"""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


def _orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, lazy_resolution="off"
    )


class TestDuplicateLookups:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._s = sa_session

    def test_the_same_on_field_under_two_fragments(self):
        orm = _orm()

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto
            published: list[PostType] = orm.field.eager(
                on="posts",
                scope=lambda qs, info: qs.where(SAPost.is_published.is_(True)),
            )

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        # The field is named twice in one selection, which is what an
        # interface's sibling fragments produce.
        result = orm.schema(query=Query).execute_sync(
            """
            {
              users {
                published { title }
                ... on UserType { published { title } }
              }
            }
            """,
            context_value={"session": self._s},
        )
        assert result.errors is None, result.errors
        titles = {p["title"] for u in result.data["users"] for p in u["published"]}
        assert "Draft Post" not in titles

    def test_an_on_field_also_named_in_using(self):
        """The optimizer reaches it twice: once selected, once as a hint."""
        orm = _orm()

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto
            published: list[PostType] = orm.field.eager(
                on="posts",
                scope=lambda qs, info: qs.where(SAPost.is_published.is_(True)),
            )

            @orm.field.lazy(using=["posts"])
            def post_count(self, info: strawberry.Info) -> int:
                return len(list(self.posts))

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        result = orm.schema(query=Query).execute_sync(
            "{ users { published { title } postCount } }",
            context_value={"session": self._s},
        )
        assert result.errors is None, result.errors
