import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from tests.abstract.payload import policy
from tests.abstract.rows_read_as_declared_type import (
    AbstractTestRowsReadAsDeclaredType,
)


class Base(DeclarativeBase):
    pass


class DtPolyFolder(Base):
    __tablename__ = "dt_poly_folder"

    id: Mapped[int] = mapped_column(primary_key=True)
    memos: Mapped[list["DtPolyMemo"]] = relationship(viewonly=True)


class DtPolyDocument(Base):
    """Read polymorphically: a memo's row comes back as a ``DtPolyMemo``."""

    __tablename__ = "dt_poly_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    is_published: Mapped[bool] = mapped_column(default=False)
    kind: Mapped[str] = mapped_column(String(20))
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("dt_poly_folder.id"))

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "document"}


class DtPolyMemo(DtPolyDocument):
    __mapper_args__ = {"polymorphic_identity": "memo"}


class DtJoinedFolder(Base):
    __tablename__ = "dt_joined_folder"

    id: Mapped[int] = mapped_column(primary_key=True)
    memos: Mapped[list["DtJoinedMemo"]] = relationship(viewonly=True)


class DtJoinedDocument(Base):
    """Not polymorphic: a read of it never hands back a ``DtJoinedMemo``."""

    __tablename__ = "dt_joined_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    is_published: Mapped[bool] = mapped_column(default=False)


class DtJoinedMemo(DtJoinedDocument):
    __tablename__ = "dt_joined_memo"

    id: Mapped[int] = mapped_column(
        ForeignKey("dt_joined_document.id"), primary_key=True
    )
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("dt_joined_folder.id"))


class DtPlainFolder(Base):
    __tablename__ = "dt_plain_folder"

    id: Mapped[int] = mapped_column(primary_key=True)
    memos: Mapped[list["DtPlainMemo"]] = relationship(viewonly=True)


class DtPlainDocument(Base):
    __tablename__ = "dt_plain_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    is_published: Mapped[bool] = mapped_column(default=False)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("dt_plain_folder.id"))


class DtPlainMemo(DtPlainDocument):
    """Single-table, with no discriminator: every row of the table reads as one."""


SEED = [("public", True), ("secret", True), ("draft", False)]


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite",
        lazy_resolution="off",
        warn_missing_scope=False,
        payload=policy(),
    )


class _SQLAlchemy(AbstractTestRowsReadAsDeclaredType):
    folder_model: type
    document_model: type
    memo_model: type
    documents_read: list[str]
    own_read = ["public"]

    @pytest.fixture
    def models(self):
        return SimpleNamespace(
            Document=self.document_model, Memo=self.memo_model, Folder=self.folder_model
        )

    @pytest.fixture(autouse=True)
    def _session(self):
        # With the optimizer off, the library's own reads run in a worker thread.
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(self.folder_model(id=i) for i in (1, 2))
            session.add_all(
                self.memo_model(id=i, title=title, is_published=published, folder_id=1)
                for i, (title, published) in enumerate(SEED, start=1)
            )
            session.commit()
            session.expunge_all()
            self.session = session
            yield

    async def run(self, schema, query):
        context = {"session": self.session}
        if self.asynchronous:
            return await schema.execute(query, context_value=context)
        # Off the event loop, so the library reads as a sync server does.
        return await asyncio.to_thread(
            schema.execute_sync, query, context_value=context
        )

    def published_only(self, stmt):
        return stmt.where(self.document_model.is_published.is_(True))

    def no_secrets(self, stmt):
        return stmt.where(self.memo_model.title.not_like("secret%"))

    def memos(self):
        return select(self.memo_model).order_by(self.memo_model.id)

    def documents(self):
        return select(self.document_model).order_by(self.document_model.id)

    def memos_of(self, folder):
        return self.memos().where(self.memo_model.folder_id == folder.id)

    def read(self, query):
        return list(self.session.scalars(query))


class _Polymorphic(_SQLAlchemy):
    folder_model = DtPolyFolder
    document_model = DtPolyDocument
    memo_model = DtPolyMemo
    documents_read = ["public"]


class _Joined(_SQLAlchemy):
    folder_model = DtJoinedFolder
    document_model = DtJoinedDocument
    memo_model = DtJoinedMemo
    documents_read = ["public", "secret"]


class _Plain(_SQLAlchemy):
    folder_model = DtPlainFolder
    document_model = DtPlainDocument
    memo_model = DtPlainMemo
    documents_read = ["public", "secret"]


class TestPolymorphicSync(_Polymorphic):
    asynchronous = False


class TestPolymorphicAsync(_Polymorphic):
    asynchronous = True


class TestJoinedSync(_Joined):
    asynchronous = False


class TestJoinedAsync(_Joined):
    asynchronous = True


class TestPlainSync(_Plain):
    asynchronous = False


class TestPlainAsync(_Plain):
    asynchronous = True
