"""Accounts holding credentials and roles -- equivalent to the SQLAlchemy ones."""

from __future__ import annotations

from tortoise import fields
from tortoise.models import Model


class Role(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)

    accounts: fields.ManyToManyRelation[Account]

    class Meta:
        table = "role"


class Account(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    pinned = fields.BooleanField(default=False)
    password_hash = fields.CharField(max_length=200, default="")
    is_admin = fields.BooleanField(default=False)
    totp_seed = fields.CharField(max_length=64, default="")
    roles: fields.ManyToManyRelation[Role] = fields.ManyToManyField(
        "models.Role", related_name="accounts", through="account_role"
    )

    class Meta:
        table = "account"
