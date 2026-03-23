"""Additional end-to-end Tortoise coverage for backend parity gaps."""

from __future__ import annotations

import pytest

from tests.abstract.query_field_hints import AbstractTestQueryFieldHintsRegistration
from tests.abstract.query_type_generation import (
    AbstractTestQueryCustomName,
    AbstractTestQueryIncludeExclude,
    AbstractTestQueryInputGeneration,
    AbstractTestQueryPartialGeneration,
    AbstractTestQueryTypeGeneration,
)


class TestQueryTypeGeneration(AbstractTestQueryTypeGeneration):
    pass


class TestQueryInputGeneration(AbstractTestQueryInputGeneration):
    pass


class TestQueryPartialGeneration(AbstractTestQueryPartialGeneration):
    pass


class TestQueryIncludeExclude(AbstractTestQueryIncludeExclude):
    pass


class TestQueryCustomName(AbstractTestQueryCustomName):
    pass


class TestQueryFieldHintsRegistration(AbstractTestQueryFieldHintsRegistration):
    pass


class TestQueryErrorHandling:
    @pytest.mark.asyncio
    async def test_empty_result(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Nobody" } } }) { name } }
            """
        )
        assert data == {"users": []}

    def test_is_query_object_with_none(self, orm):
        assert orm.is_query_object(None) is False

    def test_is_query_object_with_dict(self, orm):
        assert orm.is_query_object({"key": "value"}) is False

    def test_is_query_object_with_int(self, orm):
        assert orm.is_query_object(42) is False


class TestQueryOrderDirection:
    @pytest.mark.asyncio
    async def test_order_by_name_asc(self, execute, seed):
        data = await execute("{ users(order: [{ field: { name: ASC } }]) { name } }")
        assert data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }

    @pytest.mark.asyncio
    async def test_order_by_name_desc(self, execute, seed):
        data = await execute("{ users(order: [{ field: { name: DESC } }]) { name } }")
        assert data == {
            "users": [
                {"name": "Charlie"},
                {"name": "Bob"},
                {"name": "Alice"},
            ]
        }

    @pytest.mark.asyncio
    async def test_order_by_title_asc(self, execute, seed):
        data = await execute("{ posts(order: [{ field: { title: ASC } }]) { title } }")
        assert data == {
            "posts": [
                {"title": "Draft Post"},
                {"title": "GraphQL Guide"},
                {"title": "Hello World"},
                {"title": "Rust Adventures"},
            ]
        }

    @pytest.mark.asyncio
    async def test_order_combined_with_filter(self, execute, seed):
        data = await execute(
            """
            { posts(
                filter: { field: { isPublished: { exact: true } } },
                order: [{ field: { title: DESC } }]
            ) { title } }
            """
        )
        assert data == {
            "posts": [
                {"title": "Rust Adventures"},
                {"title": "Hello World"},
                {"title": "GraphQL Guide"},
            ]
        }

    @pytest.mark.asyncio
    async def test_users_with_example_email_ordered_desc(self, execute, seed):
        data = await execute(
            """
            { users(
                filter: { field: { email: { contains: "example" } } },
                order: [{ field: { name: DESC } }]
            ) { name email } }
            """
        )
        assert data == {
            "users": [
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Alice", "email": "alice@example.com"},
            ]
        }


class TestQueryOrderTieBreaking:
    @pytest.mark.asyncio
    async def test_tie_break_published_then_title(self, execute, seed):
        data = await execute(
            "{ posts(order: [{ field: { isPublished: DESC } }, { field: { title: ASC } }]) { title isPublished } }"
        )
        assert data == {
            "posts": [
                {"title": "GraphQL Guide", "isPublished": True},
                {"title": "Hello World", "isPublished": True},
                {"title": "Rust Adventures", "isPublished": True},
                {"title": "Draft Post", "isPublished": False},
            ]
        }

    @pytest.mark.asyncio
    async def test_tie_break_published_then_title_desc(self, execute, seed):
        data = await execute(
            "{ posts(order: [{ field: { isPublished: DESC } }, { field: { title: DESC } }]) { title isPublished } }"
        )
        assert data == {
            "posts": [
                {"title": "Rust Adventures", "isPublished": True},
                {"title": "Hello World", "isPublished": True},
                {"title": "GraphQL Guide", "isPublished": True},
                {"title": "Draft Post", "isPublished": False},
            ]
        }


class TestQueryFilterBooleanOperators:
    @pytest.mark.asyncio
    async def test_any_filter(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                any: [
                    { field: { name: { exact: "Alice" } } },
                    { field: { name: { exact: "Bob" } } }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_all_filter(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                all: [
                    { field: { email: { contains: "example" } } },
                    { field: { id: { gt: 1 } } }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_not_filter(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                not: { field: { name: { exact: "Alice" } } }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Bob"}, {"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_nested_any_all(self, execute, seed):
        data = await execute(
            """
            { posts(filter: {
                any: [
                    {
                        all: [
                            { field: { authorId: { exact: 1 } } },
                            { field: { isPublished: { exact: true } } }
                        ]
                    },
                    { field: { authorId: { exact: 3 } } }
                ]
            }) { title } }
            """
        )
        assert data == {
            "posts": [
                {"title": "Hello World"},
                {"title": "GraphQL Guide"},
                {"title": "Rust Adventures"},
            ]
        }

    @pytest.mark.asyncio
    async def test_not_any_equals_none_of(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                not: {
                    any: [
                        { field: { name: { exact: "Alice" } } },
                        { field: { name: { exact: "Bob" } } }
                    ]
                }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_one_of_combinator(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                oneOf: [
                    { field: { name: { exact: "Alice" } } },
                    { field: { name: { exact: "Charlie" } } }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_not_all_combination(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                not: {
                    all: [
                        { field: { email: { contains: "example" } } },
                        { field: { id: { gt: 1 } } }
                    ]
                }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}


class TestQueryFilterFieldLookups:
    @pytest.mark.asyncio
    async def test_exact_string(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Alice" } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}]}

    @pytest.mark.asyncio
    async def test_contains(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { contains: "Guide" } } }) { title } }
            """
        )
        assert data == {"posts": [{"title": "GraphQL Guide"}]}

    @pytest.mark.asyncio
    async def test_boolean_filter(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { isPublished: { exact: true } } }) { title } }
            """
        )
        assert data == {
            "posts": [
                {"title": "Hello World"},
                {"title": "GraphQL Guide"},
                {"title": "Rust Adventures"},
            ]
        }

    @pytest.mark.asyncio
    async def test_integer_gt(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { id: { gt: 1 } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Bob"}, {"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_in_list(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { inList: ["Alice", "Charlie"] } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_starts_with(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { email: { startsWith: "alice" } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}]}

    @pytest.mark.asyncio
    async def test_neq(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { neq: "Bob" } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_lte(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { id: { lte: 2 } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_ends_with(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { email: { endsWith: ".org" } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_not_in_list(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { notInList: ["Alice", "Bob"] } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_is_null_false(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { isNull: false } } }) { name } }
            """
        )
        assert data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }

    @pytest.mark.asyncio
    async def test_is_null_true(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { isNull: true } } }) { name } }
            """
        )
        assert data == {"users": []}

    @pytest.mark.asyncio
    async def test_gte(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { id: { gte: 2 } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Bob"}, {"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_lt(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { id: { lt: 3 } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_range(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { id: { range: { start: 1, end: 2 } } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_i_contains(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { iContains: "guide" } } }) { title } }
            """
        )
        assert data == {"posts": [{"title": "GraphQL Guide"}]}

    @pytest.mark.asyncio
    async def test_i_starts_with(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { email: { iStartsWith: "ALICE" } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}]}

    @pytest.mark.asyncio
    async def test_i_ends_with(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { email: { iEndsWith: ".ORG" } } }) { name } }
            """
        )
        assert data == {"users": [{"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_boolean_neq(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { isPublished: { neq: true } } }) { title } }
            """
        )
        assert data == {"posts": [{"title": "Draft Post"}]}

    @pytest.mark.asyncio
    async def test_boolean_is_null(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { isPublished: { isNull: true } } }) { title } }
            """
        )
        assert data == {"posts": []}


class TestQueryFilterNestedConditions:
    @pytest.mark.asyncio
    async def test_two_column_conditions(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                all: [
                    { field: { name: { contains: "li" } } },
                    { field: { email: { endsWith: ".com" } } }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}]}

    @pytest.mark.asyncio
    async def test_three_column_conditions(self, execute, seed):
        data = await execute(
            """
            { posts(filter: {
                all: [
                    { field: { authorId: { exact: 1 } } },
                    { field: { isPublished: { exact: true } } },
                    { field: { title: { contains: "World" } } }
                ]
            }) { title } }
            """
        )
        assert data == {"posts": [{"title": "Hello World"}]}

    @pytest.mark.asyncio
    async def test_multi_column_no_match(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                all: [
                    { field: { name: { exact: "Alice" } } },
                    { field: { email: { contains: "test.org" } } }
                ]
            }) { name } }
            """
        )
        assert data == {"users": []}

    @pytest.mark.asyncio
    async def test_gt_and_lt_on_same_column(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                field: { id: { gt: 1, lt: 3 } }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_contains_and_starts_with(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                field: { email: { contains: "example", startsWith: "bob" } }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_gte_and_lte_on_same_column(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                field: { id: { gte: 1, lte: 2 } }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_not_any_all_three_deep(self, execute, seed):
        data = await execute(
            """
            { posts(filter: {
                not: {
                    any: [
                        {
                            all: [
                                { field: { isPublished: { exact: true } } },
                                { field: { authorId: { exact: 1 } } }
                            ]
                        },
                        { field: { authorId: { exact: 3 } } }
                    ]
                }
            }) { title } }
            """
        )
        assert data == {"posts": [{"title": "Draft Post"}]}

    @pytest.mark.asyncio
    async def test_all_wrapping_any_wrapping_not(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                all: [
                    {
                        any: [
                            { not: { field: { name: { exact: "Alice" } } } },
                            { not: { field: { name: { exact: "Bob" } } } }
                        ]
                    },
                    { not: { field: { email: { contains: "test.org" } } } }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_any_of_all_combinations(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                any: [
                    {
                        all: [
                            { field: { name: { startsWith: "A" } } },
                            { field: { email: { contains: "example" } } }
                        ]
                    },
                    {
                        all: [
                            { field: { name: { startsWith: "C" } } },
                            { field: { email: { contains: "test" } } }
                        ]
                    }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_four_levels_deep(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                not: {
                    all: [
                        {
                            any: [
                                { not: { field: { name: { exact: "Charlie" } } } }
                            ]
                        },
                        { not: { field: { email: { contains: "test.org" } } } }
                    ]
                }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_filter_then_exclude(self, execute, seed):
        data = await execute(
            """
            { posts(filter: {
                all: [
                    { field: { isPublished: { exact: true } } },
                    { not: { field: { authorId: { exact: 1 } } } }
                ]
            }) { title } }
            """
        )
        assert data == {"posts": [{"title": "Rust Adventures"}]}

    @pytest.mark.asyncio
    async def test_one_of_with_nested_all(self, execute, seed):
        data = await execute(
            """
            { posts(filter: {
                oneOf: [
                    {
                        all: [
                            { field: { authorId: { exact: 1 } } },
                            { field: { title: { contains: "World" } } }
                        ]
                    },
                    { field: { authorId: { exact: 3 } } }
                ]
            }) { title } }
            """
        )
        assert data == {
            "posts": [
                {"title": "Hello World"},
                {"title": "Rust Adventures"},
            ]
        }

    @pytest.mark.asyncio
    async def test_not_with_in_list(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                not: { field: { name: { inList: ["Alice", "Bob"] } } }
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Charlie"}]}

    @pytest.mark.asyncio
    async def test_all_with_range_and_string_filter(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                all: [
                    { field: { id: { range: { start: 1, end: 2 } } } },
                    { field: { email: { contains: "example" } } }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Bob"}]}

    @pytest.mark.asyncio
    async def test_any_field_or_boolean_mix(self, execute, seed):
        data = await execute(
            """
            { users(filter: {
                any: [
                    { field: { name: { exact: "Alice" } } },
                    {
                        all: [
                            { field: { email: { contains: "test" } } },
                            { field: { id: { gt: 2 } } }
                        ]
                    }
                ]
            }) { name } }
            """
        )
        assert data == {"users": [{"name": "Alice"}, {"name": "Charlie"}]}


class TestQueryFilterRelationships:
    @pytest.mark.asyncio
    async def test_filter_posts_of_user_by_published(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(filter: { field: { isPublished: { exact: true } } }) { title }
            } }
            """
        )
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_filter_posts_returns_empty_when_no_match(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Charlie" } } }) {
                name
                posts(filter: { field: { isPublished: { exact: false } } }) { title }
            } }
            """
        )
        assert data == {"users": [{"name": "Charlie", "posts": []}]}

    @pytest.mark.asyncio
    async def test_filter_comments_of_post_by_body(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(filter: { field: { body: { contains: "Nice" } } }) { body }
            } }
            """
        )
        assert data == {
            "posts": [{"title": "Hello World", "comments": [{"body": "Nice post!"}]}]
        }

    @pytest.mark.asyncio
    async def test_filter_tags_of_post_by_name(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { exact: "GraphQL Guide" } } }) {
                title
                tags(filter: { field: { name: { exact: "graphql" } } }) { name }
            } }
            """
        )
        assert data == {
            "posts": [{"title": "GraphQL Guide", "tags": [{"name": "graphql"}]}]
        }

    @pytest.mark.asyncio
    async def test_no_filter_returns_all_children(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts { title }
            } }
            """
        )
        titles = sorted(post["title"] for post in data["users"][0]["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    @pytest.mark.asyncio
    async def test_filter_with_boolean_operators(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(filter: {
                    not: { field: { body: { exact: "Nice post!" } } }
                }) { body }
            } }
            """
        )
        assert data == {
            "posts": [{"title": "Hello World", "comments": [{"body": "Thanks!"}]}]
        }


class TestQueryOrderRelationships:
    @pytest.mark.asyncio
    async def test_order_posts_by_title_asc(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(order: [{ field: { title: ASC } }]) { title }
            } }
            """
        )
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "GraphQL Guide"},
                        {"title": "Hello World"},
                    ],
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_order_posts_by_title_desc(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(order: [{ field: { title: DESC } }]) { title }
            } }
            """
        )
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_order_tags_by_name_desc(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { exact: "GraphQL Guide" } } }) {
                title
                tags(order: [{ field: { name: DESC } }]) { name }
            } }
            """
        )
        assert data == {
            "posts": [
                {
                    "title": "GraphQL Guide",
                    "tags": [{"name": "python"}, {"name": "graphql"}],
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_order_tags_by_name_asc(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { exact: "GraphQL Guide" } } }) {
                title
                tags(order: [{ field: { name: ASC } }]) { name }
            } }
            """
        )
        assert data == {
            "posts": [
                {
                    "title": "GraphQL Guide",
                    "tags": [{"name": "graphql"}, {"name": "python"}],
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_order_comments_by_body_asc(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(order: [{ field: { body: ASC } }]) { body }
            } }
            """
        )
        assert data == {
            "posts": [
                {
                    "title": "Hello World",
                    "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
                },
            ]
        }


class TestQueryFilterAndOrderRelationships:
    @pytest.mark.asyncio
    async def test_filter_and_order_posts(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(
                    filter: { field: { isPublished: { exact: true } } },
                    order: [{ field: { title: DESC } }]
                ) { title }
            } }
            """
        )
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
            ]
        }

    @pytest.mark.asyncio
    async def test_filter_and_order_comments(self, execute, seed):
        data = await execute(
            """
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(
                    filter: { field: { body: { neq: "Thanks!" } } },
                    order: [{ field: { id: ASC } }]
                ) { body }
            } }
            """
        )
        assert data == {
            "posts": [{"title": "Hello World", "comments": [{"body": "Nice post!"}]}]
        }

    @pytest.mark.asyncio
    async def test_multiple_users_with_nested_filter_and_order(self, execute, seed):
        data = await execute(
            """
            { users(order: [{ field: { name: ASC } }]) {
                name
                posts(
                    filter: { field: { isPublished: { exact: true } } },
                    order: [{ field: { title: ASC } }]
                ) { title }
            } }
            """
        )
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "GraphQL Guide"},
                        {"title": "Hello World"},
                    ],
                },
                {"name": "Bob", "posts": []},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }

    @pytest.mark.asyncio
    async def test_two_levels_of_nesting(self, execute, seed):
        data = await execute(
            """
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(order: [{ field: { title: ASC } }]) {
                    title
                    comments(order: [{ field: { body: ASC } }]) { body }
                    tags(order: [{ field: { name: ASC } }]) { name }
                }
            } }
            """
        )
        assert data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {
                            "title": "GraphQL Guide",
                            "comments": [{"body": "Great guide"}],
                            "tags": [{"name": "graphql"}, {"name": "python"}],
                        },
                        {
                            "title": "Hello World",
                            "comments": [{"body": "Nice post!"}, {"body": "Thanks!"}],
                            "tags": [{"name": "python"}],
                        },
                    ],
                },
            ]
        }


class TestQueryNestedResolution:
    @pytest.mark.asyncio
    async def test_user_with_posts(self, execute, seed):
        data = await execute("{ user(id: 1) { name posts { title } } }")
        assert data["user"]["name"] == "Alice"
        assert sorted(post["title"] for post in data["user"]["posts"]) == [
            "GraphQL Guide",
            "Hello World",
        ]

    @pytest.mark.asyncio
    async def test_post_with_tags(self, execute, seed):
        data = await execute("{ posts { title tags { name } } }")
        posts = {
            post["title"]: sorted(tag["name"] for tag in post["tags"])
            for post in data["posts"]
        }
        assert posts == {
            "Hello World": ["python"],
            "GraphQL Guide": ["graphql", "python"],
            "Draft Post": [],
            "Rust Adventures": ["rust"],
        }

    @pytest.mark.asyncio
    async def test_post_with_comments(self, execute, seed):
        data = await execute("{ posts { title comments { body parentId } } }")
        assert data == {
            "posts": [
                {
                    "title": "Hello World",
                    "comments": [
                        {"body": "Nice post!", "parentId": None},
                        {"body": "Thanks!", "parentId": 1},
                    ],
                },
                {
                    "title": "GraphQL Guide",
                    "comments": [{"body": "Great guide", "parentId": None}],
                },
                {"title": "Draft Post", "comments": []},
                {"title": "Rust Adventures", "comments": []},
            ]
        }

    @pytest.mark.asyncio
    async def test_deeply_nested(self, execute, seed):
        data = await execute(
            "{ user(id: 1) { posts { title tags { name } comments { body } } } }"
        )
        posts = sorted(data["user"]["posts"], key=lambda post: post["title"])
        assert posts[0]["title"] == "GraphQL Guide"
        assert sorted(tag["name"] for tag in posts[0]["tags"]) == [
            "graphql",
            "python",
        ]
        assert posts[0]["comments"] == [{"body": "Great guide"}]
        assert posts[1]["title"] == "Hello World"
        assert sorted(tag["name"] for tag in posts[1]["tags"]) == [
            "python",
        ]
        assert posts[1]["comments"] == [{"body": "Nice post!"}, {"body": "Thanks!"}]


class TestQuerySelfIsModel:
    @pytest.mark.asyncio
    async def test_summary_uses_model_fields(self, self_model_execute):
        data = await self_model_execute("{ posts { title summary } }")
        assert data == {
            "posts": [
                {"title": "Hello World", "summary": "Hello World: First post"},
                {"title": "GraphQL Guide", "summary": "GraphQL Guide: Learn Grap"},
                {"title": "Draft Post", "summary": "Draft Post: Not publis"},
                {"title": "Rust Adventures", "summary": "Rust Adventures: Systems pr"},
            ]
        }

    @pytest.mark.asyncio
    async def test_title_upper_uses_model_method(self, self_model_execute):
        data = await self_model_execute("{ posts { title titleUpper } }")
        assert data == {
            "posts": [
                {"title": "Hello World", "titleUpper": "HELLO WORLD"},
                {"title": "GraphQL Guide", "titleUpper": "GRAPHQL GUIDE"},
                {"title": "Draft Post", "titleUpper": "DRAFT POST"},
                {"title": "Rust Adventures", "titleUpper": "RUST ADVENTURES"},
            ]
        }

    @pytest.mark.asyncio
    async def test_display_name_uses_model_fields(self, self_model_execute):
        data = await self_model_execute("{ users { displayName } }")
        assert data == {
            "users": [
                {"displayName": "Alice <alice@example.com>"},
                {"displayName": "Bob <bob@example.com>"},
                {"displayName": "Charlie <charlie@test.org>"},
            ]
        }

    @pytest.mark.asyncio
    async def test_post_count_accesses_relationship(self, self_model_execute):
        data = await self_model_execute("{ users { name postCount } }")
        assert data == {
            "users": [
                {"name": "Alice", "postCount": 2},
                {"name": "Bob", "postCount": 1},
                {"name": "Charlie", "postCount": 1},
            ]
        }


class TestQueryGetQueryset:
    @pytest.mark.asyncio
    async def test_get_queryset_filters_unpublished(self, get_queryset_execute):
        data = await get_queryset_execute("{ posts { title isPublished } }")
        assert data == {
            "posts": [
                {"title": "Hello World", "isPublished": True},
                {"title": "GraphQL Guide", "isPublished": True},
                {"title": "Rust Adventures", "isPublished": True},
            ]
        }

    @pytest.mark.asyncio
    async def test_published_count(self, get_queryset_execute):
        data = await get_queryset_execute("{ posts { id } }")
        assert data == {"posts": [{"id": 1}, {"id": 2}, {"id": 4}]}

    @pytest.mark.asyncio
    async def test_unscoped_type_returns_all(self, get_queryset_execute):
        data = await get_queryset_execute("{ users { name } }")
        assert data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }


class TestQueryMultipleTypes:
    def test_brief_type_has_fewer_fields(self, user_brief_type, user_full_type):
        brief_fields = {
            f.name for f in user_brief_type.__strawberry_definition__.fields
        }
        full_fields = {f.name for f in user_full_type.__strawberry_definition__.fields}
        assert brief_fields < full_fields
        assert "email" not in brief_fields
        assert "email" in full_fields

    @pytest.mark.asyncio
    async def test_brief_query(self, multi_type_execute):
        data = await multi_type_execute("{ usersBrief { id name } }")
        assert data == {
            "usersBrief": [
                {"id": 1, "name": "Alice"},
                {"id": 2, "name": "Bob"},
                {"id": 3, "name": "Charlie"},
            ]
        }

    @pytest.mark.asyncio
    async def test_full_query(self, multi_type_execute):
        data = await multi_type_execute("{ usersFull { id name email } }")
        assert data == {
            "usersFull": [
                {"id": 1, "name": "Alice", "email": "alice@example.com"},
                {"id": 2, "name": "Bob", "email": "bob@example.com"},
                {"id": 3, "name": "Charlie", "email": "charlie@test.org"},
            ]
        }

    @pytest.mark.asyncio
    async def test_both_queries_together(self, multi_type_execute):
        data = await multi_type_execute(
            "{ usersBrief { name } usersFull { name email } }"
        )
        assert data == {
            "usersBrief": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ],
            "usersFull": [
                {"name": "Alice", "email": "alice@example.com"},
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Charlie", "email": "charlie@test.org"},
            ],
        }
