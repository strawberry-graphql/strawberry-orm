"""Ref-list mutation tests: link, create inline, update inline, delete, replace, mixed."""


def _sort_tags(data):
    """Sort tags by name for deterministic comparison."""
    data["setPostTags"]["tags"] = sorted(
        data["setPostTags"]["tags"], key=lambda t: t["name"],
    )
    return data


class TestMutationRefList:
    def test_link_existing_tags(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 3, tags: [{ id: "1" }, { id: "3" }]) {
                    title tags { name }
                }
            }
        """)
        assert _sort_tags(data) == {"setPostTags": {
            "title": "Draft Post",
            "tags": [{"name": "python"}, {"name": "rust"}],
        }}

    def test_create_inline_tag(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 1, tags: [
                    { id: "1" },
                    { create: { name: "django" } }
                ]) { tags { name } }
            }
        """)
        assert _sort_tags(data) == {"setPostTags": {
            "tags": [{"name": "django"}, {"name": "python"}],
        }}

    def test_update_inline_tag(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 2, tags: [
                    { id: "1" },
                    { update: { id: "2", name: "gql" } }
                ]) { tags { name } }
            }
        """)
        assert _sort_tags(data) == {"setPostTags": {
            "tags": [{"name": "gql"}, {"name": "python"}],
        }}

    def test_delete_related_tag(self, execute, seed, sa_session, Tag):
        data = execute("""
            mutation {
                setPostTags(postId: 2, tags: [
                    { id: "1" },
                    { delete: { id: "2" } }
                ]) { tags { name } }
            }
        """)
        assert data == {"setPostTags": {"tags": [{"name": "python"}]}}
        tag2 = sa_session.get(Tag, 2)
        assert tag2 is None

    def test_replace_all_tags(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 2, tags: [{ id: "3" }]) {
                    tags { name }
                }
            }
        """)
        assert data == {"setPostTags": {"tags": [{"name": "rust"}]}}

    def test_mixed_operations(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 1, tags: [
                    { id: "3" },
                    { create: { name: "fastapi" } },
                    { update: { id: "1", name: "py" } }
                ]) { tags { name } }
            }
        """)
        assert _sort_tags(data) == {"setPostTags": {
            "tags": [{"name": "fastapi"}, {"name": "py"}, {"name": "rust"}],
        }}
