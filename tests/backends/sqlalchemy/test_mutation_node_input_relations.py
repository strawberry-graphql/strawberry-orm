from types import SimpleNamespace

import pytest
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from strawberry import relay

from strawberry_orm import StrawberryORM
from strawberry_orm.mutations import _sqlalchemy_relation_specs
from strawberry_orm.types import auto
from tests.abstract.mutation_node_input_relations import (
    AbstractTestExcludedRelations,
    AbstractTestPlainInputKeys,
    AbstractTestSensitiveRelations,
    _schema,
    refused,
    shape,
)
from tests.backends.sqlalchemy.account_models import Account, Role
from tests.backends.sqlalchemy.models import Comment, Post, Tag, User


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


@pytest.fixture
def models():
    return SimpleNamespace(
        User=User, Post=Post, Comment=Comment, Tag=Tag, Account=Account, Role=Role
    )


class TestExcludedRelations(AbstractTestExcludedRelations):
    pass


class TestSensitiveRelations(AbstractTestSensitiveRelations):
    pass


class TestPlainInputKeys(AbstractTestPlainInputKeys):
    pass


class _Renamed(DeclarativeBase):
    pass


class Writer(_Renamed):
    __tablename__ = "renamed_writer"

    id: Mapped[int] = mapped_column(primary_key=True)


class Article(_Renamed):
    __tablename__ = "renamed_article"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    writer_id: Mapped[int] = mapped_column("writer_fk", ForeignKey("renamed_writer.id"))
    writer: Mapped[Writer] = relationship()


class Sheet(_Renamed):
    __tablename__ = "renamed_sheet"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    title: Mapped[str] = mapped_column(String(50))
    notes: Mapped[str] = mapped_column(String(50), default="")

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "sheet"}


class Memo(Sheet):
    __mapper_args__ = {"polymorphic_identity": "memo"}


def hide_notes(orm):
    @orm.type(Sheet, exclude=["notes"])
    class SheetNode(relay.Node):
        id: relay.NodeID[int]
        title: auto


def test_node_inputs_for_a_subclass_leave_out_what_its_bases_type_hides(orm):
    hide_notes(orm)
    schema = _schema(
        create=orm.mutations.create_node_input(models=[Memo], name="Create")
    )
    assert shape(schema, "CreateMemoNodeInput") == {"title": "String"}


def test_an_input_for_a_subclass_naming_what_its_bases_type_hides_is_refused(orm):
    hide_notes(orm)
    with pytest.raises(ValueError, match=r"can write \['notes'\]"):
        orm.input(Memo, name="MemoInput")


def test_a_base_type_hiding_what_an_input_for_a_subclass_writes_is_refused(orm):
    orm.input(Memo, name="MemoInput")
    with pytest.raises(ValueError, match=r"MemoInput can still write"):
        hide_notes(orm)


@pytest.fixture
def writer_hidden(orm):
    """``exclude=`` names the attribute, whatever the database calls its column."""

    @orm.type(Article, exclude=["writer_id"])
    class ArticleNode(relay.Node):
        id: relay.NodeID[int]
        title: auto


def test_a_foreign_keys_spec_names_its_attribute_not_its_column(orm):
    specs = _sqlalchemy_relation_specs(Article, orm.backend._row_keys)
    assert specs["writer"].fk_column == "writer_id"


def test_a_link_whose_renamed_foreign_key_is_excluded_is_left_out(orm, writer_hidden):
    schema = _schema(
        create=orm.mutations.create_node_input(models=[Article], name="Create")
    )
    assert shape(schema, "CreateArticleNodeInput") == {"title": "String"}


def test_a_projection_naming_a_link_whose_renamed_key_is_excluded_is_refused(
    orm, writer_hidden
):
    with pytest.raises(ValueError, match=refused("Article", "writer", ["writer_id"])):
        orm.mutations.upsert_node_input(
            model=Article,
            project={
                "_meta": {"create": ["title"], "upsert": {"where": ["title"]}},
                "writer": {"_meta": {"upsert": {"where": ["id"]}}},
            },
        )
