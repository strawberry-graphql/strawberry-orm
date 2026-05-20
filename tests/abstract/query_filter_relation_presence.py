"""Abstract tests for FK presence (is_null) on relation filters under object."""

import pytest


class AbstractTestQueryFilterRelationPresence:
    """Filter parents by whether a FK relation is set via object.<relation>.isNull."""

    def test_object_relation_is_null_false(self, execute, seed):
        """All posts have an author."""
        data = execute("""
            { posts(filter: {
                object: { author: { isNull: false } }
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == [
            "Draft Post",
            "GraphQL Guide",
            "Hello World",
            "Rust Adventures",
        ]

    def test_object_relation_is_null_true_on_nullable_fk(self, execute, seed):
        """Comments without a parent reply (parent_id is null)."""
        data = execute("""
            { comments(filter: {
                object: { parent: { isNull: true } }
            }) { body } }
        """)
        bodies = sorted(c["body"] for c in data["comments"])
        assert bodies == ["Great guide", "Nice post!"]

    def test_object_relation_is_null_false_on_nullable_fk(self, execute, seed):
        data = execute("""
            { comments(filter: {
                object: { parent: { isNull: false } }
            }) { body } }
        """)
        assert data == {"comments": [{"body": "Thanks!"}]}

    def test_object_relation_is_null_inside_all(self, execute, seed):
        data = execute("""
            { comments(filter: {
                all: [
                    { object: { parent: { isNull: true } } },
                    { field: { body: { contains: "guide" } } }
                ]
            }) { body } }
        """)
        assert data == {"comments": [{"body": "Great guide"}]}

    def test_object_relation_is_null_not(self, execute, seed):
        data = execute("""
            { comments(filter: {
                not: { object: { parent: { isNull: true } } }
            }) { body } }
        """)
        assert data == {"comments": [{"body": "Thanks!"}]}

    def test_object_relation_is_null_with_field_predicate(self, execute, seed):
        data = execute("""
            { posts(filter: {
                all: [
                    { object: { author: { isNull: false } } },
                    { object: { author: { field: { name: { exact: "Alice" } } } } }
                ]
            }) { title } }
        """)
        titles = sorted(p["title"] for p in data["posts"])
        assert titles == ["GraphQL Guide", "Hello World"]

    def test_filter_type_relation_is_null(self, execute, seed):
        """filter_type with relation auto supports object.<relation>.isNull."""
        data = execute("""
            { comments(filter: {
                object: { parent: { isNull: true } }
            }) { body } }
        """)
        bodies = sorted(c["body"] for c in data["comments"])
        assert bodies == ["Great guide", "Nice post!"]

    def test_root_is_null_raises(self, execute):
        with pytest.raises(AssertionError, match="is_null|isNull|GraphQL errors"):
            execute("{ posts(filter: { isNull: false }) { title } }")

    def test_filter_schema_exposes_is_null_on_relation_filter(self, orm, User):
        UserFilter = orm.filter(User)
        definition = UserFilter.__strawberry_definition__
        field_names = {f.name for f in definition.fields}
        assert "is_null" in field_names
