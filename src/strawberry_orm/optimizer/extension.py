"""Unified OptimizerExtension that delegates to the active backend."""

from __future__ import annotations

import re
from contextlib import suppress
from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

from strawberry_orm._async import await_maybe
from strawberry_orm.batching import stash_parents

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend
    from strawberry_orm.optimizer.store import OptimizerStore


def extensions_optimizer_index(extensions: list[Any]) -> int | None:
    """Return the index of the optimizer extension in *extensions*, or None."""
    for index, ext in enumerate(extensions):
        if isinstance(ext, type) and issubclass(ext, OptimizerExtension):
            return index
        if getattr(ext, "__name__", "").startswith("OptimizerExtension_"):
            return index
    return None


def extensions_include_optimizer(extensions: list[Any]) -> bool:
    """Return True if *extensions* already contains an optimizer extension."""
    return extensions_optimizer_index(extensions) is not None


def _extensions_from_info(info: Any) -> list[Any]:
    schema = getattr(info, "schema", None)
    if schema is None:
        return []
    strawberry_schema = getattr(schema, "_strawberry_schema", schema)
    return list(getattr(strawberry_schema, "extensions", None) or [])


def schema_includes_optimizer(info: Any) -> bool:
    """Return True when the executing schema has an optimizer extension."""
    return extensions_include_optimizer(_extensions_from_info(info))


def get_configured_optimizer(info: Any) -> tuple[Backend | None, OptimizerStore | None]:
    """Return the backend and store bound to the schema's optimizer extension."""
    extensions = _extensions_from_info(info)
    index = extensions_optimizer_index(extensions)
    if index is None:
        return None, None
    extension = extensions[index]
    return (
        getattr(extension, "_backend", None),
        getattr(extension, "_store", None),
    )


def returns_single_orm_object(backend: Backend, info: Any) -> bool:
    """Return True when the field yields one ORM object rather than a list.

    Backends materialize query objects into lists, so a field annotated
    ``PostType | None`` needs the single row lifted back out. Connections and
    other wrapper types are left alone: their named type is not a registered
    ORM type.
    """
    from graphql import GraphQLList, GraphQLNonNull

    return_type = getattr(info, "return_type", None)
    while isinstance(return_type, GraphQLNonNull):
        return_type = return_type.of_type
    if return_type is None or isinstance(return_type, GraphQLList):
        return False

    name = getattr(return_type, "name", None)
    registry = getattr(backend, "_type_registry", {})
    return name is not None and name in registry


def _reads_a_relation_of_the_parent(backend: Backend, root: Any, info: Any) -> bool:
    """True when this field is a relation hanging off an in-memory parent row."""
    if root is None or not backend.is_model_instance(root):
        return False

    name = getattr(info, "python_name", None) or getattr(info, "field_name", "")
    if not name:
        return False  # pragma: no cover - Info always carries one of the two
    snake = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

    try:
        return snake in backend.relation_names(type(root))
    except Exception:  # pragma: no cover - defensive; unmapped roots
        return False


def _unwrap_single(result: Any, singular: bool) -> Any:
    if not singular or not isinstance(result, list):
        return result
    return result[0] if result else None


# Rows the library has already arranged loading for. Marking them keeps the
# automatic pass off rows that came out of an optimized query - a connection
# resolves one ``node`` field per edge, and re-loading each one would trade a
# saved query for a query per row.
#
# A row reached twice in one operation under different selections is marked
# from the first, so the second falls back to loading relations as it reads
# them. That is slower, not wrong: the lazy path applies the same row scoping.
_PREPARED = "_strawberry_orm_relations_loaded"


def _mark_prepared(rows: Any) -> None:
    for row in rows if isinstance(rows, list) else [rows]:
        # Immutable or slotted rows simply go unmarked and take the slow path.
        with suppress(Exception):
            setattr(row, _PREPARED, True)


def _is_prepared(row: Any) -> bool:
    return getattr(row, _PREPARED, False) is True


def optimize_query_nodes(nodes: Any, info: Any) -> Any:
    """Apply optimizer hints to *nodes* when it is still a backend query object."""
    backend, store = get_configured_optimizer(info)
    if backend is None or store is None or not backend.is_query_object(nodes):
        return nodes
    rows = backend.apply_optimizer_hints(store, nodes, info)
    if isawaitable(rows):

        async def _await_rows() -> Any:
            resolved = await rows
            _mark_prepared(resolved)
            return resolved

        return _await_rows()
    _mark_prepared(rows)
    return rows


class OptimizerExtension(SchemaExtension):
    """A Strawberry schema extension that reads the GraphQL query tree
    and tells the active backend to apply eager-loading / column-selection
    hints before the query is executed.

    The extension intercepts resolver return values. When a resolver returns
    a raw query object (SA ``Select``, Django ``QuerySet``), the optimizer
    walks the GraphQL selection set (including inline fragments and spreads),
    adds eager-loads, and executes the query
    so that downstream resolvers receive model instances instead.
    """

    _backend: Backend | None = None
    _store: OptimizerStore | None = None

    @classmethod
    def configure(
        cls, backend: Backend, store: OptimizerStore
    ) -> type[OptimizerExtension]:
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
        singular = False
        optimized = False
        if (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            singular = returns_single_orm_object(backend, info)
            result = backend.apply_optimizer_hints(self._store, result, info)
            optimized = True
            if not isawaitable(result):
                _mark_prepared(result)
        elif not isawaitable(result):
            result = self._load_rows(result, root, info)

        if isawaitable(result):
            return self._resolve_async(result, root, info, singular, optimized)

        stash_parents(getattr(self, "execution_context", None), info, result)
        return _unwrap_single(result, singular)

    def _load_rows(self, result: Any, root: Any, info: Any) -> Any:
        """Eager-load relations when a resolver hands back rows, not a query.

        A resolver that materializes its rows never gives the optimizer a query
        to add loads to, so every relation below it used to cost a round trip
        per parent. The rows are all that is needed, so load onto them instead.

        This reaches rows nested inside a wrapper too - a payload's ``data`` is
        a resolved field in its own right, so it arrives here with exactly the
        selection that describes those rows.
        """
        backend = self._backend
        if backend is None or self._store is None:
            return result  # pragma: no cover - guarded by the caller

        # Reading a relation off a row that is already in memory: whatever
        # loaded the parent loaded this too, because the lookups are computed
        # for the whole subtree at once. Re-loading here would issue a query
        # per parent to fetch rows that are already present.
        if _reads_a_relation_of_the_parent(backend, root, info):
            return result

        if backend.is_model_instance(result):
            rows: list[Any] = [] if _is_prepared(result) else [result]
            if not rows:
                return result
        elif isinstance(result, list) and result:
            rows = [
                row
                for row in result
                if backend.is_model_instance(row) and not _is_prepared(row)
            ]
            if not rows:
                return result
        else:
            return result

        # Only rows that were actually loaded onto are marked. A selection
        # that named no relations leaves them unmarked, so a later field that
        # does name some still gets its chance.
        loaded = backend.load_relations(self._store, rows, info)
        if isawaitable(loaded):

            async def _await_loaded() -> Any:
                _mark_prepared(await loaded)
                return result

            return _await_loaded()
        _mark_prepared(loaded)
        return result

    async def _resolve_async(
        self,
        result: Any,
        root: Any,
        info: Any,
        singular: bool = False,
        optimized: bool = False,
    ) -> Any:
        result = await await_maybe(result)

        backend = self._backend
        if optimized:
            # The rows were only reachable once the awaitable resolved, so this
            # is the first chance to mark them.
            _mark_prepared(result)
        elif (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            singular = returns_single_orm_object(backend, info)
            result = await await_maybe(
                backend.apply_optimizer_hints(self._store, result, info)
            )
            _mark_prepared(result)
        else:
            result = await await_maybe(self._load_rows(result, root, info))

        stash_parents(getattr(self, "execution_context", None), info, result)
        return _unwrap_single(result, singular)
