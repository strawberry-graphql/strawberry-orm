"""Accounts holding credentials and roles, on their own metadata."""

from __future__ import annotations

from sqlalchemy import Column, ForeignKey, String, Table
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class AccountBase(DeclarativeBase):
    pass


account_role_table = Table(
    "account_role",
    AccountBase.metadata,
    Column("account_id", ForeignKey("account.id"), primary_key=True),
    Column("role_id", ForeignKey("role.id"), primary_key=True),
)


class Role(AccountBase):
    __tablename__ = "role"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))

    accounts: Mapped[list[Account]] = relationship(
        secondary=account_role_table, back_populates="roles"
    )


class Account(AccountBase):
    __tablename__ = "account"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    pinned: Mapped[bool] = mapped_column(default=False)
    password_hash: Mapped[str] = mapped_column(String(200), default="")
    is_admin: Mapped[bool] = mapped_column(default=False)
    totp_seed: Mapped[str] = mapped_column(String(64), default="")

    roles: Mapped[list[Role]] = relationship(
        secondary=account_role_table, back_populates="accounts"
    )
