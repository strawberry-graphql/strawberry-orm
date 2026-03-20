"""Mutation helpers and recursive node mutation factories."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
from typing import Any, Iterable, Literal

import strawberry
from strawberry import relay
from strawberry.types.cast import cast as strawberry_cast


def make_ref_type(
    model: type,
    *,
    create: type | None = None,
    update: type | None = None,
    unlink: bool = False,
    delete: bool = False,
    name: str | None = None,
) -> type:
    """Generate a ``@oneOf`` input type for managing a related list.

    The returned Strawberry input has up to four ``@oneOf`` variants:
    - ``update`` (always): link an existing object by ID, optionally updating
      its fields.  When no *update* type is provided a minimal ``{id: ID}``
      link-only type is generated automatically.
    - ``create`` (opt-in): create a new object inline.
    - ``unlink`` (opt-in): remove an existing object from the relation.
    - ``delete`` (opt-in): hard-delete an existing object.
    """
    type_name = name or f"{model.__name__}Ref"
    annotations: dict[str, Any] = {}
    defaults: dict[str, Any] = {}

    if create is not None:
        annotations["create"] = create | None
        defaults["create"] = strawberry.UNSET

    if update is None:
        update = _make_id_only_input(f"{type_name}LinkInput")
    annotations["update"] = update | None
    defaults["update"] = strawberry.UNSET

    if unlink:
        unlink_type = _make_id_only_input(f"{type_name}UnlinkInput")
        annotations["unlink"] = unlink_type | None
        defaults["unlink"] = strawberry.UNSET

    if delete:
        delete_type = _make_id_only_input(f"{type_name}DeleteInput")
        annotations["delete"] = delete_type | None
        defaults["delete"] = strawberry.UNSET

    ns: dict[str, Any] = {"__annotations__": annotations, **defaults}
    cls = type(type_name, (), ns)
    return strawberry.input(cls, one_of=True)


def _make_id_only_input(name: str) -> type:
    ns: dict[str, Any] = {
        "__annotations__": {"id": strawberry.ID},
    }
    cls = type(name, (), ns)
    return strawberry.input(cls)


def _input_values(obj: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field_name in obj.__class__.__dataclass_fields__:
        value = getattr(obj, field_name)
        if value is not strawberry.UNSET:
            result[field_name] = value
    return result


def _model_key(model: type) -> str:
    return model.__name__[:1].lower() + model.__name__[1:]


def _primary_key_value(instance: Any) -> Any:
    return getattr(instance, "pk", getattr(instance, "id", None))


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
class RelationPolicy:
    on_replace_options: tuple[str, ...] | None = None


class MutationNamespace:
    """Build catch-all graph mutations from the registered ORM types."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._relation_cache: dict[type, dict[str, RelationSpec]] = {}
        self._create_inputs: dict[tuple[type, Any], type] = {}
        self._update_inputs: dict[tuple[type, Any], type] = {}
        self._single_ops: dict[tuple[type, str, Any], type] = {}
        self._list_ops: dict[tuple[type, str, Any], type] = {}
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

    def create_node(
        self,
        *,
        models: Iterable[type] | None = None,
        project: dict[str, Any] | None = None,
        input_name: str | None = None,
        description: str | None = None,
    ) -> Any:
        factory = self
        input_type = self.create_node_input(
            models=models, project=project, name=input_name
        )

        if self._backend.__class__.__name__ == "TortoiseBackend":

            async def resolver(root: Any, info: Any, input: Any) -> relay.Node:
                model, payload = factory._select_model_payload(input, input_type)
                instance = await factory._create_async(model, payload, info)
                return factory._cast_node(model, instance)

        else:

            def resolver(root: Any, info: Any, input: Any) -> relay.Node:
                model, payload = factory._select_model_payload(input, input_type)
                instance = factory._create_sync(model, payload, info)
                return factory._cast_node(model, instance)

        resolver.__annotations__ = {
            "info": strawberry.types.Info,
            "input": input_type,
            "return": relay.Node,
        }
        return strawberry.field(resolver=resolver, description=description)

    def update_node(
        self,
        *,
        models: Iterable[type] | None = None,
        project: dict[str, Any] | None = None,
        input_name: str | None = None,
        description: str | None = None,
    ) -> Any:
        factory = self
        input_type = self.update_node_input(
            models=models, project=project, name=input_name
        )

        if self._backend.__class__.__name__ == "TortoiseBackend":

            async def resolver(root: Any, info: Any, input: Any) -> relay.Node:
                model, payload = factory._select_model_payload(input, input_type)
                instance = await factory._update_async(model, payload, info)
                return factory._cast_node(model, instance)

        else:

            def resolver(root: Any, info: Any, input: Any) -> relay.Node:
                model, payload = factory._select_model_payload(input, input_type)
                instance = factory._update_sync(model, payload, info)
                return factory._cast_node(model, instance)

        resolver.__annotations__ = {
            "info": strawberry.types.Info,
            "input": input_type,
            "return": relay.Node,
        }
        return strawberry.field(resolver=resolver, description=description)

    def _resolve_root_models(self, models: Iterable[type] | None) -> tuple[type, ...]:
        if models is not None:
            return tuple(models)

        resolved = []
        for model, graphql_type in self._backend._graphql_type_registry.items():
            if issubclass(graphql_type, relay.Node):
                resolved.append(model)

        if not resolved:
            raise ValueError(
                "create_node_input()/update_node_input() and create_node()/update_node() "
                "require at least one registered orm.type(...) that subclasses "
                "strawberry.relay.Node"
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

    def _normalize_model_project(self, model: type, project: Any) -> Any:
        if project in (_PROJECT_UNBOUNDED, _PROJECT_SHALLOW, _PROJECT_LEAF):
            return project
        if not isinstance(project, dict):
            raise ValueError(
                f"Project for model {model.__name__} must be a dict of relation names"
            )
        if not project:
            return {"_meta": RelationPolicy(), "relations": {}}

        relation_specs = self._relation_specs(model)
        meta = project.get("_meta", {})
        if meta is not None and not isinstance(meta, dict):
            raise ValueError(f"_meta for model {model.__name__} must be a dict")

        policy = RelationPolicy()
        if meta:
            allowed_meta_keys = {"onReplace"}
            unknown_meta_keys = sorted(set(meta) - allowed_meta_keys)
            if unknown_meta_keys:
                raise ValueError(
                    f"Unknown _meta key(s) for model {model.__name__}: "
                    f"{', '.join(unknown_meta_keys)}"
                )
            if "onReplace" in meta:
                policy = RelationPolicy(
                    on_replace_options=self._normalize_enum_options(
                        meta["onReplace"],
                        allowed=("DISCONNECT", "DELETE"),
                        field_name="onReplace",
                        model_name=model.__name__,
                    ),
                )

        normalized_relations: dict[str, Any] = {}
        for relation_name, nested_project in project.items():
            if relation_name == "_meta":
                continue
            if relation_name not in relation_specs:
                raise ValueError(
                    f"Unknown relation '{relation_name}' in project for model "
                    f"{model.__name__}"
                )
            normalized_relations[relation_name] = self._normalize_model_project(
                relation_specs[relation_name].related_model,
                nested_project,
            )
        return {"_meta": policy, "relations": normalized_relations}

    def _project_signature(self, project: Any) -> Any:
        if project in (_PROJECT_UNBOUNDED, _PROJECT_SHALLOW, _PROJECT_LEAF):
            return project
        return tuple(
            [
                ("_meta", project["_meta"]),
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
        )

    def _project_suffix(self, project: Any) -> str:
        signature = self._project_signature(project)
        return self._signature_suffix(signature)

    def _signature_suffix(self, signature: Any) -> str:
        if signature == _PROJECT_UNBOUNDED:
            return ""
        digest = hashlib.sha1(repr(signature).encode("utf-8")).hexdigest()[:8]
        return f"_{digest}"

    def _child_project(self, project: Any, relation_name: str) -> Any:
        if project == _PROJECT_UNBOUNDED:
            return _PROJECT_UNBOUNDED
        if project == _PROJECT_SHALLOW:
            return _PROJECT_LEAF
        if project == _PROJECT_LEAF:
            return _PROJECT_LEAF
        return project["relations"].get(relation_name, _PROJECT_SHALLOW)

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
            f"Create{model.__name__}NodeInput{self._project_suffix(project)}",
            (),
            {},
        )
        self._create_inputs[cache_key] = cls
        self._populate_model_input(cls, model, operation="create", project=project)
        result = strawberry.input(cls)
        self._create_inputs[cache_key] = result
        return result

    def _update_input(self, model: type, project: Any) -> type:
        signature = self._project_signature(project)
        cache_key = (model, signature)
        if cache_key in self._update_inputs:
            return self._update_inputs[cache_key]

        cls = type(
            f"Update{model.__name__}NodeInput{self._project_suffix(project)}",
            (),
            {},
        )
        self._update_inputs[cache_key] = cls
        self._populate_model_input(cls, model, operation="update", project=project)
        result = strawberry.input(cls)
        self._update_inputs[cache_key] = result
        return result

    def _populate_model_input(
        self,
        cls: type,
        model: type,
        *,
        operation: str,
        project: Any,
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
            if self._backend._exclude_generated_sensitive_field(field_name, None):
                continue
            if operation == "create" and field_name in pk_names:
                continue
            if operation == "update" and field_name in pk_names:
                continue
            annotations[field_name] = field_type | None
            defaults[field_name] = strawberry.UNSET

        if operation == "update":
            annotations["id"] = strawberry.ID

        if project != _PROJECT_LEAF:
            for field_name, spec in relation_specs.items():
                child_project = self._child_project(project, field_name)
                if spec.kind == "single":
                    annotations[field_name] = (
                        self._single_relation_input(model, spec, child_project) | None
                    )
                    defaults[field_name] = strawberry.UNSET
                else:
                    ref = self._list_relation_ref_type(model, spec, child_project)
                    annotations[field_name] = list[ref] | None
                    defaults[field_name] = strawberry.UNSET

        cls.__annotations__ = annotations
        for name, default in defaults.items():
            setattr(cls, name, default)

    def _single_relation_input(
        self,
        owner_model: type,
        spec: RelationSpec,
        child_project: Any,
    ) -> type:
        cache_key = (owner_model, spec.name, self._project_signature(child_project))
        if cache_key in self._single_ops:
            return self._single_ops[cache_key]

        cls = type(
            f"{owner_model.__name__}{spec.name.title()}NodeRelationInput"
            f"{self._project_suffix(child_project)}",
            (),
            {},
        )
        self._single_ops[cache_key] = cls
        policy = (
            RelationPolicy()
            if child_project in (_PROJECT_UNBOUNDED, _PROJECT_SHALLOW, _PROJECT_LEAF)
            else child_project["_meta"]
        )
        cls.__annotations__ = {
            "create": self._create_input(spec.related_model, child_project) | None,
            "update": self._update_input(spec.related_model, child_project) | None,
        }
        cls.create = strawberry.UNSET
        cls.update = strawberry.UNSET
        default_on_replace = (
            policy.on_replace_options
            if policy.on_replace_options is not None
            else ("DISCONNECT", "DELETE")
        )
        cls.__relation_policy__ = {  # type: ignore[attr-defined]
            "on_replace_options": default_on_replace,
            "default_on_replace": self._default_option(
                default_on_replace, "DISCONNECT"
            ),
        }
        if len(default_on_replace) > 1:
            cls.__annotations__["on_replace"] = RelationRemovalPolicy | None
            cls.on_replace = strawberry.field(
                default=strawberry.UNSET,
                name="onReplace",
            )
        result = strawberry.input(cls)
        self._single_ops[cache_key] = result
        return result

    def _list_relation_ref_type(
        self,
        owner_model: type,
        spec: RelationSpec,
        child_project: Any,
    ) -> type:
        cache_key = (owner_model, spec.name, self._project_signature(child_project))
        if cache_key in self._list_ops:
            return self._list_ops[cache_key]

        ref_type = make_ref_type(
            spec.related_model,
            create=self._create_input(spec.related_model, child_project),
            update=self._update_input(spec.related_model, child_project),
            unlink=True,
            delete=True,
            name=(
                f"{owner_model.__name__}{spec.name.title()}Ref"
                f"{self._project_suffix(child_project)}"
            ),
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

    def _select_model_payload(
        self, input_obj: Any, input_type: type
    ) -> tuple[type, Any]:
        values = _input_values(input_obj)
        if len(values) != 1:
            raise ValueError("Exactly one root model must be selected")
        key, payload = next(iter(values.items()))
        model = input_type.__mutation_models__[key]  # type: ignore[attr-defined]
        return model, payload

    def _cast_node(self, model: type, instance: Any) -> relay.Node:
        graphql_type = self._backend._graphql_type_registry.get(model)
        if graphql_type is None or not issubclass(graphql_type, relay.Node):
            raise ValueError(
                f"Model {model.__name__} does not have a registered relay.Node GraphQL type"
            )
        return strawberry_cast(graphql_type, instance)

    def _split_payload(
        self, model: type, payload: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        values = _input_values(payload)
        relations = self._relation_specs(model)
        scalar_values: dict[str, Any] = {}
        relation_values: dict[str, Any] = {}
        for key, value in values.items():
            if key in relations:
                relation_values[key] = value
            else:
                scalar_values[key] = value
        return scalar_values, relation_values

    def _resolve_single_wrapper(self, wrapper: Any) -> tuple[str, Any, str]:
        values = _input_values(wrapper)
        policy = wrapper.__class__.__relation_policy__
        on_replace = values.pop("on_replace", strawberry.UNSET)
        if on_replace is strawberry.UNSET or on_replace is None:
            on_replace_value = policy["default_on_replace"]
        else:
            on_replace_value = on_replace.value
        selected = [
            (name, value)
            for name, value in values.items()
            if name in {"create", "update"}
        ]
        if len(selected) != 1:
            raise ValueError(
                "Single relation inputs require exactly one of create or update"
            )
        operation, payload = selected[0]
        return operation, payload, on_replace_value

    def _create_sync(
        self,
        model: type,
        payload: Any,
        info: Any,
        parent_link: tuple[str, Any] | None = None,
    ) -> Any:
        scalar_values, relation_values = self._split_payload(model, payload)
        relation_specs = self._relation_specs(model)

        if parent_link is not None:
            scalar_values[parent_link[0]] = parent_link[1]

        for field_name, wrapper in list(relation_values.items()):
            spec = relation_specs[field_name]
            if spec.kind == "single" and spec.relation_mode == "forward_fk":
                operation, nested_payload, _delete_previous = (
                    self._resolve_single_wrapper(wrapper)
                )
                related = (
                    self._create_sync(spec.related_model, nested_payload, info)
                    if operation == "create"
                    else self._update_sync(spec.related_model, nested_payload, info)
                )
                scalar_values[field_name] = related
                relation_values.pop(field_name)

        instance = _sync_create_instance(self._backend, model, scalar_values, info)

        for field_name, wrapper in relation_values.items():
            spec = relation_specs[field_name]
            if spec.kind == "single":
                self._apply_single_sync(instance, spec, wrapper, info)
            else:
                self._apply_many_sync(instance, spec, wrapper, info)

        _sync_save_instance(self._backend, instance, info)
        return instance

    def _update_sync(
        self,
        model: type,
        payload: Any,
        info: Any,
        parent_link: tuple[str, Any] | None = None,
    ) -> Any:
        scalar_values, relation_values = self._split_payload(model, payload)
        raw_id = scalar_values.pop("id", None)
        instance = _sync_load_instance(self._backend, model, raw_id, info)
        if instance is None:
            raise ValueError(f"{model.__name__} with id={raw_id} does not exist")

        if parent_link is not None:
            setattr(instance, parent_link[0], parent_link[1])

        for key, value in scalar_values.items():
            setattr(instance, key, value)

        relation_specs = self._relation_specs(model)
        for field_name, wrapper in relation_values.items():
            spec = relation_specs[field_name]
            if spec.kind == "single":
                self._apply_single_sync(instance, spec, wrapper, info)
            else:
                self._apply_many_sync(instance, spec, wrapper, info)

        _sync_save_instance(self._backend, instance, info)
        return instance

    async def _create_async(
        self,
        model: type,
        payload: Any,
        info: Any,
        parent_link: tuple[str, Any] | None = None,
    ) -> Any:
        scalar_values, relation_values = self._split_payload(model, payload)
        relation_specs = self._relation_specs(model)

        if parent_link is not None:
            scalar_values[parent_link[0]] = parent_link[1]

        for field_name, wrapper in list(relation_values.items()):
            spec = relation_specs[field_name]
            if spec.kind == "single" and spec.relation_mode == "forward_fk":
                operation, nested_payload, _delete_previous = (
                    self._resolve_single_wrapper(wrapper)
                )
                related = (
                    await self._create_async(spec.related_model, nested_payload, info)
                    if operation == "create"
                    else await self._update_async(
                        spec.related_model, nested_payload, info
                    )
                )
                scalar_values[field_name] = related
                relation_values.pop(field_name)

        instance = await _async_create_instance(
            self._backend, model, scalar_values, info
        )

        for field_name, wrapper in relation_values.items():
            spec = relation_specs[field_name]
            if spec.kind == "single":
                await self._apply_single_async(instance, spec, wrapper, info)
            else:
                await self._apply_many_async(instance, spec, wrapper, info)

        await _async_save_instance(self._backend, instance, info)
        return instance

    async def _update_async(
        self,
        model: type,
        payload: Any,
        info: Any,
        parent_link: tuple[str, Any] | None = None,
    ) -> Any:
        scalar_values, relation_values = self._split_payload(model, payload)
        raw_id = scalar_values.pop("id", None)
        instance = await _async_load_instance(self._backend, model, raw_id, info)
        if instance is None:
            raise ValueError(f"{model.__name__} with id={raw_id} does not exist")

        if parent_link is not None:
            setattr(instance, parent_link[0], parent_link[1])

        for key, value in scalar_values.items():
            setattr(instance, key, value)

        relation_specs = self._relation_specs(model)
        for field_name, wrapper in relation_values.items():
            spec = relation_specs[field_name]
            if spec.kind == "single":
                await self._apply_single_async(instance, spec, wrapper, info)
            else:
                await self._apply_many_async(instance, spec, wrapper, info)

        await _async_save_instance(self._backend, instance, info)
        return instance

    def _apply_single_sync(
        self, instance: Any, spec: RelationSpec, wrapper: Any, info: Any
    ) -> None:
        operation, payload, on_replace = self._resolve_single_wrapper(wrapper)
        current = getattr(instance, spec.name, None)
        related = (
            self._create_sync(spec.related_model, payload, info)
            if operation == "create"
            else self._update_sync(spec.related_model, payload, info)
        )
        setattr(instance, spec.name, related)
        _sync_save_instance(self._backend, instance, info)
        if (
            on_replace == "DELETE"
            and current is not None
            and _primary_key_value(current) != _primary_key_value(related)
        ):
            _sync_delete_instance(self._backend, current, info)

    async def _apply_single_async(
        self, instance: Any, spec: RelationSpec, wrapper: Any, info: Any
    ) -> None:
        operation, payload, on_replace = self._resolve_single_wrapper(wrapper)
        current = getattr(instance, spec.name, None)
        related = (
            await self._create_async(spec.related_model, payload, info)
            if operation == "create"
            else await self._update_async(spec.related_model, payload, info)
        )
        setattr(instance, spec.name, related)
        await _async_save_instance(self._backend, instance, info)
        if (
            on_replace == "DELETE"
            and current is not None
            and _primary_key_value(current) != _primary_key_value(related)
        ):
            await _async_delete_instance(self._backend, current, info)

    def _apply_many_sync(
        self, instance: Any, spec: RelationSpec, refs: list[Any], info: Any
    ) -> None:
        if spec.relation_mode == "many_to_many":
            self._backend.apply_ref_list(instance, spec.name, refs, info)
            return
        self._apply_reverse_many_sync(instance, spec, refs, info)

    async def _apply_many_async(
        self, instance: Any, spec: RelationSpec, refs: list[Any], info: Any
    ) -> None:
        if spec.relation_mode == "many_to_many":
            await self._backend.apply_ref_list(instance, spec.name, refs, info)
            return
        await self._apply_reverse_many_async(instance, spec, refs, info)

    def _apply_reverse_many_sync(
        self,
        instance: Any,
        spec: RelationSpec,
        refs: list[Any],
        info: Any,
    ) -> None:
        for ref in refs:
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_unlink = getattr(ref, "unlink", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_create is not strawberry.UNSET and ref_create is not None:
                self._create_sync(
                    spec.related_model,
                    ref_create,
                    info,
                    parent_link=(spec.remote_attr, instance),
                )
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                update_values = _input_values(ref_update)
                raw_id = update_values.get("id")
                child = _sync_load_instance(
                    self._backend, spec.related_model, raw_id, info
                )
                if child is None:
                    continue
                self._update_sync(
                    spec.related_model,
                    ref_update,
                    info,
                    parent_link=(spec.remote_attr, instance),
                )
            elif ref_unlink is not strawberry.UNSET and ref_unlink is not None:
                child = _sync_load_instance(
                    self._backend, spec.related_model, ref_unlink.id, info
                )
                if child is not None:
                    self._detach_reverse_sync(child, spec, info)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                child = _sync_load_instance(
                    self._backend, spec.related_model, ref_delete.id, info
                )
                if child is not None:
                    _sync_delete_instance(self._backend, child, info)
                    if self._backend.__class__.__name__ == "SQLAlchemyBackend":
                        related = getattr(instance, spec.name, None)
                        if isinstance(related, list) and child in related:
                            related.remove(child)

    async def _apply_reverse_many_async(
        self,
        instance: Any,
        spec: RelationSpec,
        refs: list[Any],
        info: Any,
    ) -> None:
        for ref in refs:
            ref_create = getattr(ref, "create", strawberry.UNSET)
            ref_update = getattr(ref, "update", strawberry.UNSET)
            ref_unlink = getattr(ref, "unlink", strawberry.UNSET)
            ref_delete = getattr(ref, "delete", strawberry.UNSET)

            if ref_create is not strawberry.UNSET and ref_create is not None:
                await self._create_async(
                    spec.related_model,
                    ref_create,
                    info,
                    parent_link=(spec.remote_attr, instance),
                )
            elif ref_update is not strawberry.UNSET and ref_update is not None:
                update_values = _input_values(ref_update)
                raw_id = update_values.get("id")
                child = await _async_load_instance(
                    self._backend, spec.related_model, raw_id, info
                )
                if child is None:
                    continue
                await self._update_async(
                    spec.related_model,
                    ref_update,
                    info,
                    parent_link=(spec.remote_attr, instance),
                )
            elif ref_unlink is not strawberry.UNSET and ref_unlink is not None:
                child = await _async_load_instance(
                    self._backend, spec.related_model, ref_unlink.id, info
                )
                if child is not None:
                    await self._detach_reverse_async(child, spec, info)
            elif ref_delete is not strawberry.UNSET and ref_delete is not None:
                child = await _async_load_instance(
                    self._backend, spec.related_model, ref_delete.id, info
                )
                if child is not None:
                    await _async_delete_instance(self._backend, child, info)

    def _detach_reverse_sync(self, child: Any, spec: RelationSpec, info: Any) -> None:
        if not spec.nullable:
            raise ValueError(
                f"Cannot detach non-nullable relation '{spec.name}' without delete_previous=True"
            )
        setattr(child, spec.remote_attr, None)
        _sync_save_instance(self._backend, child, info)

    async def _detach_reverse_async(
        self, child: Any, spec: RelationSpec, info: Any
    ) -> None:
        if not spec.nullable:
            raise ValueError(
                f"Cannot detach non-nullable relation '{spec.name}' without delete_previous=True"
            )
        setattr(child, spec.remote_attr, None)
        await _async_save_instance(self._backend, child, info)


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


def _sync_create_instance(
    backend: Any, model: type, values: dict[str, Any], info: Any
) -> Any:
    if backend.__class__.__name__ == "DjangoBackend":
        return model.objects.create(**values)

    session = backend._get_session(info)
    instance = model(**values)
    session.add(instance)
    session.flush()
    return instance


def _sync_load_instance(backend: Any, model: type, raw_id: Any, info: Any) -> Any:
    if backend.__class__.__name__ == "DjangoBackend":
        try:
            return model.objects.get(pk=raw_id)
        except model.DoesNotExist:
            return None

    session = backend._get_session(info)
    return session.get(model, int(raw_id))


def _sync_save_instance(backend: Any, instance: Any, info: Any) -> None:
    if backend.__class__.__name__ == "DjangoBackend":
        instance.save()
        return

    session = backend._get_session(info)
    from sqlalchemy.orm import object_session

    if object_session(instance) is None:
        session.add(instance)
    session.flush()


def _sync_delete_instance(backend: Any, instance: Any, info: Any) -> None:
    if backend.__class__.__name__ == "DjangoBackend":
        instance.delete()
        return

    session = backend._get_session(info)
    session.delete(instance)
    session.flush()


def _sync_get_many_related(
    backend: Any, instance: Any, field: str, info: Any
) -> list[Any]:
    if backend.__class__.__name__ == "DjangoBackend":
        return list(getattr(instance, field).all())
    return list(getattr(instance, field))


async def _async_create_instance(
    backend: Any, model: type, values: dict[str, Any], info: Any
) -> Any:
    return await model.create(**values)


async def _async_load_instance(
    backend: Any, model: type, raw_id: Any, info: Any
) -> Any:
    return await model.get_or_none(pk=int(raw_id))


async def _async_save_instance(backend: Any, instance: Any, info: Any) -> None:
    await instance.save()


async def _async_delete_instance(backend: Any, instance: Any, info: Any) -> None:
    await instance.delete()


async def _async_get_many_related(
    backend: Any, instance: Any, field: str, info: Any
) -> list[Any]:
    return list(await getattr(instance, field).all())
