import pytest

from strawberry_orm.backends.django import _read_errors
from tests.abstract.write_path_key_range import AbstractTestKeysPastAnyColumn
from tests.backends.django.models import Tag
from tests.backends.django.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestKeysPastAnyColumn(AbstractTestKeysPastAnyColumn):
    pass


def test_an_integer_the_driver_cannot_bind_is_a_refused_read(setup_tables):
    refusal = pytest.raises(ValueError, match="Invalid filter expression")
    with refusal as refused, _read_errors():
        list(Tag.objects.filter(pk__in=[1, 2**63]))
    assert isinstance(refused.value.__cause__, OverflowError)
