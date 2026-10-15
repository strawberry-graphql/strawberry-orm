"""Mutation helpers and recursive node mutation factories."""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

import strawberry
from strawberry import relay


def make_ref_type(
    model: type,
    *,
    create: type | None = None,
    update: type | None | Literal[False] = None,
    upsert: type | None = None,
    unlink: bool = False,
    delete: bool = False,
    name: str | None = None,
    descriptions: dict[str, str] | None = None,
) -> type:
    """Generate a ``@oneOf`` input type for managing a related list.

    The returned Strawberry input has up to five ``@oneOf`` variants:
    - ``update`` (default): link an existing object by ID, optionally updating
      its fields.  When no *update* type is provided a minimal ``{id: ID}``
      link-only type is generated automatically.  Pass ``update=False`` to omit.
    - ``create`` (opt-in): create a new object inline.
    - ``upsert`` (opt-in): create-or-update by configured ``where`` fields.
    - ``unlink`` (opt-in): remove an existing object from the relation.
    - ``delete`` (opt-in): hard-delete an existing object.

    ``descriptions`` maps variant names to GraphQL field descriptions.
    """
    type_name = name or f"{model.__name__}Ref"
    descriptions = descriptions or {}
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}

    def unset(variant: str) -> Any:
        if variant in descriptions:
            return strawberry.field(
                default=strawberry.UNSET, description=descriptions[variant]
            )
        return strawberry.UNSET

    if create is not None:
        annotations["create"] = create | None
        defaults["create"] = unset("create")

    if update is not False:
        if update is None:
            update = _make_id_only_input(f"{type_name}LinkInput")
        annotations["update"] = update | None
        defaults["update"] = unset("update")

    if upsert is not None:
        annotations["upsert"] = upsert | None
        defaults["upsert"] = unset("upsert")

    if unlink:
        unlink_type = _make_id_only_input(f"{type_name}UnlinkInput")
        annotations["unlink"] = unlink_type | None
        defaults["unlink"] = strawberry.UNSET

    if delete:
        delete_type = _make_id_only_input(f"{type_name}DeleteInput")
        annotations["delete"] = delete_type | None
        defaults["delete"] = strawberry.UNSET

    if not annotations:
        raise ValueError(f"Ref type {type_name} requires at least one operation")

    ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
    cls = type(type_name, (), ns)
    return strawberry.input(cls, one_of=True)


def _make_id_only_input(name: str) -> type:
    ns: dict[str, Any] = {
        "__annotations__": {"id": strawberry.ID},
    }
    cls = type(name, (), ns)
    return strawberry.input(cls)


def _model_key(model: type) -> str:
    return model.__name__[:1].lower() + model.__name__[1:]


_PROJECT_UNBOUNDED = "__unbounded__"
_PROJECT_SHALLOW = "__shallow__"
_PROJECT_LEAF = "__leaf__"


@strawberry.enum
class RelationRemovalPolicy(Enum):
    DISCONNECT = "DISCONNECT"
    DELETE = "DELETE"


@dataclass(frozen=True)
class RelationSpec:
    name: str
    related_model: type
    kind: Literal["single", "many"]
    relation_mode: Literal["forward_fk", "reverse_fk", "many_to_many"]
    fk_column: str | None = None
    remote_attr: str | None = None
    nullable: bool = True


@dataclass(frozen=True)
class OpFields:
    """Scalar allowlist for a write op. ``fields is None`` means all eligible scalars."""

    fields: tuple[str, ...] | None = None


OnMatch = Literal["UPDATE", "IGNORE", "APPEND"]
OnMatchLinks = Literal["ERROR", "IGNORE"]


@dataclass(frozen=True)
class RelationPolicy:
    # Fields added after the first release stay out of ``repr``: the repr feeds
    # the hashed suffix of every unnamed generated type, so adding one to it
    # would rename those types. ``_project_signature`` adds them back only when
    # they are set.
    on_replace_options: tuple[str, ...] | None = None
    create: OpFields | None = None
    update: OpFields | None = None
    upsert_where: tuple[str, ...] | None = None
    upsert_where_optional: bool = False
    unlink: bool = False
    delete: bool = False
    explicit_ops: bool = False
    name: str | None = dataclasses.field(default=None, repr=False)
    immutable: bool = dataclasses.field(default=False, repr=False)
    on_match: OnMatch | None = dataclasses.field(default=None, repr=False)
    on_match_links: OnMatchLinks | None = dataclasses.field(default=None, repr=False)
    max_visits: int | None = dataclasses.field(default=None, repr=False)

    @property
    def effective_on_match(self) -> OnMatch:
        if self.on_match is not None:
            return self.on_match
        return "APPEND" if self.immutable else "UPDATE"

    @property
    def effective_on_match_links(self) -> OnMatchLinks:
        return self.on_match_links or "ERROR"

    @property
    def update_enabled(self) -> bool:
        """Whether anything may generate update fields for this projection.

        An upsert with the default ``onMatch`` keeps its historical ``update``
        branch, so ``upsert`` alone still enables update fields.
        """
        if self.immutable:
            return False
        if not self.explicit_ops or self.update is not None:
            return True
        return self.upsert_where is not None and self.effective_on_match == "UPDATE"

    @property
    def match_rules_configured(self) -> bool:
        return self.immutable or self.on_match is not None

    def insert_only_signature(self) -> tuple[Any, ...] | None:
        values = (self.immutable, self.on_match, self.on_match_links, self.max_visits)
        if values == (False, None, None, None):
            return None
        return values


_OP_META_KEYS = frozenset({"create", "update", "upsert", "unlink", "delete"})
_ON_MATCH_OPTIONS = ("UPDATE", "IGNORE", "APPEND")
_ON_MATCH_LINKS_OPTIONS = ("ERROR", "IGNORE")
_MATCH_BEHAVIOUR = {
    "UPDATE": "A matched row is updated from `update`.",
    "IGNORE": (
        "Matched rows are not modified, and nothing nested under them is applied."
    ),
    "APPEND": "Matched rows are not modified; nested rows are appended.",
}


class MutationNamespace:
    """Build catch-all graph mutations from the registered ORM types."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._relation_cache: dict[type, dict[str, RelationSpec]] = {}
        self._create_inputs: dict[tuple[type, Any], type] = {}
        self._update_inputs: dict[tuple[type, Any, bool], type] = {}
        self._upsert_inputs: dict[tuple[type, Any, str | None], type] = {}
        self._where_inputs: dict[tuple[type, Any], type] = {}
        self._single_ops: dict[tuple[type, str | None, str, Any], type] = {}
        self._list_ops: dict[tuple[type, str | None, str, Any], type] = {}
        self._root_create_inputs: dict[tuple[tuple[type, Any], ...], type] = {}
        self._root_update_inputs: dict[tuple[tuple[type, Any], ...], type] = {}

    def _normalize_enum_options(
        self,
        value: Any,
        *,
        allowed: tuple[str, ...],
        field_name: str,
        model_name: str,
    ) -> tuple[str, ...]:
        if isinstance(value, str):
            options = (value,)
        elif isinstance(value, (list, tuple)):
            options = tuple(value)
        else:
            raise ValueError(
                f"_meta.{field_name} for {model_name} must be a string or list of strings"
            )

        if not options:
            raise ValueError(f"_meta.{field_name} for {model_name} cannot be empty")

        invalid = [option for option in options if option not in allowed]
        if invalid:
            raise ValueError(
                f"Invalid _meta.{field_name} value(s) for {model_name}: "
                f"{', '.join(invalid)}"
            )

        deduped: list[str] = []
        for option in options:
            if option not in deduped:
                deduped.append(option)
        return tuple(deduped)

    def _default_option(self, options: tuple[str, ...], preferred: str) -> str:
        return preferred if preferred in options else options[0]

    def create_node_input(
        self,
        *,
        models: Iterable[type] | None = None,
        project: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> type:
        selected_models = self._resolve_root_models(models)
        root_projects = self._normalize_root_project(selected_models, project)
        return self._root_input_type(
            "create", selected_models, root_projects, name=name
        )

    def update_node_input(
        self,
        *,
        models: Iterable[type] | None = None,
        project: dict[str, Any] | None = None,
        name: str | None = None,
    ) -> type:
        selected_models = self._resolve_root_models(models)
        root_projects = self._normalize_root_project(selected_models, project)
        return self._root_input_type(
            "update", selected_models, root_projects, name=name
        )

    def upsert_node_input(
        self,
        *,
        model: type,
        project: dict[str, Any],
        name: str | None = None,
    ) -> type:
        """Build a ``{where, create, update}`` input that upserts one *model* row.

        ``project`` is a model projection whose ``_meta.upsert.where`` names the
        match fields. A projection that sets ``_meta.name`` names its types after
        it (``<name>Create``, ``<name>Where``, ...) and may be reused, including
        recursively, wherever that model appears.
        """
        normalized = self._normalize_model_project(model, project)
        if not isinstance(normalized, dict) or normalized["_meta"].upsert_where is None:
            raise ValueError(
                f"upsert_node_input() for {model.__name__} requires _meta.upsert.where"
            )
        return self._upsert_input(model, normalized, name=name)

    def _resolve_root_models(self, models: Iterable[type] | None) -> tuple[type, ...]:
        if models is not None:
            return tuple(models)

        resolved = []
        for model, graphql_type in self._backend._graphql_type_registry.items():
            if issubclass(graphql_type, relay.Node):
                resolved.append(model)

        if not resolved:
            raise ValueError(
                "create_node_input()/update_node_input() require at least one "
                "registered orm.type(...) that subclasses strawberry.relay.Node"
            )

        return tuple(sorted(resolved, key=lambda model: model.__name__))

    def _normalize_root_project(
        self,
        models: tuple[type, ...],
        project: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if project is None:
            return {_model_key(model): _PROJECT_UNBOUNDED for model in models}

        if not isinstance(project, dict):
            raise ValueError("project must be a dict keyed by root model names")

        model_by_key = {_model_key(model): model for model in models}
        unknown = sorted(set(project) - set(model_by_key))
        if unknown:
            raise ValueError(
                f"Unknown root model key(s) in project: {', '.join(unknown)}"
            )

        normalized: dict[str, Any] = {}
        for key, model in model_by_key.items():
            if key not in project:
                normalized[key] = _PROJECT_SHALLOW
                continue
            normalized[key] = self._normalize_model_project(model, project[key])

        return normalized

    def _normalize_model_project(
        self, model: type, project: Any, memo: dict[int, Any] | None = None
    ) -> Any:
        if project in (_PROJECT_UNBOUNDED, _PROJECT_SHALLOW, _PROJECT_LEAF):
            return project
        if not isinstance(project, dict):
            raise ValueError(
                f"Project for model {model.__name__} must be a dict of relation names"
            )
        if not project:
            return {"_meta": RelationPolicy(), "relations": {}}

        memo = {} if memo is None else memo
        if id(project) in memo:
            normalized = memo[id(project)]
            if normalized["_meta"] is None:
                raise ValueError(
                    f"Recursive project for model {model.__name__} requires _meta.name"
                )
            return normalized

        relation_specs = self._relation_specs(model)
        meta = project.get("_meta", {})
        if meta is not None and not isinstance(meta, dict):
            raise ValueError(f"_meta for model {model.__name__} must be a dict")

        policy = self._normalize_relation_policy(model, meta or {})
        normalized: dict[str, Any] = {
            "_meta": policy if policy.name is not None else None,
            "relations": {},
        }
        memo[id(project)] = normalized
        for relation_name, nested_project in project.items():
            if relation_name == "_meta":
                continue
            if relation_name not in relation_specs:
                raise ValueError(
                    f"Unknown relation '{relation_name}' in project for model "
                    f"{model.__name__}"
                )
            normalized["relations"][relation_name] = self._normalize_model_project(
                relation_specs[relation_name].related_model,
                nested_project,
                memo,
            )
        normalized["_meta"] = policy
        return normalized

    def _normalize_relation_policy(
        self, model: type, meta: dict[str, Any]
    ) -> RelationPolicy:
        allowed_meta_keys = {
            "onReplace",
            "name",
            "immutable",
            "maxVisits",
            *_OP_META_KEYS,
        }
        unknown_meta_keys = sorted(set(meta) - allowed_meta_keys)
        if unknown_meta_keys:
            raise ValueError(
                f"Unknown _meta key(s) for model {model.__name__}: "
                f"{', '.join(unknown_meta_keys)}"
            )

        immutable = meta.get("immutable", False)
        if not isinstance(immutable, bool):
            raise ValueError(f"_meta.immutable for {model.__name__} must be a boolean")
        if immutable:
            conflicting = [
                key
                for key in ("update", "unlink", "delete", "onReplace")
                if key in meta
            ]
            if conflicting:
                raise ValueError(
                    f"_meta.immutable for {model.__name__} cannot be combined with "
                    f"{', '.join(conflicting)}"
                )

        max_visits = meta.get("maxVisits")
        if max_visits is not None and (
            isinstance(max_visits, bool)
            or not isinstance(max_visits, int)
            or max_visits < 1
        ):
            raise ValueError(
                f"_meta.maxVisits for {model.__name__} must be a positive integer"
            )

        explicit_ops = bool(set(meta) & _OP_META_KEYS)
        on_replace_options: tuple[str, ...] | None = None
        if "onReplace" in meta:
            on_replace_options = self._normalize_enum_options(
                meta["onReplace"],
                allowed=("DISCONNECT", "DELETE"),
                field_name="onReplace",
                model_name=model.__name__,
            )

        create = (
            self._normalize_op_fields(meta["create"], model=model, field_name="create")
            if "create" in meta
            else None
        )
        update = (
            self._normalize_op_fields(meta["update"], model=model, field_name="update")
            if "update" in meta
            else None
        )

        name = meta.get("name")
        if name is not None and (not isinstance(name, str) or not name):
            raise ValueError(
                f"_meta.name for {model.__name__} must be a non-empty string"
            )

        upsert_where: tuple[str, ...] | None = None
        upsert_where_optional = False
        on_match: OnMatch | None = None
        on_match_links: OnMatchLinks | None = None
        if "upsert" in meta:
            upsert_cfg = meta["upsert"]
            if not isinstance(upsert_cfg, dict):
                raise ValueError(
                    f"_meta.upsert for {model.__name__} must be a dict with 'where'"
                )
            unknown_upsert = sorted(
                set(upsert_cfg) - {"where", "optional", "onMatch", "onMatchLinks"}
            )
            if unknown_upsert:
                raise ValueError(
                    f"Unknown _meta.upsert key(s) for {model.__name__}: "
                    f"{', '.join(unknown_upsert)}"
                )
            if "where" not in upsert_cfg:
                for key in ("onMatch", "onMatchLinks"):
                    if key in upsert_cfg:
                        raise ValueError(
                            f"_meta.upsert.{key} for {model.__name__} requires "
                            f"_meta.upsert.where"
                        )
                raise ValueError(
                    f"_meta.upsert for {model.__name__} requires a non-empty 'where'"
                )
            on_match, on_match_links = self._normalize_match_rules(
                model, meta, upsert_cfg, immutable=immutable
            )
            optional = upsert_cfg.get("optional", False)
            if not isinstance(optional, bool):
                raise ValueError(
                    f"_meta.upsert.optional for {model.__name__} must be a boolean"
                )
            upsert_where_optional = upsert_cfg["where"] is True or optional
            upsert_where = (
                self._all_where_fields(model)
                if upsert_cfg["where"] is True
                else self._normalize_where_fields(upsert_cfg["where"], model=model)
            )

        unlink = self._normalize_bool_op(meta, "unlink", model.__name__)
        delete = self._normalize_bool_op(meta, "delete", model.__name__)

        return RelationPolicy(
            on_replace_options=on_replace_options,
            create=create,
            update=update,
            upsert_where=upsert_where,
            upsert_where_optional=upsert_where_optional,
            unlink=unlink,
            delete=delete,
            explicit_ops=explicit_ops,
            name=name,
            immutable=immutable,
            on_match=on_match,
            on_match_links=on_match_links,
            max_visits=max_visits,
        )

    def _normalize_match_rules(
        self,
        model: type,
        meta: dict[str, Any],
        upsert_cfg: dict[str, Any],
        *,
        immutable: bool,
    ) -> tuple[OnMatch | None, OnMatchLinks | None]:
        on_match = upsert_cfg.get("onMatch")
        if on_match is not None and on_match not in _ON_MATCH_OPTIONS:
            raise ValueError(
                f"_meta.upsert.onMatch for {model.__name__} must be one of "
                f"{', '.join(_ON_MATCH_OPTIONS)}"
            )
        if on_match == "UPDATE":
            if immutable:
                raise ValueError(
                    f"_meta.upsert.onMatch 'UPDATE' for {model.__name__} cannot be "
                    f"combined with _meta.immutable"
                )
            if "update" not in meta:
                raise ValueError(
                    f"_meta.upsert.onMatch 'UPDATE' for {model.__name__} requires "
                    f"_meta.update"
                )

        on_match_links = upsert_cfg.get("onMatchLinks")
        if on_match_links is not None:
            if on_match_links not in _ON_MATCH_LINKS_OPTIONS:
                raise ValueError(
                    f"_meta.upsert.onMatchLinks for {model.__name__} must be one of "
                    f"{', '.join(_ON_MATCH_LINKS_OPTIONS)}"
                )
            effective = on_match or ("APPEND" if immutable else "UPDATE")
            if effective != "APPEND":
                raise ValueError(
                    f"_meta.upsert.onMatchLinks for {model.__name__} only applies "
                    f"when onMatch is APPEND"
                )
        return on_match, on_match_links

    def _normalize_bool_op(
        self, meta: dict[str, Any], field_name: str, model_name: str
    ) -> bool:
        if field_name not in meta:
            return False
        if meta[field_name] is not True:
            raise ValueError(
                f"_meta.{field_name} for {model_name} must be True when present"
            )
        return True

    def _normalize_op_fields(
        self, value: Any, *, model: type, field_name: str
    ) -> OpFields:
        if value is True:
            return OpFields(fields=None)
        if isinstance(value, list):
            names = tuple(value)
            if not all(isinstance(name, str) for name in names):
                raise ValueError(
                    f"_meta.{field_name} for {model.__name__} must be True or a "
                    f"list of field name strings"
                )
            eligible = self._eligible_scalar_names(model, include=names)
            unknown = sorted(set(names) - eligible)
            if unknown:
                raise ValueError(
                    f"Unknown field(s) in _meta.{field_name} for {model.__name__}: "
                    f"{', '.join(unknown)}"
                )
            return OpFields(fields=names)
        raise ValueError(
            f"_meta.{field_name} for {model.__name__} must be True or a list of "
            f"field name strings"
        )

    def _normalize_where_fields(self, value: Any, *, model: type) -> tuple[str, ...]:
        if isinstance(value, str):
            names = (value,)
        elif isinstance(value, list):
            names = tuple(value)
        else:
            raise ValueError(
                f"_meta.upsert.where for {model.__name__} must be True, a string or "
                f"list of strings"
            )
        if not names:
            raise ValueError(f"_meta.upsert.where for {model.__name__} cannot be empty")
        if not all(isinstance(name, str) for name in names):
            raise ValueError(
                f"_meta.upsert.where for {model.__name__} must be True, a string or "
                f"list of field name strings"
            )
        eligible = self._eligible_scalar_names(model, include=names) | {"id"}
        unknown = sorted(set(names) - eligible)
        if unknown:
            raise ValueError(
                f"Unknown field(s) in _meta.upsert.where for {model.__name__}: "
                f"{', '.join(unknown)}"
            )
        deduped: list[str] = []
        for name in names:
            if name not in deduped:
                deduped.append(name)
        return tuple(deduped)

    def _all_where_fields(self, model: type) -> tuple[str, ...]:
        eligible = self._eligible_scalar_names(model)
        return ("id",) + tuple(
            field_name
            for field_name, _type, is_relation, _related in self._backend._introspect_model(
                model
            )
            if not is_relation and field_name in eligible
        )

    def _eligible_scalar_names(
        self, model: type, *, include: tuple[str, ...] | None = None
    ) -> set[str]:
        relation_specs = self._relation_specs(model)
        excluded_scalar_fields = {
            spec.fk_column
            for spec in relation_specs.values()
            if spec.fk_column is not None and spec.relation_mode == "forward_fk"
        }
        pk_names = self._backend._get_pk_names(model)
        names: set[str] = set()
        for (
            field_name,
            _field_type,
            is_relation,
            _related,
        ) in self._backend._introspect_model(model):
            if is_relation:
                continue
            if field_name in excluded_scalar_fields:
                continue
            if field_name in pk_names:
                continue
            if self._backend._exclude_generated_sensitive_field(field_name, include):
                continue
            names.add(field_name)
        return names

    def _policy_from_project(self, project: Any) -> RelationPolicy:
        if project in (_PROJECT_UNBOUNDED, _PROJECT_SHALLOW, _PROJECT_LEAF):
            return RelationPolicy()
        return project["_meta"]

    def _op_fields_for(
        self, project: Any, operation: str, model: type | None = None
    ) -> OpFields:
        policy = self._policy_from_project(project)
        if operation == "create":
            if policy.create is not None:
                return policy.create
            return OpFields(fields=None)
        if operation == "update":
            if not policy.update_enabled:
                model_name = model.__name__ if model is not None else "this model"
                raise ValueError(
                    f"Update fields were requested for {model_name}, whose "
                    f"projection has no update operation"
                )
            if policy.update is not None:
                return policy.update
            return OpFields(fields=None)
        raise ValueError(f"Unsupported operation for field allowlist: {operation}")

    def _enabled_ops(
        self, policy: RelationPolicy, *, kind: Literal["single", "many"]
    ) -> set[str]:
        if not policy.explicit_ops:
            if policy.immutable:
                return {"create"}
            enabled = {"create", "update"}
            if kind == "many":
                enabled.update({"unlink", "delete"})
            return enabled

        enabled: set[str] = set()
        if policy.create is not None:
            enabled.add("create")
        if policy.update is not None:
            enabled.add("update")
        if policy.upsert_where is not None:
            enabled.add("upsert")
        if kind == "many":
            if policy.unlink:
                enabled.add("unlink")
            if policy.delete:
                enabled.add("delete")
        elif policy.unlink or policy.delete:
            raise ValueError(
                "unlink/delete are only valid on list (to-many) relation _meta"
            )
        return enabled

    def _project_signature(self, project: Any) -> Any:
        if project in (_PROJECT_UNBOUNDED, _PROJECT_SHALLOW, _PROJECT_LEAF):
            return project
        policy = project["_meta"]
        if policy.name is not None:
            return ("named", policy.name)
        entries: list[tuple[str, Any]] = [
            ("_meta", policy),
            (
                "relations",
                tuple(
                    sorted(
                        (relation_name, self._project_signature(nested_project))
                        for relation_name, nested_project in project[
                            "relations"
                        ].items()
                    )
                ),
            ),
        ]
        insert_only = policy.insert_only_signature()
        if insert_only is not None:
            entries.append(("insert_only", insert_only))
        return tuple(entries)

    def _project_suffix(self, project: Any) -> str:
        signature = self._project_signature(project)
        return self._signature_suffix(signature)

    def _signature_suffix(self, signature: Any) -> str:
        if signature == _PROJECT_UNBOUNDED:
            return ""
        digest = hashlib.sha1(repr(signature).encode("utf-8")).hexdigest()[:8]
        return f"_{digest}"

    def _project_name(self, project: Any) -> str | None:
        if project in (_PROJECT_UNBOUNDED, _PROJECT_SHALLOW, _PROJECT_LEAF):
            return None
        return project["_meta"].name

    def _type_name(self, model: type, project: Any, kind: str, default: str) -> str:
        name = self._project_name(project)
        if name is None:
            return f"{default}{model.__name__}NodeInput{self._project_suffix(project)}"
        return f"{name}{kind}"

    def _relation_type_name(
        self,
        owner_model: type,
        owner_project: Any,
        spec: RelationSpec,
        child_project: Any,
        *,
        kind: str,
        default: str,
    ) -> str:
        owner_name = self._project_name(owner_project)
        if owner_name is None:
            return (
                f"{owner_model.__name__}{spec.name.title()}{default}"
                f"{self._project_suffix(child_project)}"
            )
        relation = "".join(part[:1].upper() + part[1:] for part in spec.name.split("_"))
        return f"{owner_name}{relation}{kind}"

    def _child_project(self, project: Any, relation_name: str) -> Any:
        if project == _PROJECT_UNBOUNDED:
            return _PROJECT_UNBOUNDED
        if project == _PROJECT_SHALLOW:
            return _PROJECT_LEAF
        if project == _PROJECT_LEAF:
            return _PROJECT_LEAF
        return project["relations"][relation_name]

    def _root_input_type(
        self,
        operation: str,
        models: tuple[type, ...],
        root_projects: dict[str, Any],
        *,
        name: str | None = None,
    ) -> type:
        cache = (
            self._root_create_inputs
            if operation == "create"
            else self._root_update_inputs
        )
        root_signature = tuple(
            (model.__name__, self._project_signature(root_projects[_model_key(model)]))
            for model in models
        )
        cache_key = (root_signature, name)
        if cache_key in cache:
            return cache[cache_key]

        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        for model in models:
            key = _model_key(model)
            if (
                operation == "update"
                and not self._policy_from_project(root_projects[key]).update_enabled
            ):
                raise ValueError(
                    f"update_node_input() cannot include {model.__name__}: its "
                    f"projection has no update operation"
                )
            annotations[key] = (
                self._create_input(model, root_projects[key])
                if operation == "create"
                else self._update_input(model, root_projects[key])
            ) | None
            defaults[key] = strawberry.UNSET

        root_suffix = self._signature_suffix(root_signature)
        class_name = name or (
            f"{'Create' if operation == 'create' else 'Update'}NodeInput{root_suffix}"
        )
        cls = type(
            class_name,
            (),
            {"__annotations__": annotations, **defaults},
        )
        result = strawberry.input(cls, one_of=True)
        result.__mutation_models__ = {  # type: ignore[attr-defined]
            _model_key(model): model for model in models
        }
        cache[cache_key] = result
        return result

    def _create_input(self, model: type, project: Any) -> type:
        signature = self._project_signature(project)
        cache_key = (model, signature)
        if cache_key in self._create_inputs:
            return self._create_inputs[cache_key]

        cls = type(
            self._type_name(model, project, "Create", "Create"),
            (),
            {},
        )
        self._create_inputs[cache_key] = cls
        self._populate_model_input(
            cls,
            model,
            operation="create",
            project=project,
            op_fields=self._op_fields_for(project, "create"),
            include_id=False,
        )
        result = strawberry.input(cls)
        result.__orm_model__ = model  # type: ignore[attr-defined]
        self._create_inputs[cache_key] = result
        return result

    def _update_input(
        self, model: type, project: Any, *, include_id: bool = True
    ) -> type:
        signature = self._project_signature(project)
        cache_key = (model, signature, include_id)
        if cache_key in self._update_inputs:
            return self._update_inputs[cache_key]

        op_fields = self._op_fields_for(project, "update", model)
        cls = type(
            self._type_name(
                model,
                project,
                "Update" if include_id else "Patch",
                "Update" if include_id else "UpdatePatch",
            ),
            (),
            {},
        )
        self._update_inputs[cache_key] = cls
        self._populate_model_input(
            cls,
            model,
            operation="update",
            project=project,
            op_fields=op_fields,
            include_id=include_id,
        )
        result = strawberry.input(cls)
        result.__orm_model__ = model  # type: ignore[attr-defined]
        self._update_inputs[cache_key] = result
        return result

    def _where_input(self, model: type, project: Any) -> type:
        policy = self._policy_from_project(project)
        if policy.upsert_where is None:
            raise ValueError(f"Model {model.__name__} project has no upsert.where")
        signature = self._project_signature(project)
        cache_key = (model, signature)
        if cache_key in self._where_inputs:
            return self._where_inputs[cache_key]

        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        type_by_name = {
            field_name: field_type
            for field_name, field_type, is_relation, _related in self._backend._introspect_model(
                model
            )
            if not is_relation
        }
        for field_name in policy.upsert_where:
            field_type = (
                strawberry.ID if field_name == "id" else type_by_name[field_name]
            )
            if policy.upsert_where_optional:
                annotations[field_name] = field_type | None
                defaults[field_name] = strawberry.UNSET
            else:
                annotations[field_name] = field_type

        cls = type(
            self._type_name(model, project, "Where", "Where"),
            (),
            {"__annotations__": annotations, **defaults},
        )
        result = strawberry.input(cls)
        result.__orm_model__ = model  # type: ignore[attr-defined]
        self._where_inputs[cache_key] = result
        return result

    def _upsert_input(
        self, model: type, project: Any, *, name: str | None = None
    ) -> type:
        signature = self._project_signature(project)
        cache_key = (model, signature, name)
        if cache_key in self._upsert_inputs:
            return self._upsert_inputs[cache_key]

        policy = self._policy_from_project(project)
        on_match = policy.effective_on_match
        cls = type(
            name or self._type_name(model, project, "Upsert", "Upsert"),
            (),
            {},
        )
        self._upsert_inputs[cache_key] = cls
        cls.__annotations__ = {
            "where": self._where_input(model, project),
            "create": self._create_input(model, project) | None,
        }
        if policy.match_rules_configured:
            behaviour = _MATCH_BEHAVIOUR[on_match]
            cls.where = strawberry.field(
                description=f"Fields that find an existing {model.__name__}. "
                f"{behaviour}"
            )
            cls.create = strawberry.field(
                default=strawberry.UNSET,
                description=f"Creates the {model.__name__} when nothing matches "
                f"`where`. {behaviour}",
            )
        else:
            cls.create = strawberry.UNSET
        if on_match == "UPDATE":
            cls.__annotations__["update"] = (
                self._update_input(model, project, include_id=False) | None
            )
            cls.update = strawberry.UNSET
        result = strawberry.input(cls)
        result.__orm_model__ = model  # type: ignore[attr-defined]
        result.__upsert_policy__ = {  # type: ignore[attr-defined]
            "where_fields": policy.upsert_where,
            "on_match": on_match,
            "on_match_links": policy.effective_on_match_links,
            "immutable": policy.immutable,
            "max_visits": policy.max_visits,
        }
        self._upsert_inputs[cache_key] = result
        return result

    def _upsert_field_description(self, model: type, project: Any) -> str | None:
        policy = self._policy_from_project(project)
        if not policy.match_rules_configured:
            return None
        return (
            f"Finds the {model.__name__} by `where`, or creates it from `create`. "
            f"{_MATCH_BEHAVIOUR[policy.effective_on_match]}"
        )

    def _populate_model_input(
        self,
        cls: type,
        model: type,
        *,
        operation: str,
        project: Any,
        op_fields: OpFields,
        include_id: bool,
    ) -> None:
        annotations: dict[str, Any] = {}
        defaults: dict[str, Any] = {}
        relation_specs = self._relation_specs(model)
        excluded_scalar_fields = {
            spec.fk_column
            for spec in relation_specs.values()
            if spec.fk_column is not None and spec.relation_mode == "forward_fk"
        }
        pk_names = self._backend._get_pk_names(model)
        allowed_scalars = None if op_fields.fields is None else set(op_fields.fields)

        for (
            field_name,
            field_type,
            is_relation,
            _related_model,
        ) in self._backend._introspect_model(model):
            if is_relation:
                continue
            if field_name in excluded_scalar_fields:
                continue
            if self._backend._exclude_generated_sensitive_field(
                field_name, op_fields.fields
            ):
                continue
            if field_name in pk_names:
                continue
            if allowed_scalars is not None and field_name not in allowed_scalars:
                continue
            annotations[field_name] = field_type | None
            defaults[field_name] = strawberry.UNSET

        if include_id:
            annotations["id"] = strawberry.ID

        relations: dict[str, RelationSpec] = {}
        if project != _PROJECT_LEAF:
            for field_name, spec in relation_specs.items():
                if isinstance(project, dict) and field_name not in project["relations"]:
                    continue
                relations[field_name] = spec
                child_project = self._child_project(project, field_name)
                if spec.kind == "single":
                    annotations[field_name] = (
                        self._single_relation_input(
                            model, spec, child_project, owner_project=project
                        )
                        | None
                    )
                    defaults[field_name] = strawberry.UNSET
                else:
                    ref = self._list_relation_ref_type(
                        model, spec, child_project, owner_project=project
                    )
                    annotations[field_name] = list[ref] | None
                    defaults[field_name] = strawberry.UNSET

        cls.__annotations__ = annotations
        for name, default in defaults.items():
            setattr(cls, name, default)
        cls.__orm_relations__ = relations  # type: ignore[attr-defined]

    def _single_relation_input(
        self,
        owner_model: type,
        spec: RelationSpec,
        child_project: Any,
        *,
        owner_project: Any = _PROJECT_UNBOUNDED,
    ) -> type:
        cache_key = (
            owner_model,
            self._project_name(owner_project),
            spec.name,
            self._project_signature(child_project),
        )
        if cache_key in self._single_ops:
            return self._single_ops[cache_key]

        cls = type(
            self._relation_type_name(
                owner_model,
                owner_project,
                spec,
                child_project,
                kind="Link",
                default="NodeRelationInput",
            ),
            (),
            {},
        )
        self._single_ops[cache_key] = cls
        policy = self._policy_from_project(child_project)
        enabled = self._enabled_ops(policy, kind="single")
        if not enabled:
            raise ValueError(
                f"Singular relation '{spec.name}' on {owner_model.__name__} "
                f"requires at least one of create, update, upsert"
            )

        annotations: dict[str, Any] = {}
        if "create" in enabled:
            annotations["create"] = (
                self._create_input(spec.related_model, child_project) | None
            )
            cls.create = strawberry.UNSET
        if "update" in enabled:
            annotations["update"] = (
                self._update_input(spec.related_model, child_project) | None
            )
            cls.update = strawberry.UNSET
        if "upsert" in enabled:
            annotations["upsert"] = (
                self._upsert_input(spec.related_model, child_project) | None
            )
            description = self._upsert_field_description(
                spec.related_model, child_project
            )
            cls.upsert = (
                strawberry.field(default=strawberry.UNSET, description=description)
                if description is not None
                else strawberry.UNSET
            )

        cls.__annotations__ = annotations

        can_repoint = (
            "create" in enabled or "upsert" in enabled
        ) and not policy.immutable
        on_replace_options: tuple[str, ...] = ("DISCONNECT",)
        if can_repoint:
            on_replace_options = (
                policy.on_replace_options
                if policy.on_replace_options is not None
                else ("DISCONNECT", "DELETE")
            )
        has_on_replace = len(on_replace_options) > 1
        if has_on_replace:
            cls.__annotations__["on_replace"] = RelationRemovalPolicy | None
            cls.on_replace = strawberry.field(
                default=strawberry.UNSET,
                name="onReplace",
            )
        cls.__relation_policy__ = {  # type: ignore[attr-defined]
            "on_replace_options": on_replace_options,
            "default_on_replace": self._default_option(
                on_replace_options, "DISCONNECT"
            ),
            **self._relation_policy_metadata(policy, enabled),
        }
        cls.__child_project__ = child_project  # type: ignore[attr-defined]
        cls.__orm_model__ = spec.related_model  # type: ignore[attr-defined]

        # ``onReplace`` rides alongside the chosen operation, which a @oneOf
        # input cannot express; the runtime enforces one operation instead.
        result = strawberry.input(cls, one_of=not has_on_replace)
        self._single_ops[cache_key] = result
        return result

    def _relation_policy_metadata(
        self, policy: RelationPolicy, enabled: set[str]
    ) -> dict[str, Any]:
        return {
            "allowed_ops": frozenset(enabled),
            "on_match": policy.effective_on_match,
            "immutable": policy.immutable,
            "max_visits": policy.max_visits,
        }

    def _list_relation_ref_type(
        self,
        owner_model: type,
        spec: RelationSpec,
        child_project: Any,
        *,
        owner_project: Any = _PROJECT_UNBOUNDED,
    ) -> type:
        cache_key = (
            owner_model,
            self._project_name(owner_project),
            spec.name,
            self._project_signature(child_project),
        )
        if cache_key in self._list_ops:
            return self._list_ops[cache_key]

        policy = self._policy_from_project(child_project)
        enabled = self._enabled_ops(policy, kind="many")
        if not enabled:
            raise ValueError(
                f"List relation '{spec.name}' on {owner_model.__name__} "
                f"requires at least one operation"
            )

        create_type = (
            self._create_input(spec.related_model, child_project)
            if "create" in enabled
            else None
        )
        if "update" in enabled:
            update_type: type | None | Literal[False] = self._update_input(
                spec.related_model, child_project
            )
        else:
            update_type = False
        upsert_type = (
            self._upsert_input(spec.related_model, child_project)
            if "upsert" in enabled
            else None
        )
        upsert_description = (
            self._upsert_field_description(spec.related_model, child_project)
            if upsert_type is not None
            else None
        )

        ref_type = make_ref_type(
            spec.related_model,
            create=create_type,
            update=update_type,
            upsert=upsert_type,
            unlink="unlink" in enabled,
            delete="delete" in enabled,
            name=self._relation_type_name(
                owner_model,
                owner_project,
                spec,
                child_project,
                kind="Item",
                default="Ref",
            ),
            descriptions=(
                {"upsert": upsert_description}
                if upsert_description is not None
                else None
            ),
        )
        ref_type.__child_project__ = child_project  # type: ignore[attr-defined]
        ref_type.__orm_model__ = spec.related_model  # type: ignore[attr-defined]
        ref_type.__relation_policy__ = self._relation_policy_metadata(  # type: ignore[attr-defined]
            policy, enabled
        )
        self._list_ops[cache_key] = ref_type
        return ref_type

    def _relation_specs(self, model: type) -> dict[str, RelationSpec]:
        if model in self._relation_cache:
            return self._relation_cache[model]

        backend_name = self._backend.__class__.__name__
        if backend_name == "DjangoBackend":
            specs = _django_relation_specs(model)
        elif backend_name == "SQLAlchemyBackend":
            specs = _sqlalchemy_relation_specs(model)
        elif backend_name == "TortoiseBackend":
            specs = _tortoise_relation_specs(model)
        else:
            raise ValueError(f"Unsupported backend for node mutations: {backend_name}")

        self._relation_cache[model] = specs
        return specs


def _django_relation_specs(model: type) -> dict[str, RelationSpec]:
    specs: dict[str, RelationSpec] = {}
    for field in model._meta.get_fields():  # type: ignore[attr-defined]
        field_type = type(field).__name__
        if field_type in {"ManyToManyField", "ManyToManyRel"}:
            specs[field.name] = RelationSpec(
                name=field.name,
                related_model=field.related_model,
                kind="many",
                relation_mode="many_to_many",
                nullable=True,
            )
        elif field_type == "ManyToOneRel":
            remote_field = field.field.name
            specs[field.name] = RelationSpec(
                name=field.name,
                related_model=field.related_model,
                kind="many",
                relation_mode="reverse_fk",
                remote_attr=remote_field,
                nullable=getattr(field.field, "null", False),
            )
        elif field_type in {"ForeignKey", "OneToOneField"}:
            specs[field.name] = RelationSpec(
                name=field.name,
                related_model=field.related_model,
                kind="single",
                relation_mode="forward_fk",
                fk_column=getattr(field, "attname", None),
                nullable=getattr(field, "null", False),
            )
    return specs


def _sqlalchemy_relation_specs(model: type) -> dict[str, RelationSpec]:
    from sqlalchemy import inspect as sa_inspect

    mapper = sa_inspect(model)
    specs: dict[str, RelationSpec] = {}
    for rel in mapper.relationships:
        direction = rel.direction.name
        if rel.secondary is not None:
            specs[rel.key] = RelationSpec(
                name=rel.key,
                related_model=rel.mapper.class_,
                kind="many",
                relation_mode="many_to_many",
                nullable=True,
            )
        elif rel.uselist:
            specs[rel.key] = RelationSpec(
                name=rel.key,
                related_model=rel.mapper.class_,
                kind="many",
                relation_mode="reverse_fk",
                remote_attr=rel.back_populates,
                nullable=all(column.nullable for column in rel.remote_side),
            )
        else:
            fk_column = next(iter(rel.local_columns)).key if rel.local_columns else None
            specs[rel.key] = RelationSpec(
                name=rel.key,
                related_model=rel.mapper.class_,
                kind="single",
                relation_mode="forward_fk"
                if direction == "MANYTOONE"
                else "forward_fk",
                fk_column=fk_column,
                nullable=all(column.nullable for column in rel.local_columns)
                if rel.local_columns
                else True,
            )
    return specs


def _tortoise_relation_specs(model: type) -> dict[str, RelationSpec]:
    specs: dict[str, RelationSpec] = {}
    for field_name, field in model._meta.fields_map.items():  # type: ignore[attr-defined]
        related_model = getattr(field, "related_model", None)
        if related_model is None:
            continue

        field_type = type(field).__name__
        if field_type == "ForeignKeyFieldInstance":
            specs[field_name] = RelationSpec(
                name=field_name,
                related_model=related_model,
                kind="single",
                relation_mode="forward_fk",
                fk_column=getattr(field, "source_field", None),
                nullable=getattr(field, "null", False),
            )
        elif field_type == "ManyToManyFieldInstance":
            specs[field_name] = RelationSpec(
                name=field_name,
                related_model=related_model,
                kind="many",
                relation_mode="many_to_many",
                nullable=True,
            )
        elif field_type == "BackwardFKRelation":
            relation_field = getattr(field, "relation_field", "")
            specs[field_name] = RelationSpec(
                name=field_name,
                related_model=related_model,
                kind="many",
                relation_mode="reverse_fk",
                remote_attr=relation_field[:-3]
                if relation_field.endswith("_id")
                else relation_field,
                nullable=getattr(
                    related_model._meta.fields_map.get(relation_field),  # type: ignore[attr-defined]
                    "null",
                    False,
                ),
            )
    return specs
