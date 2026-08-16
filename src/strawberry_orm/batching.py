"""Relation batching: collapse per-parent resolvers into one query per shape.

GraphQL resolves a relation field once per parent row, so a resolver that
returns ``Post.objects.filter(author=self, ...)`` issues one statement per
parent. The parents are already in memory and building a queryset touches no
database, so the resolver can be run for *every* sibling parent up front, the
parent predicate reflected out of each resulting query, and the remainders
grouped by shape. Each distinct shape then costs one ``IN`` query.

Branching resolvers fall out for free: two branches produce two shapes, so a
page of 500 parents collapses to two statements rather than 500.

Anything that cannot be proven equivalent to per-row resolution bails to the
original path; see :class:`_Bail` and the backend ``split_parent_predicate``
implementations for the conditions.
"""

from __future__ import annotations

from inspect import isawaitable
from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

from strawberry_orm.lazy_resolution import _path_field_names

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend

_PARENTS_KEY = "_orm_batch_parents"
_BAILS_KEY = "_orm_batch_bails"
_RESULTS_KEY = "_orm_batch_results"
_MISSING = object()
_UNBATCHABLE = object()


class _Bail(Exception):
    """Signals that this field must fall back to per-parent resolution."""


def returns_orm_list(backend: Backend, info: Any) -> bool:
    """True when the field returns a list of a registered ORM type."""
    from graphql import GraphQLList, GraphQLNonNull

    return_type = getattr(info, "return_type", None)
    while isinstance(return_type, GraphQLNonNull):
        return_type = return_type.of_type
    if not isinstance(return_type, GraphQLList):
        return False

    inner = return_type.of_type
    while isinstance(inner, GraphQLNonNull):
        inner = inner.of_type
    name = getattr(inner, "name", None)
    registry = getattr(backend, "_type_registry", {})
    return name is not None and name in registry


def _operation_store(execution_context: Any, key: str) -> dict[str, Any] | None:
    """Return a per-operation dict kept on Strawberry's execution context.

    Every extension in one operation receives the same ``ExecutionContext``
    object, which is what lets the optimizer hand parent rows to the batcher.
    The GraphQL context is not usable for this: it defaults to ``None``.
    """
    if execution_context is None:
        return None
    store = getattr(execution_context, key, None)
    if store is None:
        store = {}
        try:
            setattr(execution_context, key, store)
        except Exception:  # pragma: no cover - exotic context objects
            return None
    return store


def path_key(info: Any) -> str:
    """Dotted field path with list indices removed, shared by all siblings."""
    return ".".join(_path_field_names(info))


def stash_parents(execution_context: Any, info: Any, rows: Any) -> None:
    """Record the rows a field produced so its children can batch across them."""
    if not isinstance(rows, list) or len(rows) < 2:
        return
    store = _operation_store(execution_context, _PARENTS_KEY)
    if store is None:
        return
    store[path_key(info)] = rows


def record_bail(execution_context: Any, path: str, reason: str) -> None:
    """Note that *path* fell back to one query per parent, and why.

    The batcher knows exactly when its rewrite does not hold, and that is the
    one thing a field's declaration can never tell you: whether the collapse
    actually happened. The lazy-resolution diagnostic reports these alongside
    the loads it finds itself.
    """
    store = _operation_store(execution_context, _BAILS_KEY)
    if store is None:  # pragma: no cover - only without an execution context
        return
    store.setdefault(path, reason)


def recorded_bails(execution_context: Any) -> dict[str, str]:
    """Paths that fell back during this operation, keyed by path."""
    return _operation_store(execution_context, _BAILS_KEY) or {}


def extensions_include_batching(extensions: list[Any]) -> bool:
    for ext in extensions:
        if isinstance(ext, type) and issubclass(ext, BatchingExtension):
            return True
        if getattr(ext, "__name__", "").startswith("BatchingExtension_"):
            return True
    return False


class BatchingExtension(SchemaExtension):
    """Runs relation resolvers ahead of time and groups them into one query."""

    _backend: Backend | None = None
    _store: Any = None

    @classmethod
    def configure(cls, backend: Backend, store: Any) -> type[BatchingExtension]:
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend, "_store": store},
        )

    def resolve(
        self, _next: Any, root: Any, info: Any, *args: Any, **kwargs: Any
    ) -> Any:
        backend = self._backend
        if backend is None or root is None:
            return _next(root, info, *args, **kwargs)

        if not returns_orm_list(backend, info):
            return _next(root, info, *args, **kwargs)

        key = path_key(info)
        execution_context = getattr(self, "execution_context", None)
        results = _operation_store(execution_context, _RESULTS_KEY)
        if results is None:
            return _next(root, info, *args, **kwargs)

        cached = results.get(key, _MISSING)
        if cached is _UNBATCHABLE:  # this path already proved unrewritable
            return _next(root, info, *args, **kwargs)

        # A path is resolved once per parent *group*, not once per operation:
        # ``users.posts.comments`` is reached separately for each user's posts.
        # A cache miss therefore means "not batched yet", never "no rows".
        if cached is not _MISSING and id(root) in cached:
            return cached[id(root)]

        parents = self._siblings(execution_context, root, key)
        if parents is None:
            return _next(root, info, *args, **kwargs)

        # Resolve this parent first. If the resolver executed its own query
        # there is nothing to rewrite, and running ahead for every sibling
        # would have thrown away one query per parent to find that out.
        first = _next(root, info, *args, **kwargs)
        if isawaitable(first) or not backend.is_query_object(first):
            results[key] = _UNBATCHABLE
            record_bail(
                execution_context,
                key,
                "the resolver answers asynchronously, so there is no query to "
                "rewrite before it runs"
                if isawaitable(first)
                else "the resolver ran its own query, leaving nothing to rewrite",
            )
            return first

        try:
            batched = self._run_ahead(_next, parents, root, first, info, args, kwargs)
        except _Bail:
            results[key] = _UNBATCHABLE
            record_bail(
                execution_context,
                key,
                "the query could not be rewritten to cover every parent at once",
            )
            return first

        # Merge rather than replace: earlier parent groups at this path keep
        # their rows.
        merged = {} if cached is _MISSING else dict(cached)
        merged.update(batched)
        results[key] = merged
        return merged.get(id(root), [])

    def _siblings(
        self, execution_context: Any, root: Any, key: str
    ) -> list[Any] | None:
        """The parent rows this field is being resolved for, if batchable."""
        parent_path, _, _ = key.rpartition(".")
        if not parent_path:
            return None
        store = _operation_store(execution_context, _PARENTS_KEY)
        if not store:
            return None
        parents = store.get(parent_path)
        if not parents or len(parents) < 2:
            return None
        if not any(parent is root for parent in parents):
            return None
        return parents

    def _run_ahead(
        self,
        _next: Any,
        parents: list[Any],
        root: Any,
        first: Any,
        info: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[int, list[Any]]:
        """Build every sibling's query, group by shape, run one query per group."""
        backend = self._backend
        assert backend is not None

        groups: dict[str, list[tuple[Any, Any, Any]]] = {}
        key_names: dict[str, Any] = {}

        for parent in parents:
            if parent is root:
                query = first
            else:
                # A sibling is being resolved speculatively. If it raises, that
                # error belongs to *its* node, not this one, so abandon the
                # batch and let every parent resolve (and fail) on its own.
                try:
                    query = _next(parent, info, *args, **kwargs)
                except Exception as exc:
                    raise _Bail from exc
                if isawaitable(query) or not backend.is_query_object(query):
                    raise _Bail
            parent_pk = backend.instance_pk(parent)
            if parent_pk is None:
                raise _Bail
            split = backend.split_parent_predicate(query, parent_pk)
            if split is None:
                raise _Bail
            attr_name, key_handle, remainder = split
            signature = backend.query_signature(remainder)
            if signature is None:
                raise _Bail
            signature = f"{attr_name}|{signature}"
            groups.setdefault(signature, []).append((parent, parent_pk, remainder))
            key_names[signature] = (attr_name, key_handle)

        batched: dict[int, list[Any]] = {}
        for signature, members in groups.items():
            attr_name, key_handle = key_names[signature]
            _, _, template = members[0]
            keys = [pk for _, pk, _ in members]
            query = backend.apply_key_filter(template, attr_name, key_handle, keys)

            # Run through the optimizer rather than executing directly: this is
            # what applies the child type's scope_rows and eager-loads the
            # nested selection, so a batched read can never see more rows than
            # the per-parent read it replaces.
            rows = backend.apply_optimizer_hints(self._store, query, info)
            if isawaitable(rows):  # pragma: no cover - sync backends only
                raise _Bail

            grouped: dict[Any, list[Any]] = {}
            for row in rows:
                grouped.setdefault(getattr(row, attr_name), []).append(row)
            for parent, parent_pk, _ in members:
                batched[id(parent)] = grouped.get(parent_pk, [])
        return batched


_CONNECTION_KEY = "_orm_connection_pages"


def _connection_arg(info: Any, name: str) -> Any:
    """Read a literal argument off the connection field's own AST node."""
    from graphql.language import IntValueNode, StringValueNode

    from strawberry_orm.optimizer.selections import field_nodes_from_info

    for node in field_nodes_from_info(info):
        for arg in getattr(node, "arguments", ()) or ():
            if arg.name.value != name:
                continue
            value = arg.value
            if isinstance(value, IntValueNode):
                return int(value.value)
            if isinstance(value, StringValueNode):
                return value.value
    return None


def page_attr(field_name: str) -> str:
    """Where a windowed page is left for the field's own resolver to pick up."""
    return f"_orm_connection_page_{field_name}"


class RelationConnectionExtension(SchemaExtension):
    """Resolve every parent's page of a relation connection in one query.

    A connection cannot go through :class:`BatchingExtension`: that rewrites
    ``fk = <pk>`` into ``fk IN (...)``, which cannot express "the first ten for
    each parent" - ask for it and you get the first ten overall. So the page is
    cut with a window function instead, numbering rows within each parent and
    keeping the low numbers, which needs one query for every parent's page and
    one more for their totals.
    """

    _backend: Backend | None = None
    _store: Any = None

    @classmethod
    def configure(
        cls, backend: Backend, store: Any
    ) -> type[RelationConnectionExtension]:
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend, "_store": store},
        )

    def resolve(
        self, _next: Any, root: Any, info: Any, *args: Any, **kwargs: Any
    ) -> Any:
        backend = self._backend
        if backend is None or root is None:
            return _next(root, info, *args, **kwargs)

        spec = backend.relation_connection_spec(info)
        if spec is None:
            return _next(root, info, *args, **kwargs)

        execution_context = getattr(self, "execution_context", None)
        pages = _operation_store(execution_context, _CONNECTION_KEY)
        if pages is None:
            return _next(root, info, *args, **kwargs)

        key = path_key(info)
        cached = pages.get(key)
        if cached is _UNBATCHABLE:
            return _next(root, info, *args, **kwargs)

        if cached is None:
            parents = self._siblings(execution_context, root, key)
            if parents is None:
                return _next(root, info, *args, **kwargs)
            try:
                cached = self._fetch_pages(backend, spec, parents, info)
            except _Bail as exc:
                pages[key] = _UNBATCHABLE
                record_bail(execution_context, key, str(exc) or "not windowable")
                return _next(root, info, *args, **kwargs)
            pages[key] = cached

        # Relay builds the connection inside ``_next``, so the page is handed
        # to the resolver rather than returned here - returning rows would
        # replace the connection itself.
        page = cached.get(id(root))
        if page is not None:
            setattr(root, page_attr(spec.field_name), page)
        return _next(root, info, *args, **kwargs)

    def _siblings(
        self, execution_context: Any, root: Any, key: str
    ) -> list[Any] | None:
        parent_path, _, _ = key.rpartition(".")
        if not parent_path:
            return None
        store = _operation_store(execution_context, _PARENTS_KEY)
        if not store:
            return None
        parents = store.get(parent_path)
        if not parents or len(parents) < 2:
            return None
        if not any(parent is root for parent in parents):
            return None
        return parents

    def _fetch_pages(
        self, backend: Backend, spec: Any, parents: list[Any], info: Any
    ) -> dict[int, Any]:
        """One windowed query for the pages, one grouped query for the totals."""
        from strawberry_orm.relay.connection import (
            PreslicedRows,
            _decode_cursor_offset,
        )

        pks = [backend.instance_pk(parent) for parent in parents]
        if any(pk is None for pk in pks):
            raise _Bail("a parent has no key to group its rows by")

        after = _connection_arg(info, "after")
        offset = _decode_cursor_offset(after) if after else 0
        first = _connection_arg(info, "first")
        if first is None:
            # No page size given, so there is nothing to window against and a
            # plain batched read is already the whole answer.
            raise _Bail("no page size, so there is nothing to window")

        base = backend.relation_base_query(spec, pks, info)
        rows_by_key = backend.batch_group_items(
            base,
            [spec.key_field],
            info,
            spec.related_model,
            # Relay slices this itself, so hand it the page plus the row that
            # tells it whether another page exists.
            per_group_limit=first + offset + 1,
        )
        if isawaitable(rows_by_key):
            raise _Bail("this backend windows asynchronously")
        totals = backend.group_counts(base, spec.key_field, info)

        # Both backends stringify their group keys, so the parent key has to be
        # compared in the same form.
        pages: dict[int, Any] = {}
        for parent, pk in zip(parents, pks, strict=True):
            pages[id(parent)] = PreslicedRows(
                rows_by_key.get((str(pk),), []), totals.get(pk, 0)
            )
        return pages


def extensions_include_relation_connections(extensions: list[Any]) -> bool:
    for ext in extensions:
        if isinstance(ext, type) and issubclass(ext, RelationConnectionExtension):
            return True
        if getattr(ext, "__name__", "").startswith("RelationConnectionExtension_"):
            return True
    return False
