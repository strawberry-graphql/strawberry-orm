"""Tests for the optimizer store and extension."""

from __future__ import annotations

from strawberry_orm.optimizer import FieldHints, OptimizerStore


class TestOptimizerStore:
    def test_register_and_get(self):
        store = OptimizerStore()
        hints = FieldHints(using=["posts", "tags"])
        store.register("UserType", "posts", hints)

        retrieved = store.get("UserType", "posts")
        assert retrieved is hints
        assert retrieved.using == ["posts", "tags"]

    def test_get_missing_returns_none(self):
        store = OptimizerStore()
        assert store.get("UserType", "nonexistent") is None
        assert store.get("Nonexistent", "field") is None

    def test_multiple_types(self):
        store = OptimizerStore()
        store.register("UserType", "posts", FieldHints(using=["posts"]))
        store.register("PostType", "author", FieldHints(using=["author"]))

        assert store.get("UserType", "posts") is not None
        assert store.get("PostType", "author") is not None
        assert store.get("UserType", "author") is None

    def test_disable_optimization(self):
        store = OptimizerStore()
        hints = FieldHints(disable_optimization=True)
        store.register("UserType", "custom_field", hints)

        retrieved = store.get("UserType", "custom_field")
        assert retrieved is not None
        assert retrieved.disable_optimization is True
