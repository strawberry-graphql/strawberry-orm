"""The two ways to declare a field, and the errors that keep them apart.

``orm.field.eager`` is anything the optimizer can plan into one query for the
whole result set, whether the library writes it, a scope narrows it, or a
resolver names what it reads. ``orm.field.lazy`` is the rest: your code, once
per parent row. The name is the contract, so the library checks the callable
against it at decoration time rather than letting a wrong shape surface as a
failure deep inside a prefetch.

``auto`` / ``scoped`` / ``custom`` / ``computed`` remain as aliases and are
still covered here, since existing schemas are written in them.
"""

import pytest


class AbstractTestFieldNamespace:
    """Subclasses provide ``build`` helpers over their backend's models."""

    def test_scoped_narrows_through_the_decorated_form(self):
        """The return annotation declares the field; the body narrows it."""
        assert self.run_scoped_decorator() == ["Nice post!"]

    def test_scoped_narrows_through_the_inline_form(self):
        """Same call, with the type on the attribute instead."""
        assert self.run_scoped_inline() == ["python"]

    def test_custom_receives_the_parent_row(self):
        assert self.run_custom() == "HELLO WORLD"

    def test_computed_eager_loads_what_it_uses(self):
        assert self.run_computed() == "by Alice"

    def test_eager_bare_lets_the_library_resolve_it(self):
        assert self.run_eager_bare() == ["python"]

    def test_eager_folds_a_scope_into_the_prefetch(self):
        assert self.run_eager_scope() == ["Nice post!"]

    def test_lazy_preloads_what_it_declares(self):
        """Still one call per row, but the relation it reads costs no query."""
        assert self.run_lazy_using() == "by Alice"

    def test_lazy_receives_the_parent_row(self):
        assert self.run_lazy() == "HELLO WORLD"

    def test_eager_carries_metadata_without_a_callable(self):
        assert self.run_eager_metadata_only() == ["python"]

    def test_lazy_takes_filter_arguments(self):
        assert self.run_lazy_with_filters() == ["Nice post!"]

    def test_eager_refuses_a_callable_that_takes_self(self):
        """One name, one contract: a parent row means it cannot be eager."""
        with pytest.raises(TypeError, match="is not a scope"):
            self.declare_eager_taking_self()

    def test_scoped_rejects_a_resolver_signature(self):
        with pytest.raises(TypeError, match="never sees the parent row"):
            self.declare_scoped_taking_self()

    def test_custom_rejects_a_scope_signature(self):
        with pytest.raises(TypeError, match="must take self"):
            self.declare_custom_without_self()

    def test_scoped_needs_a_type_from_somewhere(self):
        with pytest.raises(TypeError, match="has no type"):
            self.declare_scoped_without_annotation()

    def test_scope_on_a_field_that_is_not_a_relation_is_rejected(self):
        with pytest.raises(ValueError, match="has no relation"):
            self.declare_scoped_on_a_column()

    def test_scope_receives_info(self):
        """``scope_rows`` has always had info; ``scope`` does now too."""
        assert self.run_scope_reading_info() == ["Nice post!"]


class AbstractTestFieldNamespaceAsync:
    """Async counterpart of :class:`AbstractTestFieldNamespace`."""

    async def test_scoped_narrows_through_the_decorated_form(self):
        assert await self.run_scoped_decorator() == ["Nice post!"]

    async def test_scoped_narrows_through_the_inline_form(self):
        assert await self.run_scoped_inline() == ["python"]

    async def test_custom_receives_the_parent_row(self):
        assert await self.run_custom() == "HELLO WORLD"

    async def test_computed_eager_loads_what_it_uses(self):
        assert await self.run_computed() == "by Alice"

    async def test_eager_bare_lets_the_library_resolve_it(self):
        assert await self.run_eager_bare() == ["python"]

    async def test_eager_folds_a_scope_into_the_prefetch(self):
        assert await self.run_eager_scope() == ["Nice post!"]

    async def test_lazy_preloads_what_it_declares(self):
        """Still one call per row, but the relation it reads costs no query."""
        assert await self.run_lazy_using() == "by Alice"

    async def test_lazy_receives_the_parent_row(self):
        assert await self.run_lazy() == "HELLO WORLD"

    async def test_eager_carries_metadata_without_a_callable(self):
        assert await self.run_eager_metadata_only() == ["python"]

    async def test_lazy_takes_filter_arguments(self):
        assert await self.run_lazy_with_filters() == ["Nice post!"]

    async def test_eager_refuses_a_callable_that_takes_self(self):
        """One name, one contract: a parent row means it cannot be eager."""
        with pytest.raises(TypeError, match="is not a scope"):
            self.declare_eager_taking_self()

    async def test_scoped_rejects_a_resolver_signature(self):
        with pytest.raises(TypeError, match="never sees the parent row"):
            self.declare_scoped_taking_self()

    async def test_custom_rejects_a_scope_signature(self):
        with pytest.raises(TypeError, match="must take self"):
            self.declare_custom_without_self()

    async def test_scoped_needs_a_type_from_somewhere(self):
        with pytest.raises(TypeError, match="has no type"):
            self.declare_scoped_without_annotation()

    async def test_scope_on_a_field_that_is_not_a_relation_is_rejected(self):
        with pytest.raises(ValueError, match="has no relation"):
            self.declare_scoped_on_a_column()

    async def test_scope_receives_info(self):
        assert await self.run_scope_reading_info() == ["Nice post!"]


class AbstractTestScopeIsARowControl:
    """A ``scope=`` on an edge has to hold on every path to those rows.

    Reading the relation applies it. So must filtering through the same
    relation: otherwise the rows it hides can still be confirmed one probe at
    a time, which is the oracle the type-level ``scope_rows`` traversal fix
    closed.
    """

    def test_reading_the_edge_applies_the_scope(self):
        assert self.read_scoped_edge() == [
            "GraphQL Guide",
            "Hello World",
            "Rust Adventures",
        ]

    def test_filtering_through_the_edge_cannot_probe_hidden_rows(self):
        assert self.probe_hidden_through_edge() == [], (
            "a filter reached a row that scope= hides on the read path"
        )

    def test_filtering_through_the_edge_still_finds_visible_rows(self):
        assert self.probe_visible_through_edge() == ["Alice"]

    def test_an_unhashable_scope_is_usable(self):
        """A dataclass makes a natural configurable scope, and is unhashable."""
        assert self.run_dataclass_scope() == [
            "GraphQL Guide",
            "Hello World",
            "Rust Adventures",
        ]


class AbstractTestScopeIsARowControlAsync:
    """Async counterpart of :class:`AbstractTestScopeIsARowControl`."""

    async def test_reading_the_edge_applies_the_scope(self):
        assert await self.read_scoped_edge() == [
            "GraphQL Guide",
            "Hello World",
            "Rust Adventures",
        ]

    async def test_filtering_through_the_edge_cannot_probe_hidden_rows(self):
        assert await self.probe_hidden_through_edge() == [], (
            "a filter reached a row that scope= hides on the read path"
        )

    async def test_filtering_through_the_edge_still_finds_visible_rows(self):
        assert await self.probe_visible_through_edge() == ["Alice"]

    async def test_an_unhashable_scope_is_usable(self):
        assert await self.run_dataclass_scope() == [
            "GraphQL Guide",
            "Hello World",
            "Rust Adventures",
        ]
