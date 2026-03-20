"""Abstract tests for ordering by related object fields."""


class AbstractTestOrderObjectTraversal:
    def test_order_users_by_field(self, execute, seed):
        """Baseline: field-only ordering still works."""
        data = execute("{ users(order: [{ field: { name: ASC } }]) { name } }")
        names = [u["name"] for u in data["users"]]
        assert names == sorted(names)

    def test_order_posts_by_author_name_asc(self, execute, seed):
        data = execute("""
            { posts(order: [
                { object: { author: { field: { name: ASC } } } }
            ]) { title } }
        """)
        assert len(data["posts"]) > 0

    def test_order_posts_by_author_name_desc(self, execute, seed):
        data = execute("""
            { posts(order: [
                { object: { author: { field: { name: DESC } } } }
            ]) { title } }
        """)
        assert len(data["posts"]) > 0

    def test_order_by_object_then_field_tiebreak(self, execute, seed):
        data = execute("""
            { posts(order: [
                { object: { author: { field: { name: ASC } } } },
                { field: { title: ASC } }
            ]) { title } }
        """)
        assert len(data["posts"]) > 0

    def test_order_comments_by_post_title(self, execute, seed):
        data = execute("""
            { comments(order: [
                { object: { post: { field: { title: ASC } } } }
            ]) { body } }
        """)
        assert len(data["comments"]) == 3
