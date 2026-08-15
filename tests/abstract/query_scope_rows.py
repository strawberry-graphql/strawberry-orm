"""Abstract scope_rows scoping tests."""


class AbstractTestQueryGetQueryset:
    def test_scope_rows_filters_unpublished(self, scope_rows_execute):
        data = scope_rows_execute("{ posts { title isPublished } }")
        assert data == {
            "posts": [
                {"title": "Hello World", "isPublished": True},
                {"title": "GraphQL Guide", "isPublished": True},
                {"title": "Rust Adventures", "isPublished": True},
            ]
        }

    def test_published_count(self, scope_rows_execute):
        data = scope_rows_execute("{ posts { id } }")
        assert data == {"posts": [{"id": 1}, {"id": 2}, {"id": 4}]}

    def test_unscoped_type_returns_all(self, scope_rows_execute):
        data = scope_rows_execute("{ users { name } }")
        assert data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }
