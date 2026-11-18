"""A value the library refuses is quoted cut short, never whole.

A refusal raised in the library's own code reaches the client with its
message, and Strawberry logs every error at ERROR, so a message quoting a
value as the client sent it puts a value of any length in the response and
in the server's log, on every request. Every message quoting a client's
value cuts it to a few dozen characters.

Each place the library quotes a value it refuses - a filter value it cannot
read, an id that is not a key, a global ID for another type, a ``where`` no
row matches - is sent one
50,000 characters long. A backend that masks or accepts the value passes as
well: what matters is that it is never quoted whole.

Each backend supplies a ``writes`` fixture
(:class:`~tests.abstract.write_path_authorization.WriteDb`).
"""

import inspect
import logging
from typing import Any

import pytest
import strawberry
from strawberry.relay import GlobalID

from strawberry_orm import UpsertValidationError
from strawberry_orm.types import auto
from tests.abstract.write_path_authorization import Blog, allow, member

LONG = "x" * 50_000
#: The longest a message quoting it may be.
MOST = 300
#: The longest a line of the log may be: the message, and GraphQL's excerpt
#: of the document around the error, which it prints in pieces of 80.
LOGGED = MOST * 2


def _short(message: str, most: int = MOST) -> None:
    assert len(message) <= most and LONG[:100] not in message, message[: most * 2]


class AbstractTestClientValuesInMessages(Blog):
    async def test_a_value_a_filter_cannot_read(self, writes, blog, caplog):
        orm = writes.new_orm()

        @orm.type(writes.m.Post, filters=orm.filter(writes.m.Post))
        class QuotedPost:
            id: auto
            created_at: auto

        @strawberry.type
        class Query:
            posts: list[QuotedPost] = orm.field.eager()

        query = f'{{ posts(filter: {{field: {{createdAt: {{exact: "{LONG}"}}}}}}) {{ id }} }}'
        with caplog.at_level(logging.ERROR, logger="strawberry.execution"):
            result = await writes.execute(orm.schema(query=Query), query)
        for error in result.errors or ():
            _short(error.message)
        for record in caplog.records:
            _short(record.getMessage(), LOGGED)

    async def test_an_id_that_is_not_a_key(self, writes, blog):
        orm = writes.new_orm()
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Post,
            project={"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
            name="QuotedPostUpsert",
        )
        with pytest.raises(UpsertValidationError) as refused:
            await writes.upsert(
                orm, upsert(where=member(upsert, "where")(id=LONG)), authorize=allow
            )
        _short(str(refused.value))

    async def test_a_global_id_for_another_type(self, writes, blog):
        orm = writes.new_orm()

        @orm.type(writes.m.User)
        class QuotedUser:
            id: auto

        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Post,
            project={"_meta": {"upsert": {"where": ["id"], "onMatch": "IGNORE"}}},
            name="QuotedPostUpsert",
        )
        foreign = str(GlobalID("QuotedUser", LONG))
        with pytest.raises(UpsertValidationError) as refused:
            await writes.upsert(
                orm, upsert(where=member(upsert, "where")(id=foreign)), authorize=allow
            )
        assert "is a global ID for QuotedUser" in str(refused.value), refused.value
        _short(str(refused.value))

    async def test_a_where_no_row_matches(self, writes, blog, caplog):
        orm = writes.new_orm()
        upsert = orm.mutations.upsert_node_input(
            model=writes.m.Post,
            project={"_meta": {"upsert": {"where": ["title"], "onMatch": "IGNORE"}}},
            name="QuotedPostUpsert",
        )

        @orm.type(writes.m.Post)
        class QuotedPost:
            id: auto

        @strawberry.type
        class Query:
            posts: list[QuotedPost] = orm.field.eager()

        @strawberry.type
        class Mutation:
            upsert_post: int = _mutation(orm, upsert)

        schema = orm.schema(query=Query, mutation=Mutation)
        query = f'mutation {{ upsertPost(input: {{where: {{title: "{LONG}"}}}}) }}'
        with caplog.at_level(logging.ERROR, logger="strawberry.execution"):
            result = await writes.execute(schema, query)
        assert result.errors, "the upsert matched nothing and was not refused"
        for error in result.errors:
            _short(error.message)
        for record in caplog.records:
            _short(record.getMessage(), LOGGED)


def _mutation(orm, input_type):
    def resolver(info: strawberry.Info, input: Any) -> Any:
        row = orm.apply_upsert(input, info, authorize=allow)
        if inspect.isawaitable(row):

            async def finish() -> int:
                return (await row).id

            return finish()
        return row.id

    resolver.__annotations__ = {
        "info": strawberry.Info,
        "input": input_type,
        "return": int,
    }
    return strawberry.mutation(resolver=resolver)
