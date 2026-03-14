"""Ref type generation tests for the Tortoise backend."""

import pytest
import strawberry


class TestRefType:
    def test_ref_id_only(self, orm):
        class FakeModel:
            __name__ = "Tag"
        TagRef = orm.ref(FakeModel)
        assert hasattr(TagRef, "__strawberry_definition__")

    def test_ref_with_create_and_delete(self, orm):
        @strawberry.input
        class CreateInput:
            name: str

        class FakeModel:
            __name__ = "Tag"

        TagRef = orm.ref(FakeModel, create=CreateInput, delete=True)
        definition = TagRef.__strawberry_definition__
        field_names = [f.name for f in definition.fields]
        assert "id" in field_names
        assert "create" in field_names
        assert "delete" in field_names
