"""Abstract tests for filtering and ordering on relationship (nested) fields."""


class AbstractTestQueryFilterRelationships:
    """Filter on list-relationship fields one level deep."""

    def test_filter_posts_of_user_by_published(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(filter: { field: { isPublished: { exact: true } } }) { title }
            } }
        """)
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

    def test_filter_posts_returns_empty_when_no_match(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Charlie" } } }) {
                name
                posts(filter: { field: { isPublished: { exact: false } } }) { title }
            } }
        """)
        assert data == {
            "users": [
                {"name": "Charlie", "posts": []},
            ]
        }

    def test_filter_comments_of_post_by_body(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(filter: { field: { body: { contains: "Nice" } } }) { body }
            } }
        """)
        assert data == {
            "posts": [
                {"title": "Hello World", "comments": [{"body": "Nice post!"}]},
            ]
        }

    def test_filter_tags_of_post_by_name(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { exact: "GraphQL Guide" } } }) {
                title
                tags(filter: { field: { name: { exact: "graphql" } } }) { name }
            } }
        """)
        assert data == {
            "posts": [
                {"title": "GraphQL Guide", "tags": [{"name": "graphql"}]},
            ]
        }

    def test_no_filter_returns_all_children(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts { title }
            } }
        """)
        titles = sorted(p["title"] for p in data["users"][0]["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    def test_filter_with_boolean_operators(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(filter: {
                    not: { field: { body: { exact: "Nice post!" } } }
                }) { body }
            } }
        """)
        assert data == {
            "posts": [
                {"title": "Hello World", "comments": [{"body": "Thanks!"}]},
            ]
        }


class AbstractTestQueryOrderRelationships:
    """Order on list-relationship fields one level deep."""

    def test_order_posts_by_title_asc(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(order: [{ title: ASC }]) { title }
            } }
        """)
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

    def test_order_posts_by_title_desc(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(order: [{ title: DESC }]) { title }
            } }
        """)
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

    def test_order_tags_by_name_desc(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { exact: "GraphQL Guide" } } }) {
                title
                tags(order: [{ name: DESC }]) { name }
            } }
        """)
        assert data == {
            "posts": [
                {
                    "title": "GraphQL Guide",
                    "tags": [
                        {"name": "python"},
                        {"name": "graphql"},
                    ],
                },
            ]
        }

    def test_order_tags_by_name_asc(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { exact: "GraphQL Guide" } } }) {
                title
                tags(order: [{ name: ASC }]) { name }
            } }
        """)
        assert data == {
            "posts": [
                {
                    "title": "GraphQL Guide",
                    "tags": [
                        {"name": "graphql"},
                        {"name": "python"},
                    ],
                },
            ]
        }

    def test_order_comments_by_body_asc(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(order: [{ body: ASC }]) { body }
            } }
        """)
        assert data == {
            "posts": [
                {
                    "title": "Hello World",
                    "comments": [
                        {"body": "Nice post!"},
                        {"body": "Thanks!"},
                    ],
                },
            ]
        }


class AbstractTestQueryFilterAndOrderRelationships:
    """Combine filter and order on the same relationship field."""

    def test_filter_and_order_posts(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(
                    filter: { field: { isPublished: { exact: true } } },
                    order: [{ title: DESC }]
                ) { title }
            } }
        """)
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

    def test_filter_and_order_comments(self, execute, seed):
        data = execute("""
            { posts(filter: { field: { title: { exact: "Hello World" } } }) {
                title
                comments(
                    filter: { field: { body: { neq: "Thanks!" } } },
                    order: [{ id: ASC }]
                ) { body }
            } }
        """)
        assert data == {
            "posts": [
                {"title": "Hello World", "comments": [{"body": "Nice post!"}]},
            ]
        }

    def test_multiple_users_with_nested_filter_and_order(self, execute, seed):
        data = execute("""
            { users(order: [{ name: ASC }]) {
                name
                posts(
                    filter: { field: { isPublished: { exact: true } } },
                    order: [{ title: ASC }]
                ) { title }
            } }
        """)
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
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures"},
                    ],
                },
            ]
        }

    def test_two_levels_of_nesting(self, execute, seed):
        data = execute("""
            { users(filter: { field: { name: { exact: "Alice" } } }) {
                name
                posts(order: [{ title: ASC }]) {
                    title
                    comments(order: [{ body: ASC }]) { body }
                    tags(order: [{ name: ASC }]) { name }
                }
            } }
        """)
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
