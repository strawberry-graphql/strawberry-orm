"""Relations Tortoise knows only by their annotations, before ``Tortoise.init``."""

from __future__ import annotations

import pytest
from tortoise import fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM
from strawberry_orm.upserts import _declared_fields


class Shelf(Model):
    """Never initialized, so ``books`` is not in its ``fields_map``."""

    id = fields.IntField(primary_key=True)
    books: fields.ReverseRelation[Book]


class Book(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=100)
    shelf: fields.ForeignKeyRelation[Shelf] = fields.ForeignKeyField(
        "before_init.Shelf", related_name="books"
    )


class Bookend(Model):
    """Names its shelf by class, and has no key column until init."""

    id = fields.IntField(primary_key=True)
    shelf: fields.OneToOneRelation[Shelf] = fields.OneToOneField(
        Shelf, related_name="bookend"
    )


class Misfiled(Model):
    """Names a model this module does not have."""

    id = fields.IntField(primary_key=True)
    cabinet = fields.ForeignKeyField("before_init.Cabinet", related_name="misfiled")


def _parts(upsert):
    return {
        part: set(_declared_fields(_declared_fields(upsert)[part]))
        for part in ("create", "update")
    }


def test_a_reverse_relation_is_offered_to_objects_before_init():
    orm = StrawberryORM.for_tortoise()
    orm.order(Book)
    orm.group(Book)
    order, group = orm.order(Shelf), orm.group(Shelf)
    assert "books" not in Shelf._meta.fields_map
    assert set(order._objects_type.__dataclass_fields__) == {"books"}
    assert set(group._objects_type.__dataclass_fields__) == {"books"}


def test_a_hop_before_init_reads_its_annotation():
    hop = StrawberryORM.for_tortoise()._backend.relation_hop(Shelf, "books")
    assert (hop.model, hop.target, hop.many) == (Shelf, Book, True)


def test_node_inputs_before_init_write_a_foreign_key_through_its_relation():
    upsert = StrawberryORM.for_tortoise().mutations.upsert_node_input(
        model=Book,
        project={
            "_meta": {"create": True, "update": True, "upsert": {"where": ["title"]}},
            "shelf": {},
        },
        name="BookUpsert",
    )
    assert "shelf_id" not in Book._meta.fields_map
    assert _parts(upsert) == {
        "create": {"title", "shelf"},
        "update": {"title", "shelf"},
    }


def test_node_inputs_before_init_write_a_one_to_one_through_its_relation():
    create = StrawberryORM.for_tortoise().mutations.create_node_input(
        models=[Bookend], project={"bookend": {"shelf": {}}}
    )
    assert set(_declared_fields(_declared_fields(create)["bookend"])) == {"shelf"}


def test_node_inputs_refuse_a_relation_whose_model_is_not_found():
    with pytest.raises(ValueError) as refused:
        StrawberryORM.for_tortoise().mutations.create_node_input(models=[Misfiled])
    assert str(refused.value) == (
        f"Misfiled.cabinet names 'before_init.Cabinet', which is not in {__name__} "
        "and is not known until Tortoise.init() has run. Build the node inputs "
        f"for Misfiled after Tortoise.init(), or import that model into {__name__}."
    )
