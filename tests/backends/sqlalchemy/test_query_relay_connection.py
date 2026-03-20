"""Relay connection tests for filtering and ordering with pagination."""

from collections.abc import Iterable

import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class TestRelayConnectionFilteringAndOrdering:
    def _build_schema(self, User):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        UserFilter = orm.filter(User)
        UserOrder = orm.order(User)

        @orm.type(User, filters=UserFilter, order=UserOrder)
        class UserNode(relay.Node):
            id: relay.NodeID[int]
            name: auto
            email: auto

        @strawberry.type
        class Query:
            @orm.field()
            def users(self) -> list[UserNode]:
                return orm.get_default_queryset(User)

            @orm.connection(ORMListConnection[UserNode])
            def users_connection(
                self,
            ) -> Iterable[UserNode]:
                return orm.get_default_queryset(User)

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_filter_and_order_are_applied_before_pagination(
        self, sa_session, seed, User
    ):
        schema = self._build_schema(User)
        query = """
            query UsersConnection($after: String) {
                usersConnection(
                    filter: { field: { email: { contains: "example.com" } } }
                    order: [{ field: { name: DESC } }]
                    first: 1
                    after: $after
                ) {
                    edges {
                        cursor
                        node { name email }
                    }
                    pageInfo {
                        hasNextPage
                        hasPreviousPage
                        startCursor
                        endCursor
                    }
                }
            }
        """

        first_page = schema.execute_sync(query, context_value={"session": sa_session})
        assert first_page.errors is None
        assert first_page.data == {
            "usersConnection": {
                "edges": [
                    {
                        "cursor": "YXJyYXljb25uZWN0aW9uOjA=",
                        "node": {"name": "Bob", "email": "bob@example.com"},
                    }
                ],
                "pageInfo": {
                    "hasNextPage": True,
                    "hasPreviousPage": False,
                    "startCursor": "YXJyYXljb25uZWN0aW9uOjA=",
                    "endCursor": "YXJyYXljb25uZWN0aW9uOjA=",
                },
            }
        }

        second_page = schema.execute_sync(
            query,
            variable_values={
                "after": first_page.data["usersConnection"]["pageInfo"]["endCursor"]
            },
            context_value={"session": sa_session},
        )
        assert second_page.errors is None
        assert second_page.data == {
            "usersConnection": {
                "edges": [
                    {
                        "cursor": "YXJyYXljb25uZWN0aW9uOjE=",
                        "node": {"name": "Alice", "email": "alice@example.com"},
                    }
                ],
                "pageInfo": {
                    "hasNextPage": False,
                    "hasPreviousPage": True,
                    "startCursor": "YXJyYXljb25uZWN0aW9uOjE=",
                    "endCursor": "YXJyYXljb25uZWN0aW9uOjE=",
                },
            }
        }

    def test_ordering_controls_connection_slice(self, sa_session, seed, User):
        schema = self._build_schema(User)
        result = schema.execute_sync(
            """
                {
                    usersConnection(order: [{ field: { name: DESC } }], first: 2) {
                        edges {
                            node { name }
                        }
                        pageInfo { hasNextPage }
                    }
                }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "usersConnection": {
                "edges": [
                    {"node": {"name": "Charlie"}},
                    {"node": {"name": "Bob"}},
                ],
                "pageInfo": {"hasNextPage": True},
            }
        }

    def test_custom_list_field_applies_filter_and_order(self, sa_session, seed, User):
        schema = self._build_schema(User)
        result = schema.execute_sync(
            """
                {
                    users(
                        filter: { field: { email: { contains: "example.com" } } }
                        order: [{ field: { name: DESC } }]
                    ) {
                        name
                        email
                    }
                }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Alice", "email": "alice@example.com"},
            ]
        }
