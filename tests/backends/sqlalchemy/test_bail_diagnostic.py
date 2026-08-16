"""The batcher knows when it fell back; the diagnostic says so.

A field's declaration cannot tell you whether the collapse actually happened -
that depends on the shape of the query at runtime. These are the cases where
it did not.
"""

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class TestBailDiagnostic:
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._s = sa_session

    def _schema(self, *, materialize):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", warn_missing_scope=False, lazy_resolution="warn"
        )

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

            @orm.field.lazy
            def written(self, info: strawberry.Info) -> list[PostType]:
                rows = select(SAPost).where(SAPost.author_id == self.id)
                # Materializing leaves the batcher nothing to rewrite.
                if materialize:
                    return list(info.context["session"].scalars(rows))
                return rows

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        return orm.schema(query=Query)

    QUERY = "{ users { name written { title } } }"

    def _warnings(self, schema):
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            # Identical warnings are otherwise reported once per location.
            warnings.simplefilter("always")
            result = schema.execute_sync(self.QUERY, context_value={"session": self._s})
        assert result.errors is None, result.errors
        return "\n".join(str(w.message) for w in caught)

    def test_a_resolver_running_its_own_query_is_reported(self):
        """Materializing leaves nothing to rewrite, and that is worth saying."""
        message = self._warnings(self._schema(materialize=True))
        assert "one query per parent" in message
        assert "users.written" in message

    def test_a_rewritable_resolver_is_not_reported(self):
        """Returning the query lets the batcher collapse it, so there is nothing to say."""
        message = self._warnings(self._schema(materialize=False))
        assert "one query per parent" not in message
