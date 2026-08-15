"""Regression tests for authorization and data-exposure defects.

Each of these was a way to read data the schema author intended to hide. They
share a shape: the *output* side is scoped correctly, so nothing looks wrong
until the data is approached from another direction - a second GraphQL type, an
aggregate, a filter, or a join.

Subclasses supply ``orm``/``orm_factory`` plus these hooks:

``scope_to_published(qs)``    narrow a queryset the backend's own way
``sensitive_model()``         a model carrying ``ssn`` / ``credit_card``
``build_traversal_schema()``  a schema whose filter can traverse into a scoped type
"""

import pytest

from strawberry_orm.types import auto


class AbstractTestSecurityVulnerabilities:
    def test_two_types_cannot_share_a_model_with_different_scoping(self, orm, Post):
        """Row scoping is resolved per model, not per GraphQL type.

        A second type over the same model used to overwrite the first, so a
        restrictive public type silently inherited a permissive admin scope and
        started returning rows it was written to hide.
        """
        scope = self.scope_to_published

        @orm.type(Post, name="PublicPost")
        class PublicPost:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return scope(qs)

        with pytest.raises(ValueError, match=r"both define scope_rows"):

            @orm.type(Post, name="AdminPost")
            class AdminPost:
                id: auto
                title: auto

                @classmethod
                def scope_rows(cls, qs, info):
                    return qs

    def test_one_type_per_model_still_works(self, orm, Post):
        scope = self.scope_to_published

        @orm.type(Post, name="OnlyPost")
        class OnlyPost:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return scope(qs)

        assert Post in orm.backend._type_querysets

    def test_excluded_field_is_not_left_filterable(self, orm_factory, User):
        """Hiding a column from the output while leaving it filterable turns it
        into an oracle: ``startsWith`` probes read it one character at a time."""
        orm = orm_factory()
        filters = orm.filter(User)

        with pytest.raises(ValueError, match=r"excludes \['email'\]"):

            @orm.type(User, exclude=["email"], filters=filters)
            class UT:
                id: auto
                name: auto

    def test_excluded_field_is_fine_when_the_filter_excludes_it_too(
        self, orm_factory, User
    ):
        orm = orm_factory()
        filters = orm.filter(User, exclude=["email"])

        @orm.type(User, exclude=["email"], filters=filters)
        class UT:
            id: auto
            name: auto

        assert "email" not in orm.backend._generated_input_field_names(filters)

    def test_excluded_field_is_not_left_orderable(self, orm_factory, User):
        orm = orm_factory()
        order = orm.order(User)

        with pytest.raises(ValueError, match=r"excludes \['email'\]"):

            @orm.type(User, exclude=["email"], order=order)
            class UT:
                id: auto
                name: auto

    def test_sensitive_columns_are_not_aggregatable(self, orm_factory):
        """``min``/``max`` return exact values, so a sensitive numeric column is
        as exposed through aggregates as it would be on the output type."""
        orm = orm_factory()
        meta = orm.backend._build_aggregate_types(self.sensitive_model())

        for sub_type in (meta.min_type, meta.max_type, meta.sum_type, meta.avg_type):
            fields = set(getattr(sub_type, "__annotations__", {}))
            assert "ssn" not in fields, "min/max would return the exact value"
            assert "credit_card" not in fields

    def test_sensitive_columns_are_not_group_keys(self, orm_factory):
        orm = orm_factory()
        meta = orm.backend._build_aggregate_types(self.sensitive_model())
        assert "ssn" not in dict(meta.groupable_fields)
        assert "credit_card" not in dict(meta.groupable_fields)

    def test_non_sensitive_columns_still_aggregate(self, orm_factory):
        orm = orm_factory()
        meta = orm.backend._build_aggregate_types(self.sensitive_model())
        assert "salary" in dict(meta.numeric_fields)

    def test_excluded_field_is_not_left_writable(self, orm_factory, User):
        """Excluding a column is a *read* control; the generated mutation input
        is unaffected, so the field stays writable. A caller could then set a
        value they are not allowed to read back - mass assignment."""
        orm = orm_factory()

        @orm.type(User, exclude=["email"])
        class UT:
            id: auto
            name: auto

        with pytest.raises(ValueError, match=r"can write \['email'\]"):
            orm.input(User)

    def test_input_built_before_the_type_is_caught_too(self, orm_factory, User):
        orm = orm_factory()
        orm.input(User)

        with pytest.raises(ValueError, match=r"can still write"):

            @orm.type(User, exclude=["email"])
            class UT:
                id: auto
                name: auto

    def test_matching_excludes_on_input_are_accepted(self, orm_factory, User):
        orm = orm_factory()

        @orm.type(User, exclude=["email"])
        class UT:
            id: auto
            name: auto

        generated = orm.input(User, exclude=["email"])
        assert "email" not in getattr(generated, "__annotations__", {})

    def test_a_second_wider_filter_cannot_reopen_a_narrowed_model(
        self, orm_factory, User
    ):
        """Nested ``object:`` traversal reuses one filter per model.

        A narrower filter can therefore be undone by any later, broader
        ``orm.filter()`` call for the same model - re-exposing the column
        through *other* types' filters even though this type excluded it.
        """
        orm = orm_factory()
        orm.filter(User, exclude=["email"])

        with pytest.raises(ValueError, match=r"already excludes \['email'\]"):
            orm.filter(User)

    def test_rebuilding_the_same_filter_shape_is_allowed(self, orm_factory, User):
        orm = orm_factory()
        orm.filter(User, exclude=["email"])
        again = orm.filter(User, exclude=["email"])
        assert "email" not in orm.backend._generated_input_field_names(again)

    def test_unknown_relation_name_does_not_break_the_audit(self, orm_factory, Post):
        """The audit walks filter annotations, which may not all be relations."""
        backend = orm_factory().backend
        assert backend._relation_target_model(Post, "not_a_relation") is None

    def test_filter_traversal_into_a_scoped_type_builds_without_warning(
        self, orm_factory
    ):
        """Traversal is scoped at query time (see ``filter_traversal_scoping``),
        so exposing it is no longer something to warn about."""
        assert self.build_traversal_schema(orm_factory()) == []
