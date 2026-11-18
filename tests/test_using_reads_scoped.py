"""Branches of the check a ``using=`` resolver's relations pass that schemas do not reach."""

import pytest

from strawberry_orm.backends._base import BaseBackend
from strawberry_orm.fields import every_scope


class _Row:
    pass


class _Backend(BaseBackend):
    """Rows with one relation, ``author``, that another load keeps replacing."""

    def __init__(self, *, scoped: bool = True) -> None:
        super().__init__(warn_missing_scope=False)
        self.scoped = scoped
        self.reads: list[str] = []

    def _introspect_model(self, model):  # pragma: no cover - unused
        return []

    def is_model_instance(self, value):
        return isinstance(value, _Row)

    def relation_names(self, model):
        return {"author"}

    def _relation_target_model(self, model, relation):
        return _Row

    def _resolved_type_name(self, model, info):
        return None

    def relation_scope(self, model, relation, info, *, on=None, type_name=None):
        return (lambda query, info: query) if self.scoped else None

    def relation_holds_scoped(self, row, name, token):
        return False

    def read_relation_scoped(self, rows, model, name, restrict, token, info):
        self.reads.append(name)


def test_a_resolver_reading_no_rows_runs_as_it_is():
    backend = _Backend()
    assert backend.resolve_using(object(), ["author"], None, lambda: "read") == "read"
    assert backend.resolve_using(_Row(), [], None, lambda: "read") == "read"
    assert backend.reads == []


def test_neither_a_column_nor_an_unscoped_relation_is_read_again():
    backend = _Backend(scoped=False)
    paths = ["author", "title"]
    assert backend.resolve_using(_Row(), paths, None, lambda: "read") == "read"
    assert backend.reads == []


def test_rows_other_loads_keep_replacing_are_refused():
    backend = _Backend()
    with pytest.raises(RuntimeError, match="kept replacing the relations"):
        backend.resolve_using(_Row(), ["author"], None, lambda: "read")
    assert backend.reads == ["author"] * 3


def test_the_same_scopes_compose_to_the_same_one():
    def first(query, info):
        return [*query, "first"]

    def second(query, info):
        return [*query, "second"]

    assert every_scope([first, second]) is every_scope([first, second])
    assert every_scope([first, second]) is not every_scope([second, first])
    assert every_scope([first, second])([], None) == ["first", "second"]


def test_unhashable_scopes_still_compose():
    class Narrowing:
        __hash__ = None

        def __call__(self, query, info):
            return [*query, "narrowed"]

    composed = every_scope([Narrowing(), Narrowing()])
    assert composed([], None) == ["narrowed", "narrowed"]
