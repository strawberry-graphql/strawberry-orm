"""``auto`` types a column the database may leave empty as nullable.

Typed non-null, a row with no value fails the whole field with "Cannot return
null for non-nullable field".

A backend provides ``orm`` and ``person_model``: a model whose ``name`` column
is nullable and whose ``ssn`` column is not.
"""

import strawberry

from strawberry_orm.types import auto


class AbstractTestAutoNullability:
    def test_a_nullable_column_is_typed_nullable(self, orm, person_model):
        @orm.type(person_model)
        class PersonType:
            id: auto
            ssn: auto
            name: auto

        @strawberry.type
        class Query:
            people: list[PersonType] = orm.field.auto()

        sdl = str(orm.schema(query=Query))
        assert "  name: String\n" in sdl
        assert "  ssn: String!\n" in sdl
