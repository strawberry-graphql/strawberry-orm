"""Abstract get_queryset scoping tests."""


class AbstractTestQueryGetQueryset:
    def test_get_queryset_filters_unpublished(self, get_queryset_execute):
        data = get_queryset_execute("{ posts { title isPublished } }")
        assert data == {
            "posts": [
                {"title": "Hello World", "isPublished": True},
                {"title": "GraphQL Guide", "isPublished": True},
                {"title": "Rust Adventures", "isPublished": True},
            ]
        }

    def test_published_count(self, get_queryset_execute):
        data = get_queryset_execute("{ posts { id } }")
        assert data == {"posts": [{"id": 1}, {"id": 2}, {"id": 4}]}

    def test_unscoped_type_returns_all(self, get_queryset_execute):
        data = get_queryset_execute("{ users { name } }")
        assert data == {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
                {"name": "Charlie"},
            ]
        }
