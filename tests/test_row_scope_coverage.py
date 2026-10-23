"""Branches of the row-scope markers that schema tests do not reach."""

from types import SimpleNamespace

from strawberry_orm import StrawberryORM
from strawberry_orm.backends._base import BaseBackend
from strawberry_orm.lazy_resolution import (
    mark_query_scoped,
    mark_relations_scoped,
    query_is_scoped,
    remember_scoped_edges,
    take_scoped_edges,
)
from strawberry_orm.optimizer.extension import (
    OptimizerExtension,
    RowScopeExtension,
    _without_rejected,
    extensions_include_row_scope,
)


class _Slotted:
    __slots__ = ("__weakref__",)


def test_query_scope_markers_survive_objects_that_reject_attributes():
    mark_relations_scoped([], ["title"])
    mark_relations_scoped([None, object()], [])
    mark_relations_scoped([None], ["title"])

    slotted = _Slotted()
    mark_query_scoped(slotted)
    assert query_is_scoped(slotted) is True

    assert query_is_scoped([]) is False
    mark_query_scoped([])
    assert query_is_scoped([]) is False
    remember_scoped_edges([], [(_Slotted, "title")])
    assert take_scoped_edges([]) == []
    remember_scoped_edges(slotted, [])


def test_row_scope_extension_detection_and_rejected_singletons():
    assert extensions_include_row_scope([RowScopeExtension]) is True
    named = type("RowScopeExtension_Custom", (), {})
    assert extensions_include_row_scope([named]) is True
    assert extensions_include_row_scope([object()]) is False

    row = object()
    filtered, to_load = _without_rejected(row, [row], [])
    assert filtered is None
    assert to_load == []

    extension = OptimizerExtension.__new__(OptimizerExtension)
    extension._backend = object()
    extension._store = object()
    assert extension._load_kept(row, [row], [], None) is None


def test_hint_validation_stops_when_the_far_side_is_unknown():
    class _Backend(BaseBackend):
        def __init__(self) -> None:
            super().__init__(warn_missing_scope=False)

        def _introspect_model(self, model):  # pragma: no cover - unused
            return []

        def _relation_target_model(self, model, relation):
            return None

        def relation_names(self, model):
            return {"author"}

    backend = _Backend()
    backend._strict_hints = True
    backend._validate_hints(
        object,
        "PostType",
        "byline",
        SimpleNamespace(using=["author.posts"]),
    )


def test_backends_report_when_a_query_has_no_model():
    assert BaseBackend.query_model(SimpleNamespace(), None) is None

    sqlalchemy = StrawberryORM.for_sqlalchemy(dialect="sqlite")
    assert sqlalchemy._backend.query_model(object()) is None
    assert sqlalchemy._backend._unloaded_snapshot(SimpleNamespace(), [object()]) == {}

    tortoise = StrawberryORM.for_tortoise()
    assert tortoise._backend.query_model(SimpleNamespace(model=int)) is int
