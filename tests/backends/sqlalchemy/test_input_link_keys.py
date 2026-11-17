"""``orm.input()`` and ``orm.partial()`` leave out the columns a link sets.

On SQLAlchemy the link can be declared on either model, or by a ``primaryjoin``
alone, and a joined-inheritance child's key is a foreign key to its own row.
"""

from __future__ import annotations

import pytest
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from strawberry_orm import StrawberryORM
from strawberry_orm.upserts import _declared_fields


class Base(DeclarativeBase):
    pass


class Author(Base):
    __tablename__ = "link_key_author"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    posts: Mapped[list[Post]] = relationship()


class Post(Base):
    __tablename__ = "link_key_post"
    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    author_id: Mapped[int | None] = mapped_column(ForeignKey("link_key_author.id"))


class Remark(Base):
    __tablename__ = "link_key_remark"
    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(50))
    post_ref: Mapped[int | None]
    post: Mapped[Post | None] = relationship(
        primaryjoin="foreign(Remark.post_ref) == Post.id"
    )


class Document(Base):
    __tablename__ = "link_key_document"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "document"}


class Contract(Document):
    __tablename__ = "link_key_contract"
    id: Mapped[int] = mapped_column(
        ForeignKey("link_key_document.id"), primary_key=True
    )
    signer_id: Mapped[int | None] = mapped_column(ForeignKey("link_key_author.id"))
    __mapper_args__ = {"polymorphic_identity": "contract"}


@pytest.fixture(params=["input", "partial"])
def build(request):
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)
    return getattr(orm, request.param)


def test_a_key_linked_only_from_the_other_model_is_left_out(build):
    assert list(_declared_fields(build(Post))) == ["title"]


def test_include_names_it_back(build):
    built = build(Post, include=["title", "author_id"])
    assert list(_declared_fields(built)) == ["title", "author_id"]


def test_a_key_a_join_alone_links_is_left_out(build):
    assert list(_declared_fields(build(Remark))) == ["body"]


def test_a_child_keeps_the_key_of_its_own_row(build):
    assert list(_declared_fields(build(Contract, exclude_pk=False))) == ["id", "kind"]
