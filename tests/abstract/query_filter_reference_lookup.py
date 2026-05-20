"""Abstract tests for ReferenceLookup (FK / related PK filters without joins)."""


class AbstractTestQueryFilterReferenceLookup:
    def test_filter_posts_by_object_author_id_in_list(self, execute, seed):
        data = execute("""
            { posts(filter: {
                object: { author: { field: { id: { inList: [1] } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    def test_filter_posts_by_object_author_id_one_of(self, execute, seed):
        data = execute("""
            { posts(filter: {
                object: { author: { oneOf: [
                    { field: { id: { exact: 1 } } },
                    { field: { id: { exact: 3 } } }
                ] } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World", "Rust Adventures"]

    def test_object_author_name_still_filters_by_related_field(self, execute, seed):
        """Non-reference fields on the related model still use object traversal."""
        data = execute("""
            { posts(filter: {
                object: { author: { field: { name: { exact: "Alice" } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]
