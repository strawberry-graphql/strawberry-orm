"""A column read straight off the row still goes through a property put over it."""

import pytest
import strawberry
from django.db.models.query_utils import DeferredAttribute

from strawberry_orm import StrawberryORM
from strawberry_orm.backends.django import _column_in_memory
from strawberry_orm.types import auto
from tests.backends.django.models import User


def _store_email(user, value):
    user.__dict__["email"] = value


class MaskedUser(User):
    email = property(lambda user: "hidden", _store_email)

    class Meta:
        proxy = True
        app_label = "testapp"


class _Shouting(DeferredAttribute):
    def __get__(self, instance, cls=None):
        value = super().__get__(instance, cls)
        return value if instance is None else value.upper()

    def __set__(self, instance, value):
        instance.__dict__[self.field.attname] = value


class ShoutingUser(User):
    email = _Shouting(User._meta.get_field("email"))

    class Meta:
        proxy = True
        app_label = "testapp"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("rows", "declared_on"),
    [
        (MaskedUser, User),
        (MaskedUser, MaskedUser),
        (ShoutingUser, User),
    ],
    ids=["property-model", "property-proxy", "descriptor-model"],
)
def test_what_the_row_class_puts_over_a_column_is_what_the_field_returns(
    seed, rows, declared_on
):
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(declared_on)
    class UserType:
        id: auto
        email: auto

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self) -> list[UserType]:
            return rows.objects.order_by("id")

    result = orm.schema(query=Query).execute_sync(
        "{ users { email } }", context_value={}
    )

    assert result.errors is None, result.errors
    expected = [row.email for row in rows.objects.order_by("id")]
    assert [user["email"] for user in result.data["users"]] == expected
    assert expected != list(User.objects.order_by("id").values_list("email", flat=True))


@pytest.mark.django_db
def test_a_column_is_in_memory_only_where_the_row_reads_it_plainly(seed):
    in_memory = _column_in_memory(User, "email")

    assert in_memory(User.objects.order_by("id").first())
    assert not in_memory(User.objects.only("name").order_by("id").first())
    assert not in_memory(MaskedUser.objects.order_by("id").first())
