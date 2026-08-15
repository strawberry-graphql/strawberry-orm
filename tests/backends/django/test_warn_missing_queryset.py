"""Tests for missing scope_rows warnings on @orm.type registration."""

import warnings

import pytest

from strawberry_orm.types import auto


class TestWarnMissingQueryset:
    def test_warns_when_scope_rows_missing(self, User):
        from strawberry_orm import StrawberryORM

        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=True,
        )

        with pytest.warns(
            UserWarning,
            match=r"GraphQL type 'UserType' \(model User\) has no scope_rows",
        ):

            @orm.type(User)
            class UserType:
                id: auto
                name: auto

    def test_no_warn_when_scope_rows_defined(self, User):
        from strawberry_orm import StrawberryORM

        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=True,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error", category=UserWarning)

            @orm.type(User)
            class UserTypeWithQs:
                id: auto
                name: auto

                @classmethod
                def scope_rows(cls, qs, info):
                    return qs.filter(is_active=True)

    def test_disabled_when_warn_missing_scope_false(self, User):
        from strawberry_orm import StrawberryORM

        orm = StrawberryORM.for_django(
            lazy_resolution="off",
            warn_missing_scope=False,
        )

        with warnings.catch_warnings():
            warnings.simplefilter("error", category=UserWarning)

            @orm.type(User)
            class UserTypeNoWarn:
                id: auto
                name: auto

    def test_disabled_under_pytest_by_default(self, orm):
        assert orm._backend._warn_missing_scope is False
