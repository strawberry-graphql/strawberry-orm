"""Minimal Django AppConfig for test models."""

from django.apps import AppConfig


class TestAppConfig(AppConfig):
    name = "tests.backends.django"
    label = "testapp"
    default_auto_field = "django.db.models.BigAutoField"
