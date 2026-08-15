"""Focused shared-module coverage for exact defensive branches."""

import asyncio
from types import SimpleNamespace

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm._async import await_maybe
from strawberry_orm.backends._base import BaseBackend
from strawberry_orm.core import (
    _AutoConnection,
    _AutoField,
    _AutoFilterOrderExtension,
    _extract_connection_node,
    _resolve_orm_metadata,
    _unwrap_optional_annotation,
)
from strawberry_orm.mutations import (
    _PROJECT_LEAF,
    _PROJECT_SHALLOW,
    _PROJECT_UNBOUNDED,
    MutationNamespace,
)
from strawberry_orm.types import FieldDefinition, auto
from tests.backends.sqlalchemy.models import User as SAUser


class DummyBackend(BaseBackend):
    def _introspect_model(self, model: type):
        return [
            ("id", int, False, None),
            ("name", str, False, None),
            ("password_hash", str, False, None),
        ]


class DummyAutoField:
    _orm_auto_field = True


class TestInternalSharedCoverage:
    def test_core_helper_functions_cover_none_and_wrapper_methods(self):
        assert _unwrap_optional_annotation(int | None) is int
        assert _unwrap_optional_annotation(int | str | None) == int | str | None
        assert _extract_connection_node(int) is None
        assert _resolve_orm_metadata(int) == (None, None, None, None, None)
        assert _resolve_orm_metadata(SimpleNamespace()) == (
            None,
            None,
            None,
            None,
            None,
        )
        assert _resolve_orm_metadata(list[type("PlainType", (), {})]) == (
            None,
            None,
            None,
            None,
            None,
        )
        assert asyncio.run(await_maybe("value")) == "value"

        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @strawberry.input
        class NameInput:
            name: str

        assert orm.create(NameInput) is not None
        assert orm.update(NameInput) is not None
        assert orm.delete() is not None

    @pytest.mark.asyncio
    async def test_auto_field_and_extension_defensive_paths(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        ext = _AutoFilterOrderExtension(orm.backend)
        ext._is_configured = True
        ext._configure(SimpleNamespace())
        assert ext._cast_result("value") == "value"
        ext._output_type = str
        assert ext._cast_result("value") == "value"
        assert (
            await ext._resolve_awaitable_result(asyncio.sleep(0, result="value"))
            == "value"
        )

        class NoAnnotationOwner:
            pass

        _AutoField(orm.backend).__set_name__(NoAnnotationOwner, "missing")

        class NonOrmOwner:
            value: int

        _AutoField(orm.backend).__set_name__(NonOrmOwner, "value")

        class NoConnectionOwner:
            pass

        _AutoConnection(orm.backend, None).__set_name__(NoConnectionOwner, "missing")

        class NonConnectionOwner:
            value: int

        _AutoConnection(orm.backend, None).__set_name__(NonConnectionOwner, "value")
        ext._output_type = None
        resolved = ext.resolve(
            lambda source, info, **kwargs: asyncio.sleep(0, result=["value"]),
            None,
            None,
        )
        assert resolved == ["value"]

    def test_base_backend_and_mutation_namespace_validation_errors(self):
        base = BaseBackend(warn_missing_scope=False)
        with pytest.raises(NotImplementedError):
            base._introspect_model(object)

        backend = DummyBackend(warn_missing_scope=False)
        input_type = backend.input(object)
        assert "password_hash" not in input_type.__dataclass_fields__
        assert backend.field(using=["name"]).__class__.__name__ == "FieldDefinition"
        assert backend._type_name_for_model(object) is None

        class Example:
            __annotations__ = {"id": auto, "name": auto, "password_hash": auto}
            hinted = FieldDefinition(description="hinted")
            generated = DummyAutoField()

        type_name = backend._process_type_annotations(
            Example,
            object,
            {"id": int, "name": str, "password_hash": str},
            include=["name"],
            exclude=["password_hash"],
        )
        assert type_name == "Example"

        class ExcludedOnly:
            __annotations__ = {"id": auto, "name": auto}

        # A distinct model: ``object`` already has a generated input above, and
        # excluding a field a live input can still write is now rejected.
        class _OtherModel:
            pass

        backend._process_type_annotations(
            ExcludedOnly,
            _OtherModel,
            {"id": int, "name": str},
            exclude=["name"],
        )

        ns = MutationNamespace(StrawberryORM.for_sqlalchemy(dialect="sqlite").backend)
        with pytest.raises(ValueError, match="must be a string or list"):
            ns._normalize_enum_options(
                1, allowed=("PATCH",), field_name="mode", model_name="Model"
            )  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="cannot be empty"):
            ns._normalize_enum_options(
                [], allowed=("PATCH",), field_name="mode", model_name="Model"
            )
        with pytest.raises(ValueError, match="Invalid _meta.mode"):
            ns._normalize_enum_options(
                ["INVALID"],
                allowed=("PATCH",),
                field_name="mode",
                model_name="Model",
            )
        with pytest.raises(ValueError, match="require at least one registered"):
            ns._resolve_root_models(None)
        assert ns._resolve_root_models([SAUser]) == (SAUser,)
        with pytest.raises(ValueError, match="project must be a dict"):
            ns._normalize_root_project((object,), [])  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unknown root model key"):
            ns._normalize_root_project((object,), {"missing": {}})
        assert (
            ns._normalize_model_project(object, _PROJECT_UNBOUNDED)
            == _PROJECT_UNBOUNDED
        )
        assert ns._normalize_model_project(object, {})["relations"] == {}
        with pytest.raises(ValueError, match="_meta for model User must be a dict"):
            ns._normalize_model_project(SAUser, {"_meta": "bad"})
        with pytest.raises(ValueError, match="Unknown _meta key"):
            ns._normalize_model_project(SAUser, {"_meta": {"bad": "value"}})

        assert ns._child_project(_PROJECT_UNBOUNDED, "anything") == _PROJECT_UNBOUNDED
        assert ns._child_project(_PROJECT_SHALLOW, "anything") == _PROJECT_LEAF
        assert ns._child_project({"relations": {}}, "anything") == _PROJECT_SHALLOW

    def test_node_input_allows_explicit_root_type_names(self):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @strawberry.type
        class UserNode(relay.Node):
            id: strawberry.ID

        orm.backend._graphql_type_registry[SAUser] = UserNode

        create_input = orm.mutations.create_node_input(
            models=[SAUser],
            name="CreateNodeInput",
        )
        update_input = orm.mutations.update_node_input(
            models=[SAUser],
            name="UpdateNodeInput",
        )

        assert create_input.__name__ == "CreateNodeInput"
        assert update_input.__name__ == "UpdateNodeInput"


def _minimal_backend(target=None):
    """Backend that knows nothing about models beyond *target*."""
    from strawberry_orm.backends._base import BaseBackend

    class _MinimalBackend(BaseBackend):
        def __init__(self):
            super().__init__(warn_missing_scope=False)

        def _introspect_model(self, model):  # pragma: no cover - unused
            return []

        def _relation_target_model(self, model, relation):
            return target

    return _MinimalBackend()


class TestRelationScopeLookup:
    """``relation_scope`` decides whether traversal needs restricting."""

    def test_base_relation_target_model_defaults_to_unknown(self):
        from strawberry_orm.backends._base import BaseBackend

        class _NoIntrospection(BaseBackend):
            def __init__(self):
                super().__init__(warn_missing_scope=False)

            def _introspect_model(self, model):  # pragma: no cover - unused
                return []

        assert _NoIntrospection()._relation_target_model(object, "anything") is None

    def test_unknown_relation_needs_no_scoping(self):
        backend = _minimal_backend(target=None)
        assert backend.relation_scope(object, "anything", info=None) is None

    def test_scoped_relation_without_info_fails_closed(self):
        """``scope_rows`` needs info, so traversal must refuse rather than
        quietly drop the restriction."""

        class Related:
            pass

        backend = _minimal_backend(target=Related)
        backend._type_querysets[Related] = lambda qs, info: qs

        with pytest.raises(ValueError, match="Cannot filter through"):
            backend.relation_scope(object, "rel", info=None)

    def test_unscoped_relation_is_left_alone(self):
        class Related:
            pass

        backend = _minimal_backend(target=Related)
        assert backend.relation_scope(object, "rel", info=None) is None


class TestSelectionSetFormatting:
    """The N+1 report quotes the selection that asked for the relation."""

    @staticmethod
    def _selection_set(query: str):
        from graphql import parse

        return parse(query).definitions[0].selection_set

    def test_a_flat_selection_reads_as_field_names(self):
        from strawberry_orm.lazy_resolution import _format_selection_set

        assert _format_selection_set(self._selection_set("{ title body }")) == (
            "title body"
        )

    def test_a_nested_selection_keeps_its_braces(self):
        """Without recursion the report would name a relation and stop."""
        from strawberry_orm.lazy_resolution import _format_selection_set

        formatted = _format_selection_set(
            self._selection_set("{ title tags { name colour } }")
        )
        assert formatted == "title tags { name colour }"

    def test_an_empty_selection_set_is_blank(self):
        from strawberry_orm.lazy_resolution import _format_selection_set

        assert _format_selection_set(None) == ""


class TestForwardReferencedConnectionNode:
    """A module can declare a connection above the type it names."""

    def test_the_connection_type_is_named_from_the_reference(self):
        """``__name__`` does not exist on a reference; the name it holds does."""
        from typing import ForwardRef

        from strawberry_orm.relay.connection import connection_type_for_node

        built = connection_type_for_node(ForwardRef("PostType"))
        assert built.__name__ == "PostTypeConnection"

    def test_a_bare_string_reference_works_too(self):
        from strawberry_orm.relay.connection import connection_type_for_node

        assert connection_type_for_node("PostType").__name__ == "PostTypeConnection"
