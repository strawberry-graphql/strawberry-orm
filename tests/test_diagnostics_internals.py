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


class TestQuerySelectionPath:
    """The path a report names is built from the field's own place, and only
    as much of what it selects as a report prints."""

    @staticmethod
    def _info(document: str, *keys: str):
        from graphql import parse

        operation = parse(document).definitions[0]
        node = operation
        for key in keys:
            node = next(
                s
                for s in node.selection_set.selections
                if (s.alias or s.name).value == key
            )
        path = None
        for key in keys:
            path = SimpleNamespace(key=key, prev=path)
        return SimpleNamespace(
            operation=operation,
            path=path,
            field_nodes=[node],
            field_name=node.name.value,
            python_name=node.name.value,
        )

    def test_a_field_selected_over_and_over_is_printed_once_cut_short(self):
        from strawberry_orm.lazy_resolution import _MOST_ECHOED, _query_selection_path

        names = " ".join(["name"] * 50_000)
        info = self._info(f"{{ posts {{ author {{ {names} }} }} }}", "posts", "author")
        path = _query_selection_path(info)
        assert path.startswith("query { posts { author { name name")
        assert len(path) < _MOST_ECHOED + 60

    def test_an_aliased_field_is_named_by_its_alias(self):
        from strawberry_orm.lazy_resolution import _query_selection_path

        info = self._info(
            "query Q { posts { a1: author { name } a2: author { id } } }", "posts", "a2"
        )
        assert _query_selection_path(info) == "query Q { posts { a2 { id } } }"
