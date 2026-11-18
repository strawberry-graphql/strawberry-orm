"""Generated inputs read the words of a name (Django)."""

from types import SimpleNamespace

import pytest
from django.db import models as django_models

from strawberry_orm import StrawberryORM
from tests.abstract.fb_sensitive_names import AbstractTestSensitiveNameWords


class FbVault(django_models.Model):
    name = django_models.CharField(max_length=100)
    decryption_key = django_models.CharField(max_length=100)
    otpauth_url = django_models.CharField(max_length=200)
    smtp_pass = django_models.CharField(max_length=100)
    oauth_refresh = django_models.CharField(max_length=200)
    master_seed = django_models.CharField(max_length=100)
    password_hash = django_models.CharField(max_length=200)
    keyboard_layout = django_models.CharField(max_length=20)
    tokenizer_name = django_models.CharField(max_length=100)
    passenger_count = django_models.IntegerField(default=0)

    class Meta:
        app_label = "testapp"


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@pytest.fixture
def models():
    return SimpleNamespace(FbVault=FbVault)


class TestSensitiveNameWords(AbstractTestSensitiveNameWords):
    pass
