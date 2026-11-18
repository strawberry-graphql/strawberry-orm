"""Unified OptimizerExtension that delegates to the active backend."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Iterator
from contextlib import suppress
from inspect import isawaitable, iscoroutine
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from graphql import GraphQLList, GraphQLNonNull
from graphql.pyutils import is_iterable
from strawberry.extensions import SchemaExtension
from strawberry.relay import Connection

from strawberry_orm._async import await_maybe
from strawberry_orm.batching import stash_parents
from strawberry_orm.lazy_resolution import (
    ANY_SCOPE,
    execution_scope,
    open_subscription_event,
    relation_read_by,
    row_is_scoped,
    row_mark,
    row_mark_holds,
)
from strawberry_orm.optimizer.selections import graphql_resolve_info

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


#: Per schema, whether one of its extensions holds the rows it serves to scope.
_HOLDS_ROWS: WeakKeyDictionary[Any, bool] = WeakKeyDictionary()


def refuse_rows_held_by_nothing(info: Any, type_: type) -> None:
    """Refuse a row of *type_* that no extension of the executing schema holds to scope.

    ``scope_rows`` is applied by the extensions ``orm.schema()`` installs, or
    ``orm.optimizer_extension()`` in a ``strawberry.Schema``. A schema with
    neither would serve every row a resolver returns, hidden or not, and
    every row its relations reach.
    """
    schema = info.schema
    held = _HOLDS_ROWS.get(schema)
    if held is None:
        extensions = _extensions_from_info(info)
        held = _HOLDS_ROWS[schema] = extensions_include_row_scope(
            extensions
        ) or extensions_include_optimizer(extensions)
    if not held:
        raise TypeError(
            f"Cannot serve {type_.__name__}: no extension of this schema holds its rows to "
            "scope_rows. Build the schema with orm.schema(), or give strawberry.Schema "
            "orm.optimizer_extension()."
        )


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


def extensions_include_row_scope(extensions: list[Any]) -> bool:
    """Return True if *extensions* already contains a row-scope extension."""
    for ext in extensions:
        if isinstance(ext, type) and issubclass(ext, RowScopeExtension):
            return True
        if getattr(ext, "__name__", "").startswith("RowScopeExtension_"):
            return True
    return False


def _paged_in_scope(connection: Any, info: Any) -> Any:
    """*connection*, refused if its field paged rows not yet held to scope.

    Its edges resolve below it, each ``node`` checked on its own, and a node
    the scope hides would show up as a hole in the page.
    """
    from strawberry_orm.relay.connection import refuse_unscoped_page

    refuse_unscoped_page(info)
    return connection


def _without_rejected(
    result: Any, candidates: list[Any], kept: list[Any], info: Any = None
) -> tuple[Any, list[Any]]:
    """Remove *candidates* that ``scope_rows`` did not keep.

    A list is filtered into a new one (see :func:`_rebuilt`), and so is each
    list nested in it. Never in place: the list is the app's, which may hand
    the same one to the next request, whose scope can allow more, or the
    ORM's own collection, which would record the rows taken out of it as a
    change to write. A single rejected instance becomes ``None``, and so does
    a rejected row of a list whose positions answer the IDs asked for.
    """
    kept_ids = {id(row) for row in kept}
    dropped = {id(row) for row in candidates if id(row) not in kept_ids}
    if isinstance(result, list):
        if dropped:
            by_position = info is not None and _answers_by_position(info)
            result = _without(result, dropped, by_position)
        return result, [row for row in rows_in(result) if id(row) in kept_ids]
    if dropped:
        return None, []
    return result, list(kept)


def _without(rows: list[Any], dropped: set[int], by_position: bool) -> list[Any]:
    """A copy of *rows*, and of the lists nested in it, less the *dropped* rows.

    *by_position* leaves ``None`` where a dropped row was.
    """
    return _rebuilt(
        rows,
        [
            _without(row, dropped, by_position)
            if isinstance(row, list)
            else None
            if id(row) in dropped
            else row
            for row in rows
            if by_position or id(row) not in dropped
        ],
        dropped=0 if by_position else sum(id(row) in dropped for row in rows),
    )


def _rebuilt(rows: list[Any], items: list[Any], *, dropped: int = 0) -> list[Any]:
    """A new list of *items*, to stand in for *rows*, of which *dropped* were left out.

    Only the library's own ``PreslicedRows`` keeps its type, and the page it
    carries the place and total of. Any other list comes back a plain one: a
    subclass of ``list`` may be an ORM's collection, which records each change
    made to it against the row holding it, and a copy of SQLAlchemy's still
    records them there.
    """
    from strawberry_orm.relay.connection import PreslicedRows

    if isinstance(rows, PreslicedRows):
        return PreslicedRows(items, rows.orm_total_count - dropped, rows.orm_offset)
    return list(items)


def rows_in(rows: list[Any]) -> Iterator[Any]:
    """What *rows* holds, the rows of the lists nested in it included."""
    for row in rows:
        if isinstance(row, list):
            yield from rows_in(row)
        else:
            yield row


def _answers_by_position(info: Any) -> bool:
    """True for a relay ``node()`` field, which answers each ID in its place.

    A row dropped from it would move the answers after it up, and set apart
    an ID whose row is hidden from one with no row, which is answered ``null``.
    """
    from strawberry.relay.fields import NodeExtension

    raw = graphql_resolve_info(info)
    field = raw.parent_type.fields.get(raw.field_name)
    definition = field.extensions.get("strawberry-definition") if field else None
    return any(
        isinstance(extension, NodeExtension)
        for extension in getattr(definition, "extensions", ())
    )


def _list_depth(info: Any) -> int:
    """How many lists the field's type nests: one for ``[Post]``, two for ``[[Post]]``."""
    depth = 0
    return_type = getattr(info, "return_type", None)
    while isinstance(return_type, (GraphQLList, GraphQLNonNull)):
        depth += isinstance(return_type, GraphQLList)
        return_type = return_type.of_type
    return depth


def read_rows(result: Any) -> Any:
    """What an iterable *result* yields, as a list, or ``None`` if it is not one.

    An async iterable gives an awaitable of the list. Strings and mappings are
    not read, as graphql-core does not read them as lists either.
    """
    if isinstance(result, AsyncIterable):

        async def _collect() -> list[Any]:
            return [row async for row in result]

        return _collect()
    if is_iterable(result):
        return list(result)
    return None


def settled(result: Any, depth: int, backend: Backend) -> Any:
    """*result* as graphql-core serves it, with nothing in it left to await.

    graphql-core awaits what a field resolves to, reads the iterable a list
    field resolves to, an async one included, and awaits each item of it
    that is awaitable, in every list of a list of lists; *depth* is how many
    lists the field's type nests. What it awaits can give an awaitable in
    turn, which the library or graphql-core awaits too, so a row behind any
    number of them reaches the client just as a row returned as it is. They
    are all awaited here, before anything looks for the rows to check.

    A list's items are awaited together, as graphql-core awaits them, so the
    loads a DataLoader is asked for at once are still batched. An item that
    raises leaves its exception in its place, which graphql-core reports as
    that item's error. A row is never awaited, though a Tortoise row is
    awaitable, and neither is a query *result* is or resolves to, which the
    caller scopes as it reads it. Returns *result* when nothing in it is left
    to await or read, and otherwise a coroutine of it settled.
    """
    return _settled(result, depth, backend, top=True)


def _settled(result: Any, depth: int, backend: Backend, *, top: bool = False) -> Any:
    if top and backend.is_query_object(result):
        return result
    if isawaitable(result):
        if backend.is_model_instance(result):
            return result
        return _awaited(result, depth, backend, top)
    if depth == 0:
        return result
    if not isinstance(result, list):
        read = read_rows(result)
        return result if read is None else _settled(read, depth, backend)
    items = [_settled(item, depth - 1, backend) for item in result]
    if all(new is item for new, item in zip(items, result, strict=True)):
        return result
    if any(iscoroutine(item) for item in items):
        return _gathered(result, items)
    return _rebuilt(result, items)


async def _awaited(result: Any, depth: int, backend: Backend, top: bool) -> Any:
    found = _settled(await result, depth, backend, top=top)
    return await found if iscoroutine(found) else found


async def _gathered(result: list[Any], items: list[Any]) -> list[Any]:
    """*result* rebuilt of *items*, the coroutines among them awaited together."""
    done = iter(
        await asyncio.gather(
            *(item for item in items if iscoroutine(item)), return_exceptions=True
        )
    )
    return _rebuilt(
        result, [next(done) if iscoroutine(item) else item for item in items]
    )


def load_kept(
    backend: Backend,
    store: OptimizerStore,
    result: Any,
    candidates: list[Any],
    kept: list[Any],
    info: Any,
) -> Any:
    """Drop the *candidates* ``scope_rows`` rejected, then eager-load the rest.

    Only rows that were actually loaded onto are marked. A selection that
    named no relations leaves them unmarked, so a later field that does name
    some still gets its chance; a rejected row is never marked, so it is
    checked again wherever it turns up.
    """
    filtered, to_load = _without_rejected(result, candidates, kept, info)
    if not to_load:
        return filtered
    loaded = backend.load_relations(store, to_load, info)
    if isawaitable(loaded):

        async def _await_loaded() -> Any:
            _mark_prepared(await loaded)
            return filtered

        return _await_loaded()
    _mark_prepared(loaded)
    return filtered


def scope_then_load(
    backend: Backend,
    store: OptimizerStore,
    result: Any,
    rows: list[Any],
    info: Any,
) -> Any:
    """Check *rows*, found in *result*, against ``scope_rows``, then load them.

    Rows that were read rather than queried for never passed through
    ``apply_optimizer_hints``, so their scope has not run. It runs before any
    relation is loaded onto them, as loading is what marks a row prepared.
    """
    scoped = backend.scope_instances(rows, info)
    if isawaitable(scoped):

        async def _await_scoped() -> Any:
            kept = await scoped
            return await await_maybe(
                load_kept(backend, store, result, rows, kept, info)
            )

        return _await_scoped()
    return load_kept(backend, store, result, rows, scoped, info)


def returns_single_orm_object(backend: Backend, info: Any) -> bool:
    """Return True when the field yields one ORM object rather than a list.

    Backends materialize query objects into lists, so a field annotated
    ``PostType | None`` needs the single row lifted back out. Connections and
    other wrapper types are left alone: their named type is not a registered
    ORM type.
    """
    return_type = getattr(info, "return_type", None)
    while isinstance(return_type, GraphQLNonNull):
        return_type = return_type.of_type
    if return_type is None or isinstance(return_type, GraphQLList):
        return False

    name = getattr(return_type, "name", None)
    registry = getattr(backend, "_type_registry", {})
    return name is not None and name in registry


#: Cached ``(model class, relation) -> is one of its relations`` verdicts. The
#: model's relation names are fixed by the time rows are being resolved, so
#: this is computed once rather than once per field per row.
_RELATION_VERDICTS: dict[tuple[type, str], bool] = {}


def _relation_of_the_parent(backend: Backend, root: Any, info: Any) -> str | None:
    """The relation the library's own resolver read off a loaded row, if it did.

    That resolver applied the scope itself, an ``on=`` view under a name of its
    own included. A field the app resolves is never taken on trust, whatever
    it is called: its rows can come from anywhere.
    """
    if root is None or not backend.is_model_instance(root):
        return None
    relation = relation_read_by(graphql_resolve_info(info))
    if relation is None:
        return None
    key = (type(root), relation)
    verdict = _RELATION_VERDICTS.get(key)
    if verdict is None:
        try:
            verdict = relation in backend.relation_names(type(root))
        except Exception:  # pragma: no cover - defensive; unmapped roots
            return None
        _RELATION_VERDICTS[key] = verdict
    return relation if verdict else None


def _scope_rows_apart(backend: Backend, result: Any, model: Any, info: Any) -> Any:
    """*result*, less the subclass rows the scope of *model* stood in for.

    A relation is read under the scope of the model it targets; a row of a
    subclass whose type has a scope of its own is checked against that one,
    and a row the relation's type reads as a base's against the base's.
    """
    apart = backend.rows_scoped_apart(
        result if isinstance(result, list) else [result], model, info
    )
    if not apart:
        return result
    scoped = backend.scope_instances(apart, info)
    if isawaitable(scoped):

        async def _await_scoped() -> Any:
            return _without_rejected(result, apart, await scoped, info)[0]

        return _await_scoped()
    return _without_rejected(result, apart, scoped, info)[0]


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
#
# A prepared row is not scope-checked again, so a row is marked only once its
# scope has run - a scoped query read it, or ``scope_instances`` kept it - and
# the mark holds only for the execution that made it, like the scope marks in
# ``lazy_resolution``. It names the classes whose scopes ran, as a query over a
# base model reads rows of a subclass whose own type's scope it never applied,
# and a field of a subclass's type keeping its rows in a table of its own never
# applies the scope of the base's.
_PREPARED = "_strawberry_orm_relations_loaded"

#: Exact types a resolved value can have that no branch below ``resolve``
#: touches: not a query object, not a model instance, not a list, and never
#: awaitable. A page of 500 rows resolves thousands of such leaves, so they
#: skip the machinery entirely. Exact-type matching keeps subclasses (enums,
#: bool-like wrappers) on the full path.
_SCALAR_LEAF_TYPES = frozenset({str, int, float, bool, type(None)})


def _mark_prepared(rows: Any, owner: Any = ANY_SCOPE) -> None:
    """Mark *rows* loaded, and passed by the ``scope_rows`` of *owner*.

    Left out, *owner* is the class scoping each row's own class.
    """
    made = row_mark(owner)
    if made is None:
        return
    for row in rows if isinstance(rows, list) else [rows]:
        # Immutable or slotted rows simply go unmarked and take the slow path.
        with suppress(Exception):
            setattr(row, _PREPARED, made)


def _is_prepared(row: Any, owner: Any = ANY_SCOPE) -> bool:
    """Whether *row* is prepared, and passed *owner*'s scope, in this execution.

    *owner* is the class scoping the row's own class, or the set of classes
    whose scopes the row is owed. A row a query over its base read is still
    prepared once ``scope_instances`` kept it by its own.
    """
    made = getattr(row, _PREPARED, None)
    return row_mark_holds(made, owner) or (
        row_mark_holds(made) and row_is_scoped(row, owner)
    )


def _query_scope(backend: Backend, query: Any, info: Any) -> Any:
    """The classes whose ``scope_rows`` the field *info* scopes *query* by.

    ``ANY_SCOPE`` when the backend cannot tell the model *query* reads.
    """
    model = backend.query_model(query)
    return ANY_SCOPE if model is None else backend.row_scopes(model, info)


def optimize_query_nodes(nodes: Any, info: Any) -> Any:
    """Apply optimizer hints to *nodes* when it is still a backend query object."""
    backend, store = get_configured_optimizer(info)
    if backend is None or store is None or not backend.is_query_object(nodes):
        return nodes
    owner = _query_scope(backend, nodes, info)
    rows = backend.apply_optimizer_hints(store, nodes, info)
    if isawaitable(rows):

        async def _await_rows() -> Any:
            resolved = await rows
            _mark_prepared(resolved, owner)
            return resolved

        return _await_rows()
    _mark_prepared(rows, owner)
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
        with execution_scope():
            yield

    def resolve(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if info.path.prev is None:
            open_subscription_event(info)
        result = _next(root, info, *args, **kwargs)
        if type(result) in _SCALAR_LEAF_TYPES:
            # Scalar leaves fall through every branch below unchanged.
            return result
        backend = self._backend
        if backend is not None:
            result = settled(result, _list_depth(info), backend)
        singular = False
        optimized = False
        owner = ANY_SCOPE
        if (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            singular = returns_single_orm_object(backend, info)
            owner = _query_scope(backend, result, info)
            result = backend.apply_optimizer_hints(self._store, result, info)
            optimized = True
            if not isawaitable(result):
                _mark_prepared(result, owner)
        elif not isawaitable(result):
            result = self._load_rows(result, root, info)

        if isawaitable(result):
            return self._resolve_async(result, root, info, singular, optimized, owner)

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
        if isinstance(result, Connection):
            return _paged_in_scope(result, info)

        # Reading a relation off a row that is already in memory: whatever
        # loaded the parent loaded this too, because the lookups are computed
        # for the whole subtree at once. Re-loading here would issue a query
        # per parent to fetch rows that are already present. Only a row of a
        # subclass with a scope of its own is owed a check.
        relation = _relation_of_the_parent(backend, root, info)
        if relation is not None:
            target = backend._relation_target_model(type(root), relation)
            return _scope_rows_apart(backend, result, target, info)

        def owed(row: Any) -> frozenset[type]:
            return backend.row_scopes(type(row), info, row)

        if backend.is_model_instance(result):
            prepared = _is_prepared(result, owed(result))
            rows: list[Any] = [] if prepared else [result]
            if not rows:
                return result
        elif not isinstance(result, list):
            return result
        else:
            rows = [
                row
                for row in rows_in(result)
                if backend.is_model_instance(row) and not _is_prepared(row, owed(row))
            ]
            if not rows:
                return result

        # Relation fields of the parent returned above: those caches were
        # scoped with the parent, and checking every row again would query
        # once per parent.
        return scope_then_load(backend, self._store, result, rows, info)

    async def _resolve_async(
        self,
        result: Any,
        root: Any,
        info: Any,
        singular: bool = False,
        optimized: bool = False,
        owner: Any = ANY_SCOPE,
    ) -> Any:
        result = await await_maybe(result)

        backend = self._backend
        if optimized:
            # The rows were only reachable once the awaitable resolved, so this
            # is the first chance to mark them.
            _mark_prepared(result, owner)
        elif (
            backend is not None
            and self._store is not None
            and backend.is_query_object(result)
        ):
            singular = returns_single_orm_object(backend, info)
            owner = _query_scope(backend, result, info)
            result = await await_maybe(
                backend.apply_optimizer_hints(self._store, result, info)
            )
            _mark_prepared(result, owner)
        else:
            result = await await_maybe(self._load_rows(result, root, info))

        stash_parents(getattr(self, "execution_context", None), info, result)
        return _unwrap_single(result, singular)


class RowScopeExtension(SchemaExtension):
    """Re-apply ``scope_rows`` when the optimizer is not doing it.

    The optimizer scopes a query before it runs and scopes a materialized list
    while loading relations. With the optimizer off, a resolver can return
    either and skip ``scope_rows``. This wrapper is the innermost extension in
    that configuration, so it sees the resolver's return value first.
    """

    _backend: Any = None

    @classmethod
    def configure(cls, backend: Any) -> type[RowScopeExtension]:
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend},
        )

    def on_execute(self) -> Any:
        with execution_scope():
            yield

    def resolve(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if info.path.prev is None:
            open_subscription_event(info)
        result = _next(root, info, *args, **kwargs)
        backend = self._backend
        if backend is None or type(result) in _SCALAR_LEAF_TYPES:
            return result
        result = settled(result, _list_depth(info), backend)
        if isawaitable(result) and not backend.is_query_object(result):
            return self._resolve_async(result, info)
        return self._scope(result, info)

    async def _resolve_async(self, result: Any, info: Any) -> Any:
        result = await await_maybe(result)
        scoped = self._scope(result, info)
        if isawaitable(scoped):
            return await scoped
        return scoped

    def _scope(self, result: Any, info: Any) -> Any:
        backend = self._backend
        if type(result) in _SCALAR_LEAF_TYPES:
            return result
        if isinstance(result, Connection):
            return _paged_in_scope(result, info)
        if backend.is_query_object(result):
            scoped = backend.apply_type_scope(result, backend.query_model(result), info)
            # Left to GraphQL, the query would run where a database error
            # reaches the client with its SQL.
            return backend.materialize_query(scoped, info)

        if backend.is_model_instance(result):
            rows: list[Any] | None = [result]
        elif not isinstance(result, list):
            return result
        else:
            rows = [row for row in rows_in(result) if backend.is_model_instance(row)]
        if not rows:
            return result

        scoped = backend.scope_instances(rows, info)
        if isawaitable(scoped):

            async def _await_scoped() -> Any:
                kept = await scoped
                filtered, _to_load = _without_rejected(result, rows, kept, info)
                return filtered

            return _await_scoped()
        filtered, _to_load = _without_rejected(result, rows, scoped, info)
        return filtered
