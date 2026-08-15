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
            return first

        try:
            batched = self._run_ahead(_next, parents, root, first, info, args, kwargs)
        except _Bail:
            results[key] = _UNBATCHABLE
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
