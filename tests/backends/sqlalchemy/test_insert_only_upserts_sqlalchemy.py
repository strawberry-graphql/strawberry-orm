"""Upsert cases only SQLAlchemy can express.

A relationship without a back-reference ties a nested row to its parent by the
foreign key column alone, and a column type may have no Python type.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import Integer

from strawberry_orm import UpsertValidationError
from tests.abstract.insert_only_upserts import ADA, _collect, address_linked_to
from tests.backends.sqlalchemy.insert_only_harness import ORM
from tests.backends.sqlalchemy.insert_only_models import Note, Person

NOTED_PERSON = {
    "_meta": {
        "name": "NotedPerson",
        "immutable": True,
        "create": ["ssn", "dob"],
        "upsert": {"where": ["ssn", "dob"]},
    },
    "notes": {
        "_meta": {"name": "Note", "immutable": True, "create": ["body", "person_id"]}
    },
}
N = _collect(
    ORM.mutations.upsert_node_input(
        model=Person, project=NOTED_PERSON, name="NotedPersonInput"
    )
)


def _noted_person(**note: Any) -> Any:
    item = N["NotedPersonNotesItem"](create=N["NoteCreate"](**note))
    return N["NotedPersonInput"](
        where=N["NotedPersonWhere"](**ADA), create=N["NotedPersonCreate"](notes=[item])
    )


class TestColumnOnlyRelations:
    async def test_a_note_without_a_back_reference_is_tied_by_its_column(self, db):
        await db.apply(_noted_person(body="hi"))
        [note] = await db.rows(Note)
        [person] = await db.rows(Person)
        assert note.person_id == person.id

    async def test_a_nested_item_cannot_set_its_parent_key(self, db):
        with pytest.raises(UpsertValidationError, match="NoteCreate.person_id cannot"):
            await db.apply(_noted_person(body="hi", person_id=2))
        assert db.statements == []


class TestColumnTypes:
    async def test_an_id_column_without_a_python_type_is_passed_through(
        self, db, monkeypatch
    ):
        await db.create(Person, id=1, name="Ada", **ADA)

        def no_python_type(self: Any) -> type:
            raise NotImplementedError

        monkeypatch.setattr(Integer, "python_type", property(no_python_type))
        address = await db.apply(address_linked_to(db, "1"))
        assert address.person_id == 1
