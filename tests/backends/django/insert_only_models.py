"""Models for the insert-only upsert tests -- equivalent to the SQLAlchemy ones."""

from django.db import models


class Person(models.Model):
    ssn = models.CharField(max_length=11)
    dob = models.DateField()
    name = models.CharField(max_length=100, null=True)
    groups = models.ManyToManyField(
        "Group", related_name="members", db_table="group_member"
    )

    class Meta:
        app_label = "testapp"
        db_table = "person"


class Address(models.Model):
    line1 = models.CharField(max_length=200)
    person = models.ForeignKey(
        Person, on_delete=models.CASCADE, related_name="addresses"
    )

    class Meta:
        app_label = "testapp"
        db_table = "address"


class Event(models.Model):
    kind = models.CharField(max_length=50)
    occurred_on = models.DateField()
    person = models.ForeignKey(Person, on_delete=models.CASCADE, related_name="events")

    class Meta:
        app_label = "testapp"
        db_table = "event"


class Group(models.Model):
    name = models.CharField(max_length=50, unique=True)

    class Meta:
        app_label = "testapp"
        db_table = "person_group"
