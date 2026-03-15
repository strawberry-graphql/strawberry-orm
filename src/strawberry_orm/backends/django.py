"""Django backend -- built from scratch using Django model introspection."""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any, Optional

import strawberry
from strawberry.extensions import SchemaExtension

from strawberry_orm.optimizer import OptimizerExtension

from ._base import BaseBackend, extract_element_type, input_to_dict

_DJANGO_FIELD_MAP: dict[str, type] = {
    "AutoField": int,
    "BigAutoField": int,
    "SmallAutoField": int,
    "IntegerField": int,
    "SmallIntegerField": int,
    "BigIntegerField": int,
    "PositiveIntegerField": int,
    "PositiveSmallIntegerField": int,
    "PositiveBigIntegerField": int,
    "FloatField": float,
    "DecimalField": Decimal,
    "CharField": str,
    "TextField": str,
    "SlugField": str,
    "URLField": str,
    "EmailField": str,
    "FilePathField": str,
    "BooleanField": bool,
    "NullBooleanField": bool,
    "DateField": datetime.date,
    "TimeField": datetime.time,
    "DateTimeField": datetime.datetime,
    "DurationField": str,
    "UUIDField": str,
    "GenericIPAddressField": str,
    "IPAddressField": str,
    "FileField": str,
    "ImageField": str,
    "BinaryField": bytes,
    "JSONField": str,
}


class DjangoBackend(BaseBackend):
    """Backend adapter for Django."""

    def __init__(
        self,
        *,
        max_filter_depth: int = 10,
        max_filter_branches: int = 50,
        enable_regex_filters: bool = False,
        max_in_list_size: int = 500,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._max_filter_depth = max_filter_depth
        self._max_filter_branches = max_filter_branches
        self._enable_regex_filters = enable_regex_filters
        self._max_in_list_size = max_in_list_size

    def _introspect_model(
        self, model: type
    ) -> list[tuple[str, type, bool, type | None]]:
        """Return (field_name, python_type, is_relation, related_model) for each
        field on a Django model."""
        meta = model._meta  # type: ignore[attr-defined]
        result: list[tuple[str, type, bool, type | None]] = []

        for field in meta.get_fields():
            field_class_name = type(field).__name__

            if field_class_name in (
                "ManyToManyField",
                "ManyToManyRel",
                "ManyToOneRel",
                "OneToOneRel",
                "ForeignObject",
            ):
                related_model = (
                    field.related_model if hasattr(field, "related_model") else None
                )
                result.append((field.name, Any, True, related_model))
                continue

            if field_class_name in ("ForeignKey", "OneToOneField"):
                related_model = (
                    field.related_model if hasattr(field, "related_model") else None
                )
                result.append((field.name, Any, True, related_model))
                # Also expose the _id column as a concrete integer field
                attname = getattr(field, "attname", None)
                if attname and attname != field.name:
                    result.append((attname, int, False, None))
                continue

            py_type = _DJANGO_FIELD_MAP.get(field_class_name, str)
            result.append((field.name, py_type, False, None))

        return result

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        include = kwargs.get("include")
        exclude = kwargs.get("exclude")
        name = kwargs.get("name")
        filters = kwargs.get("filters")
        order = kwargs.get("order")

        def decorator(cls: type) -> Any:
            meta = model._meta  # type: ignore[attr-defined]

            col_types: dict[str, type] = {}
            rel_fields: dict[str, dict[str, Any]] = {}

            for field_obj in meta.get_fields():
                field_class_name = type(field_obj).__name__
                if field_class_name in (
                    "ManyToManyField",
                    "ManyToManyRel",
                    "ManyToOneRel",
                    "OneToOneRel",
                ):
                    rel_fields[field_obj.name] = {
                        "kind": "many" if field_class_name != "OneToOneRel" else "one",
                        "model": field_obj.related_model,
                    }
                    continue
                if field_class_name in ("ForeignKey", "OneToOneField"):
                    rel_fields[field_obj.name] = {
                        "kind": "fk" if field_class_name == "ForeignKey" else "one",
                        "model": field_obj.related_model,
                    }
                    attname = getattr(field_obj, "attname", None)
                    if attname and attname != field_obj.name:
                        fk_type: type = int
                        if getattr(field_obj, "null", False):
                            fk_type = Optional[int]
                        col_types[attname] = fk_type
                    continue
                py_type = _DJANGO_FIELD_MAP.get(field_class_name, str)
                col_types[field_obj.name] = py_type

            type_name = self._process_type_annotations(
                cls,
                model,
                col_types,
                include=include,
                exclude=exclude,
                name=name,
                filters=filters,
                order=order,
            )

            annotations = getattr(cls, "__annotations__", {})
            for field_name, rel_info in rel_fields.items():
                if field_name not in annotations:
                    continue
                if field_name in vars(cls):
                    continue
                kind = rel_info["kind"]
                if kind == "many":
                    ann = annotations[field_name]
                    el_type = extract_element_type(ann)
                    f_type = (
                        getattr(el_type, "__orm_filter__", None) if el_type else None
                    )
                    o_type = (
                        getattr(el_type, "__orm_order__", None) if el_type else None
                    )
                    rel_model = rel_info["model"]

                    if f_type or o_type:
                        setattr(
                            cls,
                            field_name,
                            _make_dj_rel_resolver(
                                self,
                                field_name,
                                rel_model,
                                f_type,
                                o_type,
                            ),
                        )
                    else:

                        def _make_resolver(fname: str, return_ann: Any) -> Any:
                            def resolver(self: Any) -> Any:
                                return list(getattr(self, fname).all())

                            resolver.__name__ = fname
                            resolver.__annotations__ = {"return": return_ann}
                            return strawberry.field(resolver=resolver)

                        setattr(cls, field_name, _make_resolver(field_name, ann))

            return self._finalize_type(cls, model, type_name, name)

        return decorator

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Any | None = None,
        mode: str = "replace",
    ) -> None:
        manager = getattr(instance, field)
        rel_model = manager.model

        new_related: list[Any] = []
        to_delete: list[Any] = []

        for ref in refs:
            ref_id = getattr(ref, "id", strawberry.UNSET)
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_id is not strawberry.UNSET and ref_id is not None:
                if authorize and not authorize("link", rel_model, ref_id, info):
                    continue
                obj = rel_model.objects.get(pk=ref_id)
                new_related.append(obj)
            elif ref_create is not strawberry.UNSET and ref_create is not None:
                if authorize and not authorize("create", rel_model, None, info):
                    continue
                obj = rel_model.objects.create(**input_to_dict(ref_create))
                new_related.append(obj)
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                data = input_to_dict(ref_update)
                pk = data.pop("id")
                if authorize and not authorize("update", rel_model, pk, info):
                    continue
                rel_model.objects.filter(pk=pk).update(**data)
                obj = rel_model.objects.get(pk=pk)
                new_related.append(obj)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                if authorize and not authorize(
                    "delete", rel_model, ref_delete.id, info
                ):
                    continue
                to_delete.append(ref_delete.id)

        if mode == "patch":
            if new_related:
                manager.add(*new_related)
            if to_delete:
                manager.remove(*rel_model.objects.filter(pk__in=to_delete))
                if self._hard_delete_refs:
                    rel_model.objects.filter(pk__in=to_delete).delete()
        else:
            manager.set(new_related)
            if to_delete and self._hard_delete_refs:
                rel_model.objects.filter(pk__in=to_delete).delete()

    # -- Query application ----------------------------------------------------

    def apply_filters(self, query: Any, filter_input: Any, model: type) -> Any:
        q_obj = _build_django_filter(
            filter_input,
            max_depth=self._max_filter_depth,
            max_branches=self._max_filter_branches,
            enable_regex=self._enable_regex_filters,
            max_in_list_size=self._max_in_list_size,
        )
        if q_obj is not None:
            query = query.filter(q_obj)
        return query

    def apply_ordering(self, query: Any, order_input: Any, model: type) -> Any:
        order_list = order_input if isinstance(order_input, list) else [order_input]
        clauses: list[Any] = []
        for entry in order_list:
            clauses.extend(_build_django_ordering(entry))
        if clauses:
            query = query.order_by(*clauses)
        return query

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        qs = model.objects.all()
        if self._default_query_limit is not None:
            qs = qs[: self._default_query_limit]
        return qs

    def is_query_object(self, value: Any) -> bool:
        try:
            from django.db.models import QuerySet

            return isinstance(value, QuerySet)
        except ImportError:
            return False

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        return OptimizerExtension.configure(backend=self, store=self._store)

    def apply_optimizer_hints(self, store: Any, query: Any, info: Any) -> Any:
        import re

        try:
            model = query.model
        except AttributeError:
            return query

        get_qs = self._type_querysets.get(model)
        if get_qs is not None:
            query = get_qs(query, info)

        select_related: list[str] = []
        prefetch_related: list[Any] = []

        def _to_snake(name: str) -> str:
            return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()

        def _get_nested_queryset(
            parent_model: type,
            field_name: str,
            related_model: type,
        ) -> Any:
            """Build a custom queryset if the field has a load callable or
            the related model's type defines get_queryset."""
            get_qs = self._type_querysets.get(related_model)

            type_name = self._type_name_for_model(parent_model)
            load_fn = None
            if type_name and store:
                hints = store.get(type_name, field_name)
                if hints and callable(hints.load):
                    load_fn = hints.load

            if get_qs is None and load_fn is None:
                return None

            qs = related_model.objects.all()
            if get_qs is not None:
                qs = get_qs(qs, info)
            if load_fn is not None:
                qs = load_fn(qs)
            return qs

        def _walk_selections(
            selection_set: Any,
            current_model: type,
            prefix: str = "",
            in_prefetch: bool = False,
        ) -> None:
            if selection_set is None:
                return
            meta = current_model._meta  # type: ignore[attr-defined]
            for node in selection_set.selections:
                field_name = _to_snake(node.name.value)
                full_path = f"{prefix}__{field_name}" if prefix else field_name
                try:
                    field_obj = meta.get_field(field_name)
                except Exception:
                    continue

                field_class_name = type(field_obj).__name__

                if field_class_name in ("ForeignKey", "OneToOneField"):
                    attname = getattr(field_obj, "attname", None)
                    if (
                        attname
                        and field_name == attname
                        and field_name != field_obj.name
                    ):
                        continue
                    related_model = field_obj.related_model
                    custom_qs = _get_nested_queryset(
                        current_model, field_name, related_model
                    )
                    if custom_qs is not None:
                        from django.db.models import Prefetch

                        prefetch_related.append(Prefetch(full_path, queryset=custom_qs))
                        if node.selection_set:
                            _walk_selections(
                                node.selection_set,
                                related_model,
                                full_path,
                                in_prefetch=True,
                            )
                    elif in_prefetch:
                        prefetch_related.append(full_path)
                        if node.selection_set:
                            _walk_selections(
                                node.selection_set,
                                related_model,
                                full_path,
                                in_prefetch,
                            )
                    else:
                        select_related.append(full_path)
                        if node.selection_set:
                            _walk_selections(
                                node.selection_set,
                                related_model,
                                full_path,
                                in_prefetch,
                            )
                elif field_class_name == "OneToOneRel":
                    related_model = field_obj.related_model
                    custom_qs = _get_nested_queryset(
                        current_model, field_name, related_model
                    )
                    if custom_qs is not None:
                        from django.db.models import Prefetch

                        prefetch_related.append(Prefetch(full_path, queryset=custom_qs))
                        if node.selection_set:
                            _walk_selections(
                                node.selection_set,
                                related_model,
                                full_path,
                                in_prefetch=True,
                            )
                    elif in_prefetch:
                        prefetch_related.append(full_path)
                        if node.selection_set:
                            _walk_selections(
                                node.selection_set,
                                related_model,
                                full_path,
                                in_prefetch,
                            )
                    else:
                        select_related.append(full_path)
                        if node.selection_set:
                            _walk_selections(
                                node.selection_set,
                                related_model,
                                full_path,
                                in_prefetch,
                            )
                elif field_class_name in (
                    "ManyToManyField",
                    "ManyToManyRel",
                    "ManyToOneRel",
                ):
                    related_model = field_obj.related_model
                    custom_qs = _get_nested_queryset(
                        current_model, field_name, related_model
                    )
                    if custom_qs is not None:
                        from django.db.models import Prefetch

                        prefetch_related.append(Prefetch(full_path, queryset=custom_qs))
                    else:
                        prefetch_related.append(full_path)
                    if node.selection_set:
                        _walk_selections(
                            node.selection_set,
                            related_model,
                            full_path,
                            in_prefetch=True,
                        )

                type_name = self._type_name_for_model(current_model)
                if type_name and store:
                    hints = store.get(type_name, field_name)
                    if hints and not hints.disable_optimization:
                        if hints.load and not callable(hints.load):
                            for rel_name in hints.load:
                                try:
                                    rel_field = meta.get_field(rel_name)
                                except Exception:
                                    continue
                                rel_class = type(rel_field).__name__
                                rel_path = (
                                    f"{prefix}__{rel_name}" if prefix else rel_name
                                )
                                is_fk = rel_class in (
                                    "ForeignKey",
                                    "OneToOneField",
                                    "OneToOneRel",
                                )
                                if is_fk and not in_prefetch:
                                    select_related.append(rel_path)
                                else:
                                    prefetch_related.append(rel_path)

        for field_node in info.field_nodes:
            _walk_selections(field_node.selection_set, model)

        only_fields: list[str] = []

        type_name_root = self._type_name_for_model(model)
        if type_name_root and store:
            for field_node in info.field_nodes:
                if field_node.selection_set:
                    for sel in field_node.selection_set.selections:
                        fname = _to_snake(sel.name.value)
                        hints = store.get(type_name_root, fname)
                        if hints and hints.only:
                            only_fields.extend(hints.only)

        if select_related:
            query = query.select_related(*select_related)
        if prefetch_related:
            query = query.prefetch_related(*prefetch_related)
        if only_fields:
            query = query.only(*only_fields)

        return list(query)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_dj_rel_resolver(
    backend: Any,
    fname: str,
    rel_model: type,
    filter_type: Any,
    order_type: Any,
) -> Any:
    """Create a Strawberry field for a Django many-relation with filter/order."""
    if filter_type and order_type:

        def resolver(self: Any, filter: Any = None, order: Any = None) -> Any:
            qs = getattr(self, fname).all()
            if filter is not None:
                qs = backend.apply_filters(qs, filter, rel_model)
            if order is not None:
                qs = backend.apply_ordering(qs, order, rel_model)
            return list(qs)

        resolver.__annotations__ = {
            "filter": Optional[filter_type],
            "order": Optional[list[order_type]],
        }
    elif filter_type:

        def resolver(self: Any, filter: Any = None) -> Any:
            qs = getattr(self, fname).all()
            if filter is not None:
                qs = backend.apply_filters(qs, filter, rel_model)
            return list(qs)

        resolver.__annotations__ = {"filter": Optional[filter_type]}
    else:

        def resolver(self: Any, order: Any = None) -> Any:
            qs = getattr(self, fname).all()
            if order is not None:
                qs = backend.apply_ordering(qs, order, rel_model)
            return list(qs)

        resolver.__annotations__ = {"order": Optional[list[order_type]]}

    return strawberry.field(resolver=resolver)


# ---------------------------------------------------------------------------
# Filter translation
# ---------------------------------------------------------------------------

_LOOKUP_TO_DJANGO: dict[str, str] = {
    "exact": "exact",
    "neq": "exact",  # handled with exclude
    "gt": "gt",
    "gte": "gte",
    "lt": "lt",
    "lte": "lte",
    "contains": "contains",
    "i_contains": "icontains",
    "starts_with": "startswith",
    "i_starts_with": "istartswith",
    "ends_with": "endswith",
    "i_ends_with": "iendswith",
    "regex": "regex",
    "i_regex": "iregex",
}


def _build_django_filter(
    filter_input: Any,
    *,
    max_depth: int = 10,
    max_branches: int = 50,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
    _depth: int = 0,
) -> Any:
    from django.db.models import Q

    if _depth > max_depth:
        raise ValueError(f"Filter nesting exceeds maximum depth of {max_depth}")

    if filter_input is None or filter_input is strawberry.UNSET:
        return None

    fields = filter_input.__class__.__dataclass_fields__
    recurse_kw = dict(
        max_depth=max_depth,
        max_branches=max_branches,
        enable_regex=enable_regex,
        max_in_list_size=max_in_list_size,
        _depth=_depth + 1,
    )

    for key in fields:
        val = getattr(filter_input, key)
        if val is strawberry.UNSET or val is None:
            continue

        if key == "field":
            return _build_django_field_clause(
                val,
                enable_regex=enable_regex,
                max_in_list_size=max_in_list_size,
            )
        elif key == "all":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            sub = [_build_django_filter(f, **recurse_kw) for f in val]
            sub = [s for s in sub if s is not None]
            result = Q()
            for s in sub:
                result &= s
            return result if sub else None
        elif key == "any":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            sub = [_build_django_filter(f, **recurse_kw) for f in val]
            sub = [s for s in sub if s is not None]
            result = Q()
            for i, s in enumerate(sub):
                result = s if i == 0 else result | s
            return result if sub else None
        elif key == "not_":
            inner = _build_django_filter(val, **recurse_kw)
            return ~inner if inner is not None else None
        elif key == "one_of":
            if len(val) > max_branches:
                raise ValueError(
                    f"Filter has {len(val)} branches; maximum is {max_branches}"
                )
            sub = [_build_django_filter(f, **recurse_kw) for f in val]
            sub = [s for s in sub if s is not None]
            result = Q()
            for i, s in enumerate(sub):
                result = s if i == 0 else result | s
            return result if sub else None

    return None


def _build_django_field_clause(
    field_input: Any,
    *,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    from django.db.models import Q

    q = Q()
    fields = field_input.__class__.__dataclass_fields__

    for col_name in fields:
        lookup = getattr(field_input, col_name)
        if lookup is strawberry.UNSET or lookup is None:
            continue

        col_q = _build_django_lookup(
            col_name,
            lookup,
            enable_regex=enable_regex,
            max_in_list_size=max_in_list_size,
        )
        q &= col_q

    return q


def _build_django_lookup(
    col_name: str,
    lookup: Any,
    *,
    enable_regex: bool = False,
    max_in_list_size: int = 500,
) -> Any:
    from django.db.models import Q

    q = Q()
    fields = lookup.__class__.__dataclass_fields__

    for op_name in fields:
        val = getattr(lookup, op_name)
        if val is strawberry.UNSET or val is None:
            continue

        if op_name == "is_null":
            q &= Q(**{f"{col_name}__isnull": val})
        elif op_name in ("in_list", "not_in_list"):
            if len(val) > max_in_list_size:
                raise ValueError(
                    f"in_list/not_in_list has {len(val)} items; "
                    f"maximum is {max_in_list_size}"
                )
            if op_name == "in_list":
                q &= Q(**{f"{col_name}__in": val})
            else:
                q &= ~Q(**{f"{col_name}__in": val})
        elif op_name == "range":
            q &= Q(**{f"{col_name}__range": (val.start, val.end)})
        elif op_name == "neq":
            q &= ~Q(**{f"{col_name}__exact": val})
        elif op_name in ("regex", "i_regex"):
            if not enable_regex:
                raise ValueError(
                    "Regex filters are disabled. Pass enable_regex_filters=True "
                    "to enable."
                )
            django_lookup = _LOOKUP_TO_DJANGO[op_name]
            q &= Q(**{f"{col_name}__{django_lookup}": val})
        elif op_name in _LOOKUP_TO_DJANGO:
            django_lookup = _LOOKUP_TO_DJANGO[op_name]
            q &= Q(**{f"{col_name}__{django_lookup}": val})

    return q


# ---------------------------------------------------------------------------
# Ordering translation
# ---------------------------------------------------------------------------


def _build_django_ordering(order_input: Any) -> list[str]:
    clauses: list[str] = []
    fields = order_input.__class__.__dataclass_fields__

    for col_name in fields:
        direction = getattr(order_input, col_name)
        if direction is strawberry.UNSET or direction is None:
            continue

        dir_value = direction.value if hasattr(direction, "value") else str(direction)

        from django.db.models import F

        nulls_first = True if "NULLS_FIRST" in dir_value else None
        nulls_last = True if "NULLS_LAST" in dir_value else None

        if dir_value.startswith("DESC"):
            expr = F(col_name).desc(nulls_first=nulls_first, nulls_last=nulls_last)
        else:
            expr = F(col_name).asc(nulls_first=nulls_first, nulls_last=nulls_last)
        clauses.append(expr)

    return clauses
