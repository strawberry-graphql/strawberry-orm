"""Unit tests for diagnostics internals shared by every backend."""

from types import SimpleNamespace

from strawberry_orm.backends.sqlalchemy import SQLAlchemyBackend
from strawberry_orm.lazy_resolution import (
    LazyResolutionExtension,
    unloaded_relations,
)


class _ExplodingBackend:
    def relation_names(self, model):
        raise RuntimeError("cannot introspect")


class TestUnloadedRelations:
    def test_returns_empty_when_backend_cannot_introspect(self):
        assert unloaded_relations(_ExplodingBackend(), object()) == set()


class TestShouldProbe:
    def _extension(self, mode="warn"):
        backend = SQLAlchemyBackend(dialect="sqlite")
        return LazyResolutionExtension.configure(backend, mode=mode)()

    def test_no_probe_without_a_field_name(self):
        info = SimpleNamespace(python_name=None, field_name=None)
        assert self._extension()._should_probe(object(), info) is False

    def test_no_probe_when_root_is_not_an_orm_model(self):
        info = SimpleNamespace(python_name="anything", field_name="anything")
        assert self._extension()._should_probe(object(), info) is False

    def test_no_probe_when_mode_is_off(self):
        info = SimpleNamespace(python_name="anything", field_name="anything")
        assert self._extension(mode="off")._should_probe(object(), info) is False

    def test_sqlalchemy_probe_without_a_session_counts_nothing(self):
        backend = SQLAlchemyBackend(dialect="sqlite")
        with backend.query_probe(SimpleNamespace(context={})) as probe:
            pass
        assert probe.count == 0
