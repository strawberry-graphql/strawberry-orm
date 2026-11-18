"""Generated inputs read the words of a name (SQLAlchemy)."""

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from strawberry_orm import StrawberryORM
from tests.abstract.fb_sensitive_names import AbstractTestSensitiveNameWords


class FbBase(DeclarativeBase):
    pass


class FbVault(FbBase):
    __tablename__ = "fb_vault"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    decryption_key: Mapped[str]
    otpauth_url: Mapped[str]
    smtp_pass: Mapped[str]
    oauth_refresh: Mapped[str]
    master_seed: Mapped[str]
    password_hash: Mapped[str]
    keyboard_layout: Mapped[str]
    tokenizer_name: Mapped[str]
    passenger_count: Mapped[int]


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(FbVault=FbVault)


class TestSensitiveNameWords(AbstractTestSensitiveNameWords):
    pass
