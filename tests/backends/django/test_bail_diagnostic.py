"""The batcher knows when it fell back; the diagnostic says so.

A field's declaration cannot tell you whether the collapse actually happened -
that depends on the shape of the query at runtime. These are the cases where
it did not.
"""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


@pytest.mark.django_db
class TestBailDiagnostic:
    def _schema(self, User, Post, *, materialize):
        orm = StrawberryORM.for_django(warn_missing_scope=False, lazy_resolution="warn")

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

        @orm.type(User)
        class UserType:
            id: auto
            name: auto

            @orm.field.lazy
            def written(self, info: strawberry.Info) -> list[PostType]:
                rows = Post.objects.filter(author_id=self.id)
                # Materializing leaves the batcher nothing to rewrite.
                return list(rows) if materialize else rows

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
            result = schema.execute_sync(self.QUERY, context_value={})
        assert result.errors is None, result.errors
        return "\n".join(str(w.message) for w in caught)

    def test_a_resolver_running_its_own_query_is_reported(self, seed, User, Post):
        """Materializing leaves nothing to rewrite, and that is worth saying."""
        message = self._warnings(self._schema(User, Post, materialize=True))
        assert "one query per parent" in message
        assert "users.written" in message

    def test_a_rewritable_resolver_is_not_reported(self, seed, User, Post):
        """Returning the query lets the batcher collapse it, so there is nothing to say."""
        message = self._warnings(self._schema(User, Post, materialize=False))
        assert "one query per parent" not in message
