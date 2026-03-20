"""Abstract tests for object traversal filters (filtering by related object fields)."""


class AbstractTestQueryFilterObjectTraversal:
    """Filter parent records by conditions on related objects via the `object` key."""

    def test_filter_posts_by_author_name(self, execute, seed):
        data = execute("""
            { posts(filter: {
                object: { author: { field: { name: { exact: "Alice" } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    def test_filter_posts_by_author_name_no_match(self, execute, seed):
        data = execute("""
            { posts(filter: {
                object: { author: { field: { name: { exact: "Nobody" } } } }
            }) { title } }
        """)
        assert data == {"posts": []}

    def test_filter_comments_by_post_title(self, execute, seed):
        data = execute("""
            { comments(filter: {
                object: { post: { field: { title: { exact: "Hello World" } } } }
            }) { body } }
        """)
        bodies = sorted(c["body"] for c in data["comments"])
        assert bodies == ["Nice post!", "Thanks!"]

    def test_filter_comments_by_author_name(self, execute, seed):
        data = execute("""
            { comments(filter: {
                object: { author: { field: { name: { exact: "Bob" } } } }
            }) { body } }
        """)
        assert data == {"comments": [{"body": "Nice post!"}]}

    def test_object_inside_all(self, execute, seed):
        data = execute("""
            { posts(filter: {
                all: [
                    { field: { isPublished: { exact: true } } },
                    { object: { author: { field: { name: { exact: "Alice" } } } } }
                ]
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    def test_object_inside_any(self, execute, seed):
        data = execute("""
            { posts(filter: {
                any: [
                    { object: { author: { field: { name: { exact: "Alice" } } } } },
                    { object: { author: { field: { name: { exact: "Charlie" } } } } }
                ]
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World", "Rust Adventures"]

    def test_not_object(self, execute, seed):
        data = execute("""
            { posts(filter: {
                not: { object: { author: { field: { name: { exact: "Alice" } } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["Draft Post", "Rust Adventures"]

    def test_nested_object_traversal(self, execute, seed):
        data = execute("""
            { comments(filter: {
                object: { post: {
                    object: { author: { field: { name: { exact: "Alice" } } } }
                } }
            }) { body } }
        """)
        bodies = sorted(c["body"] for c in data["comments"])
        assert bodies == ["Great guide", "Nice post!", "Thanks!"]

    def test_nested_object_no_match(self, execute, seed):
        data = execute("""
            { comments(filter: {
                object: { post: {
                    object: { author: { field: { name: { exact: "Bob" } } } }
                } }
            }) { body } }
        """)
        assert data == {"comments": []}
