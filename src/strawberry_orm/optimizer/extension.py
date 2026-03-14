"""Unified OptimizerExtension that delegates to the active backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend
    from strawberry_orm.optimizer.store import OptimizerStore


class OptimizerExtension(SchemaExtension):
    """A Strawberry schema extension that reads the GraphQL query tree
    and tells the active backend to apply eager-loading / column-selection
    hints before the query is executed.

    The extension intercepts resolver return values. When a resolver returns
    a raw query object (SA ``Select``, Django ``QuerySet``), the optimizer
    walks ``info.selected_fields``, adds eager-loads, and executes the query
    so that downstream resolvers receive model instances instead.
    """

    _backend: Backend | None = None
    _store: OptimizerStore | None = None

    @classmethod
    def configure(cls, backend: Backend, store: OptimizerStore) -> type[OptimizerExtension]:
        """Return a configured subclass bound to a specific backend/store."""
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend, "_store": store},
        )

    def on_execute(self) -> Any:
        yield

    def resolve(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        result = _next(root, info, *args, **kwargs)

        backend = self._backend
        if backend is None:
            return result

        if backend.is_query_object(result) and self._store is not None:
            result = backend.apply_optimizer_hints(self._store, result, info)

        return result
