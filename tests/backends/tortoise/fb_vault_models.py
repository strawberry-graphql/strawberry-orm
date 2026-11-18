"""A vault of secrets and ordinary columns -- equivalent to the SQLAlchemy one."""

from tortoise import fields
from tortoise.models import Model


class FbVault(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=100)
    decryption_key = fields.CharField(max_length=100)
    otpauth_url = fields.CharField(max_length=200)
    smtp_pass = fields.CharField(max_length=100)
    oauth_refresh = fields.CharField(max_length=200)
    master_seed = fields.CharField(max_length=100)
    password_hash = fields.CharField(max_length=200)
    keyboard_layout = fields.CharField(max_length=20)
    tokenizer_name = fields.CharField(max_length=100)
    passenger_count = fields.IntField(default=0)

    class Meta:
        table = "fb_vault"
