"""Shared tests for default query limit behavior."""

import strawberry

from strawberry_orm.types import auto


def build_default_limit_schema(orm, User):
    @orm.type(User)
    class UserType:
        id: auto
        name: auto

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()

    return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])


class AbstractTestQueryDefaultLimitSync:
    def test_default_query_limit_is_applied(
        self, make_default_limit_orm, schema_execute, User, seed
    ):
        orm = make_default_limit_orm(default_query_limit=1)
        schema = build_default_limit_schema(orm, User)
        result = schema_execute(schema, "{ users { name } }")

        assert result.errors is None
        assert result.data == {"users": [{"name": "Alice"}]}


class AbstractTestQueryDefaultLimitAsync:
    async def test_default_query_limit_is_applied(
        self, make_default_limit_orm, schema_execute_async, User, seed
    ):
        orm = make_default_limit_orm(default_query_limit=1)
        schema = build_default_limit_schema(orm, User)
        result = await schema_execute_async(schema, "{ users { name } }")

        assert result.errors is None
        assert result.data == {"users": [{"name": "Alice"}]}
