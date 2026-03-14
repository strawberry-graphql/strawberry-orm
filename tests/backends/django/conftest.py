"""Configure Django and register all Django test fixtures."""

import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": ":memory:",
            }
        },
        INSTALLED_APPS=[
            "django.contrib.contenttypes",
            "django.contrib.auth",
            "tests.backends.django.app.TestAppConfig",
        ],
        DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        SECRET_KEY="test-secret-key-not-for-production",
        USE_TZ=False,
    )
    django.setup()

from tests.backends.django.fixtures import *  # noqa: F401,F403,E402
