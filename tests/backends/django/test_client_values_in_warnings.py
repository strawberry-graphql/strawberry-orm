from django.test import override_settings
from django.utils import timezone

from tests.abstract.client_values_in_warnings import (
    AbstractTestClientValuesInWarnings,
)
from tests.backends.django.test_write_path_authorization import writes  # noqa: F401


class TestClientValuesInWarnings(AbstractTestClientValuesInWarnings):
    @staticmethod
    def time_zones():
        return override_settings(USE_TZ=True)

    @staticmethod
    def stored(stamp):
        return timezone.make_aware(stamp, timezone.get_default_timezone())
