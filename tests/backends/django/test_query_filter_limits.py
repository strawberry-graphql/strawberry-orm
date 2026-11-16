"""Real query-level tests for filter limit and regex configuration branches."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestQueryFilterLimits:
    def _build_schema(self, User, **orm_kwargs):
        orm = StrawberryORM.for_django(**orm_kwargs)
        UserFilter = orm.filter(User)

        @orm.type(User, filters=UserFilter)
        class UserType:
            id: auto
            name: auto
            email: auto

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_filter_depth_limit_errors(self, seed, User):
        schema = self._build_schema(User, max_filter_depth=0)
        result = schema.execute_sync(
            """
            {
                users(filter: { not: { field: { name: { exact: "Alice" } } } }) {
                    name
                }
            }
            """
        )
        assert result.errors is not None
        assert "maximum depth of 0" in str(result.errors[0])

    def test_filter_branch_limit_errors(self, seed, User):
        schema = self._build_schema(User, max_filter_branches=1)
        result = schema.execute_sync(
            """
            {
                users(filter: {
                    any: [
                        { field: { name: { exact: "Alice" } } }
                        { field: { name: { exact: "Bob" } } }
                    ]
                }) {
                    name
                }
            }
            """
        )
        assert result.errors is not None
        assert "maximum is 1" in str(result.errors[0])

    def test_filter_in_list_limit_errors(self, seed, User):
        schema = self._build_schema(User, max_in_list_size=1)
        result = schema.execute_sync(
            """
            {
                users(filter: { field: { name: { inList: ["Alice", "Bob"] } } }) {
                    name
                }
            }
            """
        )
        assert result.errors is not None
        assert "maximum is 1" in str(result.errors[0])

    @pytest.mark.parametrize(
        ("pattern", "refusal"),
        [
            ("(a+)+$", "may not repeat a group"),
            ("(a|aa)+$", "may not repeat a group"),
            (".*" * 6 + "!", "may not hold two repeats"),
            ("a" * 101, "at most 100 characters"),
        ],
    )
    def test_a_pattern_that_can_backtrack_for_long_is_refused(
        self, seed, User, pattern, refusal
    ):
        schema = self._build_schema(User, enable_regex_filters=True)
        result = schema.execute_sync(
            "query ($p: String!) { users(filter: {field: {name: {regex: $p}}}) { name } }",
            variable_values={"p": pattern},
        )
        assert result.data is None
        assert refusal in result.errors[0].message

    def test_regex_filter_works_when_enabled(self, seed, User):
        schema = self._build_schema(User, enable_regex_filters=True)
        result = schema.execute_sync(
            """
            {
                users(filter: { field: { name: { regex: "^A.*" } } }) {
                    name
                }
            }
            """
        )
        assert result.errors is None
        assert result.data == {"users": [{"name": "Alice"}]}
