"""Runtime guardrails for unoptimized (lazy) ORM relation loads."""

from __future__ import annotations

import logging
import re
import warnings
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend

logger = logging.getLogger("strawberry_orm.lazy_query")


def extensions_include_lazy_resolution(extensions: list[Any]) -> bool:
    """Return True if *extensions* already contains a lazy-resolution extension."""
    for ext in extensions:
        if isinstance(ext, type) and issubclass(ext, LazyResolutionExtension):
            return True
        if getattr(ext, "__name__", "").startswith("LazyResolutionExtension_"):
            return True
    return False


def _field_name_from_info(info: Any) -> str | None:
    field_name = getattr(info, "python_name", None) or getattr(info, "field_name", None)
    if not field_name:
        return None
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", field_name).lower()


def _graphql_field_name(info: Any) -> str:
    return str(
        getattr(info, "field_name", None) or getattr(info, "python_name", None) or "?"
    )


def _path_field_names(info: Any) -> list[str]:
    path = getattr(info, "path", None)
    if path is None:
        field = _graphql_field_name(info)
        return [field] if field != "?" else []

    keys: list[str] = []
    node = path
    while node is not None:
        key = node.key
        if not isinstance(key, int):
            keys.append(str(key))
        node = node.prev
    keys.reverse()
    return keys


def _graphql_path_from_info(info: Any) -> str:
    keys = _path_field_names(info)
    return ".".join(keys) if keys else _graphql_field_name(info)


def _format_selection_set(selection_set: Any) -> str:
    if selection_set is None:
        return ""

    parts: list[str] = []
    for selection in selection_set.selections:
        name_node = getattr(selection, "name", None)
        if name_node is None:
            continue
        name = name_node.value
        nested = getattr(selection, "selection_set", None)
        if nested is not None and nested.selections:
            parts.append(f"{name} {{ {_format_selection_set(nested)} }}")
        else:
            parts.append(name)
    return " ".join(parts)


def _query_selection_path(info: Any) -> str:
    """Return the GraphQL selection path that requested this relation."""
    operation = getattr(info, "operation", None)
    path_fields = _path_field_names(info)
    if operation is None or not path_fields:
        return f"{{ {_graphql_path_from_info(info)} }}"

    def walk(selection_set: Any, remaining: list[str]) -> str | None:
        target = remaining[0]
        for selection in selection_set.selections:
            name_node = getattr(selection, "name", None)
            if name_node is None or name_node.value != target:
                continue

            nested = getattr(selection, "selection_set", None)
            if len(remaining) == 1:
                child = _format_selection_set(nested) if nested is not None else ""
                return f"{target} {{ {child} }}" if child else target

            if nested is None:
                return None
            inner = walk(nested, remaining[1:])
            if inner is None:
                return None
            return f"{target} {{ {inner} }}"

        return None

    inner = walk(operation.selection_set, path_fields)
    if inner is None:
        return f"{{ {_graphql_path_from_info(info)} }}"

    op_type = getattr(getattr(operation, "operation", None), "value", None)
    op_name = getattr(getattr(operation, "name", None), "value", None)
    if op_name:
        prefix = f"{op_type} {op_name}" if op_type else op_name
        return f"{prefix} {{ {inner} }}"
    if op_type:
        return f"{op_type} {{ {inner} }}"
    return f"{{ {inner} }}"


def _parent_graphql_type(info: Any) -> str:
    parent = getattr(info, "parent_type", None)
    if parent is None:
        return "?"
    return str(getattr(parent, "name", parent))


def _django_relation_hint(instance: Any, field_name: str, model_name: str) -> str:
    try:
        field = instance._meta.get_field(field_name)  # type: ignore[attr-defined]
    except Exception:
        return (
            f"Return a queryset from the parent resolver via orm.field() and use "
            f"orm.schema() so the optimizer eager-loads {model_name}.{field_name}."
        )

    field_class = type(field).__name__
    if field_class in ("ForeignKey", "OneToOneField"):
        return (
            f"Return a queryset (not list(...)) from the parent field via orm.field(); "
            f"orm.schema() will apply select_related('{field_name}') on {model_name}. "
            f"Manual fix: {model_name}.objects...select_related('{field_name}')."
        )
    if field_class in (
        "ManyToManyField",
        "ManyToOneRel",
        "ManyToManyRel",
        "OneToOneRel",
    ):
        return (
            f"Return a queryset from the parent field via orm.field(); "
            f"orm.schema() will apply prefetch_related('{field_name}') on {model_name}. "
            f"Manual fix: {model_name}.objects...prefetch_related('{field_name}')."
        )
    return (
        f"Return a queryset from the parent resolver via orm.field() and use "
        f"orm.schema() so the optimizer eager-loads {model_name}.{field_name}."
    )


def _sqlalchemy_relation_hint(instance: Any, field_name: str, model_name: str) -> str:
    from sqlalchemy import inspect as sa_inspect

    try:
        rel = sa_inspect(instance).mapper.relationships[field_name]
    except Exception:
        return (
            f"Return a Select from the parent resolver via orm.field() and use "
            f"orm.schema() so the optimizer eager-loads {model_name}.{field_name}."
        )

    loader = "selectinload" if rel.uselist else "joinedload"
    return (
        f"Return a Select (not materialized rows) from the parent field via orm.field(); "
        f"orm.schema() will apply {loader}({model_name}.{field_name}). "
        f"Manual fix: select({model_name}).options({loader}({model_name}.{field_name}))."
    )


def _tortoise_relation_hint(instance: Any, field_name: str, model_name: str) -> str:
    return (
        f"Return a Tortoise queryset from the parent field via orm.field(); "
        f"orm.schema() will apply prefetch_related('{field_name}') on {model_name}. "
        f"Manual fix: {model_name}.all().prefetch_related('{field_name}')."
    )


def _relation_hint(backend: Backend, instance: Any, field_name: str) -> str:
    model_name = type(instance).__name__
    backend_name = backend.__class__.__name__
    if backend_name == "DjangoBackend":
        return _django_relation_hint(instance, field_name, model_name)
    if backend_name == "SQLAlchemyBackend":
        return _sqlalchemy_relation_hint(instance, field_name, model_name)
    if backend_name == "TortoiseBackend":
        return _tortoise_relation_hint(instance, field_name, model_name)
    return (
        f"Return an unfetched queryset from the parent resolver via orm.field() and "
        f"use orm.schema() so the optimizer eager-loads {model_name}.{field_name}."
    )


@dataclass(frozen=True)
class _UnoptimizedLoad:
    orm_field: str
    model_name: str
    query_path: str
    resolver_type: str
    graphql_field: str
    hint: str


def _django_relation_prefetched(instance: Any, field_name: str) -> bool | None:
    try:
        field = instance._meta.get_field(field_name)  # type: ignore[attr-defined]
    except Exception:
        return None

    if not getattr(field, "is_relation", False):
        return None

    if getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False):
        is_cached = getattr(field, "is_cached", None)
        if callable(is_cached):
            return is_cached(instance)
        return True

    prefetched = getattr(instance, "_prefetched_objects_cache", None)
    return isinstance(prefetched, dict) and field_name in prefetched


def _sqlalchemy_relation_prefetched(instance: Any, field_name: str) -> bool | None:
    from sqlalchemy import inspect as sa_inspect

    try:
        state = sa_inspect(instance)
    except Exception:
        return None

    if field_name not in state.mapper.relationships:
        return None

    return field_name not in state.unloaded


def _tortoise_relation_prefetched(instance: Any, field_name: str) -> bool | None:
    meta = getattr(instance, "_meta", None)
    if meta is None:
        return None

    fields_map = getattr(meta, "fields_map", None)
    if not isinstance(fields_map, dict) or field_name not in fields_map:
        return None

    field = fields_map[field_name]
    if getattr(field, "related_model", None) is None:
        return None

    fetched = getattr(instance, "_fetched", None)
    if isinstance(fetched, (set, frozenset)) and field_name in fetched:
        return True
    if isinstance(fetched, dict) and field_name in fetched:
        return True

    return field_name in instance.__dict__


def relation_is_prefetched(
    backend: Backend, instance: Any, field_name: str
) -> bool | None:
    """Return prefetch state for an ORM relation, or None if not applicable."""
    backend_name = backend.__class__.__name__
    if backend_name == "DjangoBackend":
        return _django_relation_prefetched(instance, field_name)
    if backend_name == "SQLAlchemyBackend":
        return _sqlalchemy_relation_prefetched(instance, field_name)
    if backend_name == "TortoiseBackend":
        return _tortoise_relation_prefetched(instance, field_name)
    return None


def _format_unoptimized_loads(loads: list[_UnoptimizedLoad]) -> str:
    grouped: Counter[_UnoptimizedLoad] = Counter(loads)
    lines = []
    for load, count in sorted(
        grouped.items(),
        key=lambda item: (item[0].query_path, item[0].resolver_type, item[0].orm_field),
    ):
        suffix = f" ({count} instances)" if count > 1 else ""
        lines.append(
            f"  - query path: {load.query_path}\n"
            f"    resolver: {load.resolver_type}.{load.graphql_field} "
            f"(ORM: {load.model_name}.{load.orm_field}){suffix}\n"
            f"    {load.hint}"
        )

    total = len(loads)
    unique = len(grouped)
    header = (
        f"Unoptimized relation loads detected ({total} total, {unique} unique) "
        f"— may waterfall (N+1):"
    )
    return f"{header}\n" + "\n".join(lines)


class LazyResolutionExtension(SchemaExtension):
    """Log or raise when a relation resolver loads without optimizer prefetch."""

    _backend: Backend | None = None
    _mode: str = "warn"

    def __init__(self, *, execution_context: Any | None = None) -> None:
        self.execution_context = execution_context  # type: ignore[assignment]
        self._loads: list[_UnoptimizedLoad] = []

    @classmethod
    def configure(
        cls,
        backend: Backend,
        *,
        mode: str = "warn",
    ) -> type[LazyResolutionExtension]:
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend, "_mode": mode},
        )

    def on_operation(self) -> Iterator[None]:
        self._loads = []
        yield
        self._flush_loads()

    def resolve(
        self, _next: Any, root: Any, info: Any, *args: Any, **kwargs: Any
    ) -> Any:
        self._record_relation_access(root, info)
        return _next(root, info, *args, **kwargs)

    async def resolve_async(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self._record_relation_access(root, info)
        return await _next(root, info, *args, **kwargs)

    def _record_relation_access(self, root: Any, info: Any) -> None:
        if self._mode == "off" or self._backend is None or root is None:
            return

        field_name = _field_name_from_info(info)
        if not field_name:
            return

        prefetched = relation_is_prefetched(self._backend, root, field_name)
        if prefetched is None or prefetched:
            return

        model_name = type(root).__name__
        self._loads.append(
            _UnoptimizedLoad(
                orm_field=field_name,
                model_name=model_name,
                query_path=_query_selection_path(info),
                resolver_type=_parent_graphql_type(info),
                graphql_field=_graphql_field_name(info),
                hint=_relation_hint(self._backend, root, field_name),
            )
        )

    def _flush_loads(self) -> None:
        if not self._loads or self._mode == "off":
            return

        message = _format_unoptimized_loads(self._loads)
        logger.warning(message)
        if self._mode == "error":
            raise RuntimeError(message)
        if self._mode == "warn":
            warnings.warn(message, UserWarning, stacklevel=2)
