"""Models joined one-to-one, on an app of their own."""

from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class Member(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)

    profile: fields.BackwardOneToOneRelation[Profile]

    class Meta:
        table = "one_to_one_member"


class Profile(Model):
    id = fields.IntField(primary_key=True)
    bio = fields.CharField(max_length=200)
    is_public = fields.BooleanField(default=True)
    member: fields.OneToOneRelation[Member] = fields.OneToOneField(
        "one_to_one.Member", related_name="profile"
    )

    class Meta:
        table = "one_to_one_profile"
