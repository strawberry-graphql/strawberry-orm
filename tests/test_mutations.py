"""Tests for the mutations module (ref types)."""

from __future__ import annotations

import strawberry

from strawberry_orm.mutations import make_ref_type


class TestMakeRefType:
    def test_id_only(self):
        class FakeModel:
            __name__ = "Tag"

        RefType = make_ref_type(FakeModel)
        definition = RefType.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert "id" in field_names
        assert len(field_names) == 1

    def test_with_create(self):
        @strawberry.input
        class CreateTagInput:
            name: str

        class FakeModel:
            __name__ = "Tag"

        RefType = make_ref_type(FakeModel, create=CreateTagInput)
        definition = RefType.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert "id" in field_names
        assert "create" in field_names
        assert len(field_names) == 2

    def test_with_create_and_update(self):
        @strawberry.input
        class CreateInput:
            name: str

        @strawberry.input
        class UpdateInput:
            id: strawberry.ID
            name: str

        class FakeModel:
            __name__ = "Item"

        RefType = make_ref_type(FakeModel, create=CreateInput, update=UpdateInput)
        definition = RefType.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert set(field_names) == {"id", "create", "update"}

    def test_with_delete(self):
        class FakeModel:
            __name__ = "Tag"

        RefType = make_ref_type(FakeModel, delete=True)
        definition = RefType.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert "id" in field_names
        assert "delete" in field_names

    def test_full_variant(self):
        @strawberry.input
        class CreateInput:
            name: str

        @strawberry.input
        class UpdateInput:
            id: strawberry.ID
            name: str

        class FakeModel:
            __name__ = "Widget"

        RefType = make_ref_type(
            FakeModel, create=CreateInput, update=UpdateInput, delete=True
        )
        definition = RefType.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert set(field_names) == {"id", "create", "update", "delete"}

    def test_custom_name(self):
        class FakeModel:
            __name__ = "Tag"

        RefType = make_ref_type(FakeModel, name="MyCustomRef")
        assert RefType.__strawberry_definition__.name == "MyCustomRef"
