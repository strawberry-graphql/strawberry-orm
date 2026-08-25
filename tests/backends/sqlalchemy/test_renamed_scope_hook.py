"""The 0.14 name for the row-scoping hook must not be ignored in silence.

``get_queryset`` became ``scope_rows`` in 0.15. A class still carrying the old
name looks scoped to its author and is not, so every row it was meant to hide
is returned. The upgrade path has to fail loudly rather than quietly widen
what a caller can read.
"""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Post as SAPost


def _orm(**kwargs):
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", **kwargs
    )


class TestRenamedScopeHook:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._s = sa_session

    def _build(self, orm):
        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def get_queryset(cls, query, info):
                return query.where(SAPost.is_published.is_(True))

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.eager()

        return orm.schema(query=Query)

    def test_the_old_name_is_refused(self):
        with pytest.raises(ValueError, match="get_queryset"):
            self._build(_orm(warn_missing_scope=False))

    def test_the_message_names_the_replacement(self):
        with pytest.raises(ValueError, match="scope_rows"):
            self._build(_orm(warn_missing_scope=False))

    def test_it_is_refused_even_with_the_warning_turned_off(self):
        """warn_missing_scope covers an absent hook, not a misnamed one."""
        with pytest.raises(ValueError):
            self._build(_orm(warn_missing_scope=False))

    def test_the_new_name_still_works(self):
        orm = _orm(warn_missing_scope=False)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, query, info):
                return query.where(SAPost.is_published.is_(True))

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.eager()

        result = orm.schema(query=Query).execute_sync(
            "{ posts { title } }", context_value={"session": self._s}
        )
        assert result.errors is None, result.errors
        titles = [p["title"] for p in result.data["posts"]]
        assert "Draft Post" not in titles, titles
