"""Abstract ordering tests: ASC, DESC, NULLS_FIRST, NULLS_LAST, combined with filters."""


class AbstractTestQueryOrderDirection:
    def test_order_by_name_asc(self, execute, seed):
        data = execute("{ users(order: [{ name: ASC }]) { name } }")
        assert data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }

    def test_order_by_name_desc(self, execute, seed):
        data = execute("{ users(order: [{ name: DESC }]) { name } }")
        assert data == {
            "users": [
                {"name": "Charlie"},
                {"name": "Bob"},
                {"name": "Alice"},
            ]
        }

    def test_order_by_title_asc(self, execute, seed):
        data = execute("{ posts(order: [{ title: ASC }]) { title } }")
        assert data == {
            "posts": [
                {"title": "Draft Post"},
                {"title": "GraphQL Guide"},
                {"title": "Hello World"},
                {"title": "Rust Adventures"},
            ]
        }

    def test_order_combined_with_filter(self, execute, seed):
        data = execute("""
            { posts(
                filter: { field: { isPublished: { exact: true } } },
                order: [{ title: DESC }]
            ) { title } }
        """)
        assert data == {
            "posts": [
                {"title": "Rust Adventures"},
                {"title": "Hello World"},
                {"title": "GraphQL Guide"},
            ]
        }

    def test_users_with_example_email_ordered_desc(self, execute, seed):
        data = execute("""
            { users(
                filter: { field: { email: { contains: "example" } } },
                order: [{ name: DESC }]
            ) { name email } }
        """)
        assert data == {
            "users": [
                {"name": "Bob", "email": "bob@example.com"},
                {"name": "Alice", "email": "alice@example.com"},
            ]
        }


class AbstractTestQueryOrderTieBreaking:
    def test_tie_break_published_then_title(self, execute, seed):
        data = execute(
            "{ posts(order: [{ isPublished: DESC }, { title: ASC }]) { title isPublished } }"
        )
        assert data == {
            "posts": [
                {"title": "GraphQL Guide", "isPublished": True},
                {"title": "Hello World", "isPublished": True},
                {"title": "Rust Adventures", "isPublished": True},
                {"title": "Draft Post", "isPublished": False},
            ]
        }

    def test_tie_break_published_then_title_desc(self, execute, seed):
        data = execute(
            "{ posts(order: [{ isPublished: DESC }, { title: DESC }]) { title isPublished } }"
        )
        assert data == {
            "posts": [
                {"title": "Rust Adventures", "isPublished": True},
                {"title": "Hello World", "isPublished": True},
                {"title": "GraphQL Guide", "isPublished": True},
                {"title": "Draft Post", "isPublished": False},
            ]
        }


class AbstractTestQueryOrderNulls:
    def test_asc_nulls_first(self, execute, seed):
        data = execute(
            "{ comments(order: [{ parentId: ASC_NULLS_FIRST }]) { id parentId } }"
        )
        parent_ids = [c["parentId"] for c in data["comments"]]
        null_positions = [i for i, v in enumerate(parent_ids) if v is None]
        non_null_positions = [i for i, v in enumerate(parent_ids) if v is not None]
        if null_positions and non_null_positions:
            assert max(null_positions) < min(non_null_positions)

    def test_asc_nulls_last(self, execute, seed):
        data = execute(
            "{ comments(order: [{ parentId: ASC_NULLS_LAST }]) { id parentId } }"
        )
        parent_ids = [c["parentId"] for c in data["comments"]]
        null_positions = [i for i, v in enumerate(parent_ids) if v is None]
        non_null_positions = [i for i, v in enumerate(parent_ids) if v is not None]
        if null_positions and non_null_positions:
            assert min(null_positions) > max(non_null_positions)

    def test_desc_nulls_first(self, execute, seed):
        data = execute(
            "{ comments(order: [{ parentId: DESC_NULLS_FIRST }]) { id parentId } }"
        )
        parent_ids = [c["parentId"] for c in data["comments"]]
        null_positions = [i for i, v in enumerate(parent_ids) if v is None]
        non_null_positions = [i for i, v in enumerate(parent_ids) if v is not None]
        if null_positions and non_null_positions:
            assert max(null_positions) < min(non_null_positions)

    def test_desc_nulls_last(self, execute, seed):
        data = execute(
            "{ comments(order: [{ parentId: DESC_NULLS_LAST }]) { id parentId } }"
        )
        parent_ids = [c["parentId"] for c in data["comments"]]
        null_positions = [i for i, v in enumerate(parent_ids) if v is None]
        non_null_positions = [i for i, v in enumerate(parent_ids) if v is not None]
        if null_positions and non_null_positions:
            assert min(null_positions) > max(non_null_positions)
