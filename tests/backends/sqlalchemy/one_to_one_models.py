"""Models joined one-to-one, on their own metadata."""

from __future__ import annotations

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class OneToOneBase(DeclarativeBase):
    pass


class Member(OneToOneBase):
    __tablename__ = "one_to_one_member"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))

    profile: Mapped[Profile | None] = relationship(back_populates="member")


class Profile(OneToOneBase):
    __tablename__ = "one_to_one_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    bio: Mapped[str] = mapped_column(String(200))
    is_public: Mapped[bool] = mapped_column(default=True)
    member_id: Mapped[int] = mapped_column(
        ForeignKey("one_to_one_member.id"), unique=True
    )

    member: Mapped[Member] = relationship(back_populates="profile")
