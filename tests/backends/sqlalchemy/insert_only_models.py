"""Models for the insert-only upsert tests, on their own metadata."""

from __future__ import annotations

import datetime

from sqlalchemy import Column, Date, ForeignKey, String, Table
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class InsertOnlyBase(DeclarativeBase):
    pass


group_member_table = Table(
    "group_member",
    InsertOnlyBase.metadata,
    Column("group_id", ForeignKey("group.id"), primary_key=True),
    Column("person_id", ForeignKey("person.id"), primary_key=True),
)


class Person(InsertOnlyBase):
    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    ssn: Mapped[str] = mapped_column(String(11))
    dob: Mapped[datetime.date] = mapped_column(Date)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    addresses: Mapped[list[Address]] = relationship(back_populates="person")
    events: Mapped[list[Event]] = relationship(back_populates="person")
    groups: Mapped[list[Group]] = relationship(
        secondary=group_member_table, back_populates="members"
    )
    # No back_populates: a nested note is tied to its person by the column.
    notes: Mapped[list[Note]] = relationship()


class Address(InsertOnlyBase):
    __tablename__ = "address"

    id: Mapped[int] = mapped_column(primary_key=True)
    line1: Mapped[str] = mapped_column(String(200))
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"))

    person: Mapped[Person] = relationship(back_populates="addresses")


class Event(InsertOnlyBase):
    __tablename__ = "event"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(50))
    occurred_on: Mapped[datetime.date] = mapped_column(Date)
    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"))

    person: Mapped[Person] = relationship(back_populates="events")


class Group(InsertOnlyBase):
    __tablename__ = "group"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)

    members: Mapped[list[Person]] = relationship(
        secondary=group_member_table, back_populates="groups"
    )


class Note(InsertOnlyBase):
    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(200))
    person_id: Mapped[int | None] = mapped_column(
        ForeignKey("person.id"), nullable=True
    )
