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

import re
from asyncio import ensure_future
from inspect import isawaitable
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from graphql import GraphQLList, GraphQLNonNull, get_named_type, is_abstract_type
from strawberry.extensions import SchemaExtension
from strawberry.relay import Edge
from strawberry.types.cast import get_strawberry_type_cast

from strawberry_orm._async import AwaitableOrValue, await_maybe
from strawberry_orm.grouping import GroupPath
from strawberry_orm.lazy_resolution import _path_field_names, execution_mark

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend

_PARENTS_KEY = "_orm_batch_parents"
_BAILS_KEY = "_orm_batch_bails"
_RESULTS_KEY = "_orm_batch_results"
_CONNECTION_KEY = "_orm_connection_pages"
#: Which execution the parents, rows and pages kept on the context were read in.
_EXECUTION_KEY = "_orm_batch_execution"
_MISSING = object()
_UNBATCHABLE = object()


class _Bail(Exception):
    """Signals that this field must fall back to per-parent resolution."""


def returns_orm_list(backend: Backend, info: Any) -> bool:
    """True when the field returns a list of a registered ORM type."""
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


def _execution_store(execution_context: Any, key: str) -> dict[Any, Any] | None:
    """A per-operation dict, emptied whenever another execution has opened.

    A subscription's events share one ``ExecutionContext``, and each event is
    an execution of its own. The parents, rows and pages one event read, under
    the scope it had, are dropped before the next event reads anything.
    """
    if execution_context is None:
        return None
    mark = execution_mark()
    if getattr(execution_context, _EXECUTION_KEY, None) is not mark:
        try:
            setattr(execution_context, _EXECUTION_KEY, mark)
        except Exception:  # pragma: no cover - exotic context objects
            return None
        for stale in (_PARENTS_KEY, _RESULTS_KEY, _CONNECTION_KEY):
            setattr(execution_context, stale, {})
    return _operation_store(execution_context, key)


def path_key(info: Any) -> str:
    """Dotted field path with list indices removed, shared by all siblings."""
    return ".".join(_path_field_names(info))


def _parent_type_name(info: Any) -> str | None:
    return getattr(getattr(info, "parent_type", None), "name", None)


def _cache_key(key: str, info: Any) -> tuple[Any, ...]:
    """Where a field's batched reads are kept: per path, and per parent type.

    The rows of a union or interface list share their fields' paths, but each
    of their types resolves a field its own way, with its own ``scope=``. One
    path can also be selected in several branches of the query, with other
    arguments, so the field's nodes in the query are part of the key too.
    """
    nodes = tuple(id(node) for node in getattr(info, "field_nodes", None) or ())
    return (key, _parent_type_name(info), nodes)


def _type_of(row: Any, abstract: Any, info: Any) -> str | None:
    """The object type *row* will complete as in *abstract*, if it can be told now.

    graphql-core decides that only as the row completes, after its siblings'
    fields are batched, so it is decided here the way Strawberry decides it:
    by the row's ``strawberry.cast``, or else by the first possible type whose
    ``is_type_of`` takes the row. Only the library's own ``is_type_of`` is
    asked ahead of time; a type that decides for itself is not told.
    """
    possible = info.schema.get_possible_types(abstract)
    cast = get_strawberry_type_cast(row)
    if cast is not None:
        return next(
            (
                type_.name
                for type_ in possible
                if type_.extensions["strawberry-definition"].origin is cast
            ),
            None,
        )
    for type_ in possible:
        is_type_of = type_.is_type_of
        if getattr(is_type_of, "orm_model", None) is None:
            return None
        try:
            if is_type_of(row, info):
                return type_.name
        except TypeError:  # two types read the row, and it was not cast
            return None
    return None


class _Parents:
    """The rows a list field produced, for the fields below them to batch across.

    The rows of a union or interface list can complete as several types, and
    each type resolves a field its own way: its own resolver, relation and
    ``scope=``. So a field is batched across the rows of its own type only,
    and a row whose type cannot be told ahead is batched with none.
    """

    __slots__ = ("_by_type", "info", "item_type", "rows")

    def __init__(self, rows: list[Any], item_type: Any, info: Any) -> None:
        self.rows = rows
        self.item_type = item_type
        self.info = info
        self._by_type: dict[str, list[Any]] | None = None

    def of_type(self, type_name: str | None) -> list[Any]:
        """The rows that complete as the object type *type_name*."""
        if self._by_type is None:
            rows = [row for row in self.rows if row is not None]
            if not is_abstract_type(self.item_type):
                self._by_type = {self.item_type.name: rows}
            else:
                self._by_type = {}
                for row in rows:
                    name = _type_of(row, self.item_type, self.info)
                    if name is not None:
                        self._by_type.setdefault(name, []).append(row)
        return self._by_type.get(type_name, []) if type_name else []


def stash_parents(execution_context: Any, info: Any, rows: Any) -> None:
    """Record the rows a field produced so its children can batch across them."""
    if not isinstance(rows, list) or len(rows) < 2:
        return
    store = _execution_store(execution_context, _PARENTS_KEY)
    if store is None:
        return
    key = path_key(info)
    item_type = get_named_type(info.return_type)
    store[key] = _Parents(rows, item_type, info)
    # A connection's edges each resolve ``node`` to one row, which is never a
    # list to record, so what is read under ``node`` is batched across these.
    if all(isinstance(row, Edge) for row in rows):
        node_type = get_named_type(item_type.fields["node"].type)
        store[f"{key}.node"] = _Parents([row.node for row in rows], node_type, info)


def _siblings(
    execution_context: Any, root: Any, key: str, info: Any
) -> list[Any] | None:
    """The parents of *root*'s type this field is being resolved for, if batchable.

    Not when *root* is not among them: its type could not be told ahead, or
    the parents recorded at this path are another branch's.
    """
    parent_path, _, _ = key.rpartition(".")
    if not parent_path:
        return None
    store = _execution_store(execution_context, _PARENTS_KEY)
    stashed = store.get(parent_path) if store else None
    if stashed is None:
        return None
    parents = stashed.of_type(_parent_type_name(info))
    if len(parents) < 2 or not any(parent is root for parent in parents):
        return None
    return parents


def _held_for(entries: dict[int, tuple[Any, Any]], parent: Any) -> Any:
    """What *entries* keep for *parent*, or ``None``.

    They are keyed by ``id()``, which a row allocated after another was freed
    can share. So each entry holds its parent too: kept alive, its address is
    not reused, and compared, the entry is never handed to another row.
    """
    entry = entries.get(id(parent))
    if entry is None or entry[0] is not parent:
        return None
    return entry[1]


def _hand_out(
    batched: dict[int, tuple[Any, Any]],
    attr_name: str,
    members: list[tuple[Any, Any, Any]],
    rows: list[Any],
) -> None:
    """Give each parent its rows from one batched read."""
    from strawberry_orm.lazy_resolution import mark_rows_scoped
    from strawberry_orm.optimizer.extension import _mark_prepared

    # The read carried the type's scope and the selection's loads, so the
    # optimizer must not check or load these again, a parent at a time.
    _mark_prepared(rows)
    mark_rows_scoped(rows)
    grouped: dict[Any, list[Any]] = {}
    for row in rows:
        grouped.setdefault(getattr(row, attr_name), []).append(row)
    for parent, parent_pk, _ in members:
        batched[id(parent)] = (parent, grouped.get(parent_pk, []))


def _own_rows(entry: Any, root: Any) -> Any:
    """A parent's rows, or a wait for them when the batched read is pending."""
    if isawaitable(entry):
        return _await_own_rows(entry, root)
    return entry


async def _await_own_rows(shared: Any, root: Any) -> list[Any]:
    return _held_for(await shared, root) or []


def _to_snake(name: str) -> str:
    """GraphQL camelCase to the model's attribute spelling."""
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def _already_loaded(root: Any, info: Any) -> bool:
    """True when this field's rows were loaded with the parent.

    A prefetched relation hands back the rows it already holds, which is not a
    query object and so looks to the rewrite like a resolver that ran its own.
    Nothing ran, and nothing is owed a report.
    """
    from strawberry_orm.lazy_resolution import (
        _django_relation_prefetched,
        _sqlalchemy_relation_prefetched,
        _tortoise_relation_prefetched,
    )

    field_name = getattr(info, "python_name", None)
    if field_name is None:
        # A raw resolve info knows only the GraphQL name, which is camelCase
        # while the model attribute is not.
        path = _path_field_names(info)
        field_name = _to_snake(path[-1]) if path else None
    if field_name is None:
        return False
    for probe in (
        _sqlalchemy_relation_prefetched,
        _django_relation_prefetched,
        _tortoise_relation_prefetched,
    ):
        try:
            if probe(root, field_name) is True:
                return True
        except Exception:  # pragma: no cover - probes are best effort
            continue
    return False


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

    #: Per-execution ``id(return_type) -> returns_orm_list`` verdicts. Set
    #: lazily on the instance (extensions are instantiated per operation), so
    #: entries never outlive the return-type objects they key on. The verdict
    #: is asked once per field per row; a page of rows repeats a handful of
    #: return types thousands of times.
    _orm_list_verdicts: dict[int, bool] | None = None

    def resolve(
        self, _next: Any, root: Any, info: Any, *args: Any, **kwargs: Any
    ) -> Any:
        backend = self._backend
        if backend is None or root is None:
            return _next(root, info, *args, **kwargs)

        verdicts = self._orm_list_verdicts
        if verdicts is None:
            verdicts = self._orm_list_verdicts = {}
        type_key = id(info.return_type)
        verdict = verdicts.get(type_key)
        if verdict is None:
            verdict = returns_orm_list(backend, info)
            verdicts[type_key] = verdict
        if not verdict:
            return _next(root, info, *args, **kwargs)

        key = path_key(info)
        execution_context = getattr(self, "execution_context", None)
        results = _execution_store(execution_context, _RESULTS_KEY)
        if results is None:
            return _next(root, info, *args, **kwargs)

        cache_key = _cache_key(key, info)
        cached = results.get(cache_key, _MISSING)
        if cached is _UNBATCHABLE:  # this path already proved unrewritable
            return _next(root, info, *args, **kwargs)

        # A path is resolved once per parent *group*, not once per operation:
        # ``users.posts.comments`` is reached separately for each user's posts.
        # A cache miss therefore means "not batched yet", never "no rows".
        if cached is not _MISSING:
            own = _held_for(cached, root)
            if own is not None:
                return _own_rows(own, root)

        parents = _siblings(execution_context, root, key, info)
        if parents is None:
            return _next(root, info, *args, **kwargs)

        # Resolve this parent first. If the resolver executed its own query
        # there is nothing to rewrite, and running ahead for every sibling
        # would have thrown away one query per parent to find that out.
        first = _next(root, info, *args, **kwargs)
        if isawaitable(first) or not backend.is_query_object(first):
            results[cache_key] = _UNBATCHABLE
            if not _already_loaded(root, info):
                record_bail(
                    execution_context,
                    key,
                    "the resolver answers asynchronously, so there is no query "
                    "to rewrite before it runs"
                    if isawaitable(first)
                    else "the resolver ran its own query, leaving nothing to rewrite",
                )
            return first

        try:
            batched = self._run_ahead(_next, parents, root, first, info, args, kwargs)
        except _Bail:
            results[cache_key] = _UNBATCHABLE
            record_bail(
                execution_context,
                key,
                "the query could not be rewritten to cover every parent at once",
            )
            return first

        if isawaitable(batched):
            # Siblings resolve before the read finishes, so each is handed the
            # read itself: a task, which every one of them can await.
            shared = ensure_future(batched)
            batched = {id(parent): (parent, shared) for parent in parents}

        # Merge rather than replace: earlier parent groups at this path keep
        # their rows.
        merged = {} if cached is _MISSING else dict(cached)
        merged.update(batched)
        results[cache_key] = merged
        return _own_rows(_held_for(merged, root), root)

    def _run_ahead(
        self,
        _next: Any,
        parents: list[Any],
        root: Any,
        first: Any,
        info: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> dict[int, tuple[Any, Any]]:
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

        reads: list[tuple[str, list[tuple[Any, Any, Any]], Any]] = []
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
            reads.append((attr_name, members, rows))

        if any(isawaitable(rows) for _, _, rows in reads):
            return self._hand_out_async(reads)
        batched: dict[int, tuple[Any, Any]] = {}
        for attr_name, members, rows in reads:
            _hand_out(batched, attr_name, members, rows)
        return batched

    @staticmethod
    async def _hand_out_async(
        reads: list[tuple[str, list[tuple[Any, Any, Any]], Any]],
    ) -> dict[int, tuple[Any, Any]]:
        batched: dict[int, tuple[Any, Any]] = {}
        for attr_name, members, rows in reads:
            _hand_out(batched, attr_name, members, await await_maybe(rows))
        return batched


def page_attr(field_name: str) -> str:
    """Where a windowed page is left for the field's own resolver to pick up."""
    return f"_orm_connection_page_{field_name}"


def _occurrence(info: Any) -> tuple[Any, ...]:
    """Where in the response this field is being read, list indices included."""
    return tuple(info.path.as_list())


def hand_over_page(root: Any, field_name: str, info: Any, page: Any) -> None:
    """Leave *page* for the resolver of this one occurrence of the field.

    The row is shared: an ORM hands back the same instance wherever it is
    reached, so a page keyed only by the field would be read by an alias with
    other arguments, or by a later request with another caller's scope.
    """
    pages = getattr(root, page_attr(field_name), None)
    if pages is None:
        pages = {}
        setattr(root, page_attr(field_name), pages)
    pages[_occurrence(info)] = page


def take_page(root: Any, field_name: str, info: Any) -> Any:
    """The page handed over for this occurrence, removed from the row."""
    attr = page_attr(field_name)
    pages = getattr(root, attr, None)
    if not pages:
        return None
    page = pages.pop(_occurrence(info), None)
    if not pages:
        delattr(root, attr)
    return page


def _strawberry_field(info: Any) -> Any:
    """The Strawberry field behind a raw graphql-core ``info``."""
    fields = getattr(getattr(info, "parent_type", None), "fields", None) or {}
    gql_field = fields.get(getattr(info, "field_name", None))
    if gql_field is None:
        return None
    return gql_field.extensions.get("strawberry-definition")


def _page_window(
    backend: Backend, execution_context: Any, info: Any, kwargs: dict[str, Any]
) -> Any:
    """The rows every parent's page needs, read off the field's own arguments.

    Strawberry's own arithmetic, with the field's ``max_results``, so a page
    size over the limit or a malformed cursor is refused here, before any row
    is read; the field then runs plain and reports it as usual. The page is
    the one the connection returns: held to ``max_results`` and
    ``default_query_limit``, and one page from the start when no size is given.
    """
    from strawberry.relay.fields import ConnectionExtension
    from strawberry.relay.utils import SliceMetadata

    first, last = kwargs.get("first"), kwargs.get("last")
    if first is None and last is not None:
        raise _Bail("last without first starts at each parent's own row count")
    field = _strawberry_field(info)
    extension = next(
        (
            e
            for e in getattr(field, "extensions", ())
            if isinstance(e, ConnectionExtension)
        ),
        None,
    )
    if extension is None:
        raise _Bail("not a connection field")
    max_results = extension.max_results
    if max_results is None:
        max_results = execution_context.schema.config.relay_max_results
    limit = getattr(backend, "_default_query_limit", None)
    size = max_results if limit is None else min(max_results, limit)
    cursors = {
        "before": kwargs.get("before"),
        "after": kwargs.get("after"),
        "max_results": max_results,
        "prefix": extension.connection_type._edge_type().CURSOR_PREFIX,
    }
    try:
        SliceMetadata.from_arguments(info, first=first, last=last, **cursors)
        window = SliceMetadata.from_arguments(
            info,
            first=size if first is None else min(first, size),
            last=None if last is None else min(last, size),
            **cursors,
        )
    except (TypeError, ValueError) as exc:
        raise _Bail(str(exc)) from exc
    if window.start < 0 or window.end < 0:
        raise _Bail("a cursor before the first row")
    return window


def _asks_of_the_whole_relation(info: Any) -> bool:
    """Whether the field selects what a page of rows cannot answer.

    Aggregates and groups are taken over each parent's whole relation, from
    the query the field's resolver builds; a windowed page hands it rows.
    """
    from strawberry.types.nodes import convert_selections

    from strawberry_orm.backends._base import _selection_requests

    selected = SimpleNamespace(
        selected_fields=convert_selections(info, info.field_nodes)
    )
    return any(
        _selection_requests(selected, *path)
        for path in (("aggregates",), ("groups",), ("pageInfo", "aggregates"))
    )


def _connection_inputs(
    execution_context: Any, info: Any, kwargs: dict[str, Any]
) -> tuple[Any, Any]:
    """The field's own ``filter`` and ``order``, as the backends expect them.

    A schema extension runs before Strawberry converts arguments, so these
    arrive as plain dictionaries while the backends build their clauses out of
    input instances. Converting here is what keeps a windowed page honest: the
    page is cut before the field's own resolver is reached, so a filter applied
    any later would be applied to rows that were already chosen without it.
    """
    schema = getattr(execution_context, "schema", None)
    field = _strawberry_field(info)
    if schema is None or field is None:
        return None, None

    from strawberry.types.arguments import convert_arguments

    converted = convert_arguments(
        kwargs,
        field.arguments,
        schema.schema_converter.scalar_registry,
        schema.config,
    )
    return converted.get("filter"), converted.get("order")


class RelationConnectionExtension(SchemaExtension):
    """Resolve every parent's page of a relation connection in one query.

    A connection cannot go through :class:`BatchingExtension`: that rewrites
    ``fk = <pk>`` into ``fk IN (...)``, which cannot express "the first ten for
    each parent" - ask for it and you get the first ten overall. So the page is
    cut with a window function instead, numbering rows within each parent and
    keeping the low numbers, which needs one query for every parent's page and
    one more for their totals.

    Both reads are shared across the sibling parents. On an async backend they
    resolve concurrently, so what is shared is the in-flight read rather than
    its result; see :meth:`resolve`.
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
        pages = _execution_store(execution_context, _CONNECTION_KEY)
        if pages is None:
            return _next(root, info, *args, **kwargs)

        key = path_key(info)
        cache_key = _cache_key(key, info)
        cached = pages.get(cache_key)
        if cached is _UNBATCHABLE:
            return _next(root, info, *args, **kwargs)

        if cached is None:
            parents = _siblings(execution_context, root, key, info)
            if parents is None:
                return _next(root, info, *args, **kwargs)
            try:
                cached = self._fetch_pages(backend, spec, parents, info, kwargs=kwargs)
            except _Bail as exc:
                pages[cache_key] = _UNBATCHABLE
                record_bail(execution_context, key, str(exc) or "not windowable")
                return _next(root, info, *args, **kwargs)
            if isawaitable(cached):
                # Siblings reach this concurrently, before any result exists to
                # cache, so what gets cached is the read itself. It has to be a
                # task rather than the bare coroutine: a coroutine is awaitable
                # once, which would leave every other sibling to start a read of
                # its own - the N+1 this exists to collapse.
                cached = ensure_future(cached)
            pages[cache_key] = cached

        if isawaitable(cached):
            return self._resolve_async(
                cached,
                _next,
                root,
                info,
                spec,
                execution_context,
                cache_key,
                args,
                kwargs,
            )
        return self._with_page(cached, _next, root, info, spec, args, kwargs)

    def _with_page(
        self,
        pages_by_parent: dict[int, tuple[Any, Any]],
        _next: Any,
        root: Any,
        info: Any,
        spec: Any,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        """Hand this parent its page, then let the field build the connection.

        Relay builds the connection inside ``_next``, so the page is handed to
        the resolver rather than returned here - returning rows would replace
        the connection itself.
        """
        hand_over_page(root, spec.field_name, info, _held_for(pages_by_parent, root))
        result = None
        try:
            result = _next(root, info, *args, **kwargs)
        finally:
            # Whether or not the resolver took it, nothing outlives the field.
            if not isawaitable(result):
                take_page(root, spec.field_name, info)
        if isawaitable(result):
            return self._take_page_after(result, root, spec.field_name, info)
        return result

    @staticmethod
    async def _take_page_after(
        result: Any, root: Any, field_name: str, info: Any
    ) -> Any:
        try:
            return await result
        finally:
            take_page(root, field_name, info)

    async def _resolve_async(
        self,
        pending: Any,
        _next: Any,
        root: Any,
        info: Any,
        spec: Any,
        execution_context: Any,
        cache_key: tuple[Any, ...],
        args: tuple,
        kwargs: dict,
    ) -> Any:
        """Await the shared read, then resolve this parent from it.

        Nothing bails here: every condition that falls back to per-parent reads
        is decided before the query is issued, so a failure this late is a bug
        in the backend rather than a shape it cannot window.
        """
        from strawberry_orm._async import await_maybe

        fetched = await pending
        pages = _execution_store(execution_context, _CONNECTION_KEY)
        if pages is not None:
            pages[cache_key] = fetched
        return await await_maybe(
            self._with_page(fetched, _next, root, info, spec, args, kwargs)
        )

    def _fetch_pages(
        self,
        backend: Backend,
        spec: Any,
        parents: list[Any],
        info: Any,
        kwargs: dict[str, Any] | None = None,
    ) -> AwaitableOrValue[dict[int, tuple[Any, Any]]]:
        """One windowed query for the pages, one grouped query for the totals."""
        from strawberry_orm.relay.connection import _LAST_POSITION

        keys = [spec.key_of(backend, parent) for parent in parents]
        if any(key is None for key in keys):
            raise _Bail("a parent has no key to group its rows by")

        execution_context = getattr(self, "execution_context", None)
        window = _page_window(backend, execution_context, info, kwargs or {})
        if _asks_of_the_whole_relation(info):
            raise _Bail("aggregates and groups are of the whole relation")
        # Relay slices each page itself, so it is handed the page at its
        # positions plus the row that says whether another page exists. A
        # cursor can name any position; none past the last one is sent.
        start = min(window.start, _LAST_POSITION)
        limit = min(window.overfetch, _LAST_POSITION) - start

        filter_input, order_input = _connection_inputs(
            execution_context, info, kwargs or {}
        )
        base = backend.relation_base_query(spec, keys, info)
        if filter_input is not None:
            base = backend.apply_filters(
                base, filter_input, spec.related_model, info=info
            )
        parent_key = GroupPath(alias="_g0", name=spec.key_field, column=spec.key_field)
        rows_by_key = backend.batch_group_items(
            base,
            [parent_key],
            info,
            spec.related_model,
            per_group_limit=max(limit, 0),
            per_group_offset=start,
            order_input=order_input,
        )
        if isawaitable(rows_by_key):
            return self._fetch_pages_async(
                backend, spec, parents, keys, info, base, rows_by_key, start
            )
        totals = backend.group_counts(base, spec.key_field, info)
        return self._assemble(parents, keys, rows_by_key, totals, start)

    async def _fetch_pages_async(
        self,
        backend: Backend,
        spec: Any,
        parents: list[Any],
        keys: list[Any],
        info: Any,
        base: Any,
        rows_by_key: Any,
        start: int,
    ) -> dict[int, tuple[Any, Any]]:
        """The same two reads, for a backend that only answers asynchronously."""
        from strawberry_orm._async import await_maybe

        rows = await rows_by_key
        totals = await await_maybe(backend.group_counts(base, spec.key_field, info))
        return self._assemble(parents, keys, rows, totals, start)

    @staticmethod
    def _assemble(
        parents: list[Any],
        keys: list[Any],
        rows_by_key: dict[tuple, list[Any]],
        totals: dict[Any, int],
        start: int,
    ) -> dict[int, tuple[Any, Any]]:
        """Pair every parent with its own page, from *start*, and its real total."""
        from strawberry_orm.lazy_resolution import mark_rows_scoped
        from strawberry_orm.relay.connection import PreslicedRows

        # Every backend stringifies its group keys, so the parent key has to be
        # compared in the same form. The window already applied scope_rows, so
        # the page must not be checked again one row at a time.
        pages: dict[int, tuple[Any, Any]] = {}
        for parent, key in zip(parents, keys, strict=True):
            page = rows_by_key.get((str(key),), [])
            mark_rows_scoped(page)
            pages[id(parent)] = (parent, PreslicedRows(page, totals.get(key, 0), start))
        return pages


def extensions_include_relation_connections(extensions: list[Any]) -> bool:
    for ext in extensions:
        if isinstance(ext, type) and issubclass(ext, RelationConnectionExtension):
            return True
        if getattr(ext, "__name__", "").startswith("RelationConnectionExtension_"):
            return True
    return False
