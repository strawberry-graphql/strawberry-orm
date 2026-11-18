from typing import Any

import pytest
import strawberry
from sqlalchemy import (
    ForeignKey,
    String,
    TypeDecorator,
    bindparam,
    create_engine,
    event,
    select,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    selectinload,
    sessionmaker,
    with_loader_criteria,
)
from sqlalchemy.sql.selectable import Select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_query_state import AbstractTestBatchingQueryState
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import User as SAUser


class Base(DeclarativeBase):
    pass


class WkWriter(Base):
    __tablename__ = "wk_writer"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class WkArticle(Base):
    __tablename__ = "wk_article"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    writer_ref: Mapped[int] = mapped_column("writer_id", ForeignKey("wk_writer.id"))


class WkArticleView(WkArticle):
    """Reads the same rows as its base, as a different class."""


class WkMaskedArticle(Base):
    """Keeps its key under an attribute named apart from the column, which a
    property of the column's name masks."""

    __tablename__ = "wk_masked_article"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    writer_ref: Mapped[int] = mapped_column("writer_id", ForeignKey("wk_writer.id"))

    @property
    def writer_id(self) -> int:
        return 1


class WkArticleTitle(Base):
    """Maps the article table without its key column."""

    __table__ = WkArticle.__table__
    __mapper_args__ = {"include_properties": ["id", "title"]}


class WkSelect(Select):
    inherit_cache = True


class Opaque:
    """A bound value whose repr does not show it."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "Opaque()"


class OpaqueType(TypeDecorator):
    impl = String
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        return str(value.value)


def posts(pk: int) -> Select:
    return select(SAPost).where(SAPost.author_id == pk)


def marked(pk: int, mine: bool, viewer: int) -> Select:
    statement = posts(pk)
    statement.wk_show_drafts = mine
    return statement


BUILDS = {
    "execution_options": lambda pk, mine, viewer: posts(pk).execution_options(
        show_drafts=mine
    ),
    "loader_option": lambda pk, mine, viewer: (
        posts(pk).options(selectinload(SAPost.comments)) if mine else posts(pk)
    ),
    "loader_criteria": lambda pk, mine, viewer: posts(pk).options(
        with_loader_criteria(SAComment, SAComment.author_id == (0 if mine else viewer))
    ),
    "app_attribute": marked,
    "statement_class": lambda pk, mine, viewer: (
        posts(pk) if mine else WkSelect(SAPost).where(SAPost.author_id == pk)
    ),
    "entity": lambda pk, mine, viewer: select(
        WkArticle if mine else WkArticleView
    ).where(WkArticle.writer_ref == pk),
    "opaque_param": lambda pk, mine, viewer: posts(pk).where(
        SAPost.title != bindparam("wk", Opaque(mine), type_=OpaqueType())
    ),
    # Siblings carry the same state.
    "plain": lambda pk, mine, viewer: posts(pk),
    "same_execution_options": lambda pk, mine, viewer: posts(pk).execution_options(
        show_drafts=True
    ),
    "same_loader_option": lambda pk, mine, viewer: posts(pk).options(
        selectinload(SAPost.comments)
    ),
    "same_loader_criteria": lambda pk, mine, viewer: posts(pk).options(
        with_loader_criteria(SAComment, SAComment.author_id == viewer)
    ),
}

STATES = [
    "execution_options",
    "loader_option",
    "loader_criteria",
    "app_attribute",
    "statement_class",
    "entity",
    "opaque_param",
]
SHARED = [
    "plain",
    "same_execution_options",
    "same_loader_option",
    "same_loader_criteria",
]
OBSERVED = ["execution_options", "app_attribute", "plain", "same_execution_options"]


def _shows_drafts(state: Any) -> bool:
    return state.execution_options.get("show_drafts", False) or getattr(
        state.statement, "wk_show_drafts", False
    )


@pytest.fixture
def hide_drafts(sa_session):
    """Hide drafts from a statement unless it asks for them, as an app might."""

    def hook(state):
        if state.is_select and not _shows_drafts(state):
            state.statement = state.statement.options(
                with_loader_criteria(SAPost, SAPost.is_published.is_(True))
            )

    event.listen(sa_session, "do_orm_execute", hook)
    yield
    event.remove(sa_session, "do_orm_execute", hook)


def _schema(build: Any, *, batch: bool) -> strawberry.Schema:
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", batch_relations=batch
    )

    @orm.type(SAPost)
    class PostType:
        title: auto

    @orm.type(SAUser)
    class UserType:
        name: auto

        @orm.field.lazy
        def posts(self, info: strawberry.Info) -> list[PostType]:
            viewer = info.context["viewer"]
            return build(self.id, self.id == viewer, viewer).order_by(SAPost.id)

    @strawberry.type
    class Query:
        @strawberry.field
        def users(self, info: strawberry.Info) -> list[UserType]:
            return select(SAUser).order_by(SAUser.id)

    return orm.schema(query=Query)


@pytest.fixture
def backend():
    return StrawberryORM.for_sqlalchemy(dialect="sqlite")._backend


@pytest.fixture
def read(sa_session, seed, hide_drafts):
    def run(state: str, viewer: int, *, batch: bool) -> Any:
        result = _schema(BUILDS[state], batch=batch).execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session, "viewer": viewer},
        )
        return result.data, sorted(error.message for error in result.errors or [])

    return run


@pytest.fixture(params=STATES)
def state(request):
    return request.param


@pytest.fixture(params=SHARED)
def shared(request):
    return request.param


@pytest.fixture(params=OBSERVED)
def observed(request):
    return request.param


class TestBatchingQueryState(AbstractTestBatchingQueryState):
    builds = BUILDS


@pytest.mark.parametrize("model", [WkArticle, WkMaskedArticle])
def test_a_batch_hands_rows_out_by_the_attribute_holding_the_key(model):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add_all(
        [
            WkWriter(id=1, name="Ann"),
            WkWriter(id=2, name="Ben"),
            model(id=1, title="Ann's", writer_ref=1),
            model(id=2, title="Ben's", writer_ref=2),
        ]
    )
    session.commit()

    def read(batch: bool) -> Any:
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            lazy_resolution="off",
            warn_missing_scope=False,
            batch_relations=batch,
        )

        @orm.type(model)
        class ArticleType:
            title: auto

        @orm.type(WkWriter)
        class WriterType:
            name: auto

            @orm.field.lazy
            def articles(self) -> list[ArticleType]:
                return select(model).where(model.writer_ref == self.id)

        @strawberry.type
        class Query:
            @strawberry.field
            def writers(self) -> list[WriterType]:
                return select(WkWriter).order_by(WkWriter.id)

        result = orm.schema(query=Query).execute_sync(
            "{ writers { name articles { title } } }",
            context_value={"session": session},
        )
        return result.data, [error.message for error in result.errors or []]

    assert (
        read(batch=True)
        == read(batch=False)
        == (
            {
                "writers": [
                    {"name": "Ann", "articles": [{"title": "Ann's"}]},
                    {"name": "Ben", "articles": [{"title": "Ben's"}]},
                ]
            },
            [],
        )
    )
    session.close()


def test_a_key_the_entity_does_not_map_is_not_batched(backend):
    query = select(WkArticleTitle).where(WkArticle.__table__.c.writer_id == 1)
    assert backend.split_parent_predicate(query, 1) is None
