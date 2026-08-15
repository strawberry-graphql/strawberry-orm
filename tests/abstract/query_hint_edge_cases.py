"""Shared tests for root-level field hint edge cases."""

import strawberry

from strawberry_orm.types import auto


def build_root_only_hint_schema(orm, User, *, include_email: bool):
    @orm.type(User)
    class UserType:
        id: auto
        name: auto = orm.field.auto()
        email: auto

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()

    query = "{ users { name email } }" if include_email else "{ users { name } }"
    return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()]), query


def build_invalid_load_hint_schema(orm, User):
    """Build a schema whose hint names a relation that does not exist.

    Only reachable with ``strict_hints=False``; the strict default raises.
    """

    @orm.type(User)
    class UserType:
        id: auto
        name: auto = orm.field.auto(using=["does_not_exist"])

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.auto()

    return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])


class AbstractTestQueryHintEdgeCasesSync:
    root_only_includes_email = True
    expected_root_only = [
        {"name": "Alice", "email": "alice@example.com"},
        {"name": "Bob", "email": "bob@example.com"},
        {"name": "Charlie", "email": "charlie@test.org"},
    ]

    def test_root_only_hint_is_applied(
        self, make_basic_orm, schema_execute, User, seed
    ):
        orm = make_basic_orm()
        schema, query = build_root_only_hint_schema(
            orm, User, include_email=self.root_only_includes_email
        )
        result = schema_execute(schema, query)
        assert result.errors is None
        assert result.data == {"users": self.expected_root_only}

    def test_invalid_load_hint_is_ignored(
        self, make_basic_orm, schema_execute, User, seed
    ):
        orm = make_basic_orm(strict_hints=False)
        schema = build_invalid_load_hint_schema(orm, User)
        result = schema_execute(schema, "{ users { name } }")
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }


class AbstractTestQueryHintEdgeCasesAsync:
    root_only_includes_email = False
    expected_root_only = [
        {"name": "Alice"},
        {"name": "Bob"},
        {"name": "Charlie"},
    ]

    async def test_root_only_hint_is_applied(
        self, make_basic_orm, schema_execute_async, User, seed
    ):
        orm = make_basic_orm()
        schema, query = build_root_only_hint_schema(
            orm, User, include_email=self.root_only_includes_email
        )
        result = await schema_execute_async(schema, query)
        assert result.errors is None
        assert result.data == {"users": self.expected_root_only}

    async def test_invalid_load_hint_is_ignored(
        self, make_basic_orm, schema_execute_async, User, seed
    ):
        orm = make_basic_orm(strict_hints=False)
        schema = build_invalid_load_hint_schema(orm, User)
        result = await schema_execute_async(schema, "{ users { name } }")
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }
