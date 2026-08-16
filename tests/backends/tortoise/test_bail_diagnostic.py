"""The batcher knows when it fell back; the diagnostic says so.

Tortoise resolves asynchronously and implements no query rewrite, so a
relation resolver is answered one parent at a time whatever it returns. The
diagnostic reports that rather than leaving it to be discovered in a query log.
"""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.tortoise.models import Post as TPost
from tests.backends.tortoise.models import User as TUser


class TestBailDiagnostic:
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def _schema(self, *, materialize):
        orm = StrawberryORM.for_tortoise(
            warn_missing_scope=False, lazy_resolution="warn"
        )

        @orm.type(TPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(TUser)
        class UserType:
            id: auto
            name: auto

            @orm.field.lazy
            async def written(self, info: strawberry.Info) -> list[PostType]:
                rows = TPost.filter(author_id=self.id)
                # Materializing leaves the batcher nothing to rewrite.
                return await rows if materialize else rows

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.eager()

        return orm.schema(query=Query)

    QUERY = "{ users { name written { title } } }"

    async def _warnings(self, schema):
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            # Identical warnings are otherwise reported once per location.
            warnings.simplefilter("always")
            result = await schema.execute(self.QUERY, context_value={})
        assert result.errors is None, result.errors
        return "\n".join(str(w.message) for w in caught)

    async def test_a_resolver_running_its_own_query_is_reported(self):
        message = await self._warnings(self._schema(materialize=True))
        assert "one query per parent" in message
        assert "users.written" in message

    async def test_a_rewritable_resolver_is_not_reported(self):
        """Async resolution cannot be rewritten either, so this one is reported too."""
        message = await self._warnings(self._schema(materialize=False))
        assert "asynchronously" in message
