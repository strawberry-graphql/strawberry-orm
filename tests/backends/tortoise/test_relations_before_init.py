"""Relations Tortoise knows only by their annotations, before ``Tortoise.init``."""

from __future__ import annotations

from tortoise import fields
from tortoise.models import Model

from strawberry_orm import StrawberryORM


class Shelf(Model):
    """Never initialized, so ``books`` is not in its ``fields_map``."""

    id = fields.IntField(primary_key=True)
    books: fields.ReverseRelation[Book]


class Book(Model):
    id = fields.IntField(primary_key=True)
    shelf: fields.ForeignKeyRelation[Shelf] = fields.ForeignKeyField(
        "before_init.Shelf", related_name="books"
    )


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
