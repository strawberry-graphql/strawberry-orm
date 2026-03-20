"""Abstract tests for filter project logic (controlling which relations appear
in the object type)."""


class AbstractTestFilterProjectTypeGeneration:
    """Verify that ``project`` controls which relations appear in ``object``."""

    def test_project_none_includes_registered_relations(self, orm, User, Post):
        orm.filter(User)
        PostFilter = orm.filter(Post)
        assert hasattr(PostFilter, "_object_type")
        obj_fields = set(PostFilter._object_type.__dataclass_fields__.keys())
        assert "author" in obj_fields

    def test_project_empty_dict_removes_object(self, orm, User, Post):
        orm.filter(User)
        PostFilter = orm.filter(Post, project={})
        assert not hasattr(PostFilter, "_object_type")

    def test_project_limits_to_listed_relations(self, orm, User, Post, Tag, Comment):
        orm.filter(User)
        orm.filter(Tag)
        orm.filter(Comment)
        PostFilter = orm.filter(Post, project={"author": {}})
        assert hasattr(PostFilter, "_object_type")
        obj_fields = set(PostFilter._object_type.__dataclass_fields__.keys())
        assert obj_fields == {"author"}

    def test_project_unknown_relation_raises(self, orm, User):
        import pytest

        with pytest.raises(ValueError, match="Unknown relation"):
            orm.filter(User, project={"nonexistent": {}})

    def test_project_does_not_register_in_global_registry(self, orm, User, Post):
        orm.filter(User)
        base = orm.filter(Post)
        projected = orm.filter(Post, project={"author": {}})
        assert projected is not base
        assert orm._backend._filter_registry[Post] is base

    def test_projected_filter_cached(self, orm, User, Post):
        orm.filter(User)
        orm.filter(Post)
        p1 = orm.filter(Post, project={"author": {}})
        p2 = orm.filter(Post, project={"author": {}})
        assert p1 is p2

    def test_nested_project_limits_child_object(self, orm, User, Post, Tag, Comment):
        orm.filter(User)
        orm.filter(Tag)
        orm.filter(Comment)
        orm.filter(Post)
        CommentFilter = orm.filter(
            Comment,
            project={"post": {"author": {}}},
        )
        assert hasattr(CommentFilter, "_object_type")
        obj_fields = set(CommentFilter._object_type.__dataclass_fields__.keys())
        assert obj_fields == {"post"}


class AbstractTestFilterProjectQueries:
    """Run real queries using projected filters to verify end-to-end behavior."""

    def test_projected_filter_query(self, execute_projected, seed):
        data = execute_projected("""
            { posts(filter: {
                object: { author: { field: { name: { exact: "Alice" } } } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    def test_projected_filter_excludes_unprojected_relation(
        self, execute_projected, seed
    ):
        result = execute_projected(
            """
            { posts(filter: {
                object: { tags: { field: { name: { exact: "python" } } } }
            }) { title } }
            """,
            expect_errors=True,
        )
        assert result is not None
