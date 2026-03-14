"""Tests for the StrawberryORM entry point and backend instantiation."""

from __future__ import annotations

import pytest

from strawberry_orm import StrawberryORM


class TestBackendInstantiation:
    def test_sqlalchemy_backend(self):
        orm = StrawberryORM("sqlalchemy", dialect="sqlite")
        assert orm._backend_name == "sqlalchemy"

    def test_tortoise_backend(self):
        orm = StrawberryORM("tortoise")
        assert orm._backend_name == "tortoise"

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="Unknown backend"):
            StrawberryORM("nosql")  # type: ignore[arg-type]
