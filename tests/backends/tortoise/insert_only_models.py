"""Models for the insert-only upsert tests -- equivalent to the SQLAlchemy ones."""

from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class Person(Model):
    id = fields.IntField(primary_key=True)
    ssn = fields.CharField(max_length=11)
    dob = fields.DateField()
    name = fields.CharField(max_length=100, null=True)
    groups: fields.ManyToManyRelation[Group] = fields.ManyToManyField(
        "models.Group", related_name="members", through="group_member"
    )

    addresses: fields.ReverseRelation[Address]
    events: fields.ReverseRelation[Event]

    class Meta:
        table = "person"


class Address(Model):
    id = fields.IntField(primary_key=True)
    line1 = fields.CharField(max_length=200)
    person = fields.ForeignKeyField("models.Person", related_name="addresses")

    class Meta:
        table = "address"


class Event(Model):
    id = fields.IntField(primary_key=True)
    kind = fields.CharField(max_length=50)
    occurred_on = fields.DateField()
    person = fields.ForeignKeyField("models.Person", related_name="events")

    class Meta:
        table = "event"


class Group(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50, unique=True)

    members: fields.ManyToManyRelation[Person]

    class Meta:
        table = "person_group"
