import pytest
from tortoise.exceptions import ValidationError

from strawberry_orm.backends.tortoise import _read_errors
from tests.abstract.write_path_authorization import TagCreate, item, tag_names
from tests.abstract.write_path_key_range import REFUSED, AbstractTestKeysPastAnyColumn
from tests.backends.tortoise.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestKeysPastAnyColumn(AbstractTestKeysPastAnyColumn):
    pass


async def test_an_integer_the_driver_cannot_bind_is_a_refused_read(writes):  # noqa: F811
    refusal = pytest.raises(ValueError, match="Invalid filter expression")
    with refusal as refused, _read_errors():
        await writes.m.Tag.filter(id=2**63)
    assert isinstance(refused.value.__cause__, OverflowError)


async def test_a_value_its_field_refuses_is_a_refused_write(writes):  # noqa: F811
    m = writes.m
    author = await writes.create(m.User, id=1, name="Alice", email="a@example.com")
    post = await writes.create(m.Post, id=1, title="Hello", body="B", author=author)
    orm = writes.new_orm()
    ref = orm.ref(m.Tag, create=TagCreate)
    secret = "x" * 60
    with pytest.raises(ValueError, match=REFUSED) as refused:
        await writes.refs(orm, post, "tags", [item(ref, "create", name=secret)])
    assert isinstance(refused.value.__cause__, ValidationError)
    assert secret not in str(refused.value)
    assert await tag_names(writes) == []
