"""Related list mutation (orm.ref) tests: link, create, update, unlink, delete, mixed."""


def _sort_tags(data):
    """Sort tags by name for deterministic comparison."""
    data["setPostTags"]["tags"] = sorted(
        data["setPostTags"]["tags"],
        key=lambda t: t["name"],
    )
    return data


class TestMutationRefList:
    def test_link_existing_tags(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 3, tags: [{ update: { id: "1" } }]) {
                    title tags { name }
                }
            }
        """)
        assert data == {
            "setPostTags": {
                "title": "Draft Post",
                "tags": [{"name": "python"}],
            }
        }

    def test_create_inline_tag(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 3, tags: [{ create: { name: "new-tag" } }]) {
                    title tags { name }
                }
            }
        """)
        assert data == {
            "setPostTags": {
                "title": "Draft Post",
                "tags": [{"name": "new-tag"}],
            }
        }

    def test_update_inline_tag(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 1, tags: [
                    { update: { id: "1", name: "python3" } }
                ]) {
                    tags { name }
                }
            }
        """)
        assert data == {"setPostTags": {"tags": [{"name": "python3"}]}}

    def test_unlink_related_tag(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 1, tags: [{ unlink: { id: "1" } }]) {
                    tags { name }
                }
            }
        """)
        assert data == {"setPostTags": {"tags": []}}

    def test_delete_related_tag(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 1, tags: [{ delete: { id: "1" } }]) {
                    tags { name }
                }
            }
        """)
        assert data == {"setPostTags": {"tags": []}}

    def test_mixed_operations(self, execute, seed):
        data = execute("""
            mutation {
                setPostTags(postId: 2, tags: [
                    { update: { id: "2" } },
                    { create: { name: "testing" } },
                    { update: { id: "1", name: "python3" } }
                ]) {
                    tags { name }
                }
            }
        """)
        assert _sort_tags(data) == {
            "setPostTags": {
                "tags": [{"name": "graphql"}, {"name": "python3"}, {"name": "testing"}],
            }
        }
