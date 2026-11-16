"""Accounts holding credentials and roles -- equivalent to the SQLAlchemy ones."""

from django.db import models


class Role(models.Model):
    name = models.CharField(max_length=50)

    class Meta:
        app_label = "testapp"
        db_table = "role"


class Account(models.Model):
    name = models.CharField(max_length=100)
    pinned = models.BooleanField(default=False)
    password_hash = models.CharField(max_length=200, default="")
    is_admin = models.BooleanField(default=False)
    totp_seed = models.CharField(max_length=64, default="")
    roles = models.ManyToManyField(
        Role, related_name="accounts", db_table="account_role"
    )

    class Meta:
        app_label = "testapp"
        db_table = "account"
