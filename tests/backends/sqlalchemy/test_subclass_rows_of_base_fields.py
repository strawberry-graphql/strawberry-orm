from types import SimpleNamespace

import pytest
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from strawberry_orm import StrawberryORM
from tests.abstract.subclass_rows_of_base_fields import (
    AbstractTestSubclassRowsOfBaseFields,
)


class Base(DeclarativeBase):
    pass


class RaDocument(Base):
    """Read polymorphically: a memo's row comes back as a ``RaMemo``."""

    __tablename__ = "ra_document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    is_published: Mapped[bool] = mapped_column(default=False)
    kind: Mapped[str] = mapped_column(String(20))

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "document"}


class RaMemo(RaDocument):
    __mapper_args__ = {"polymorphic_identity": "memo"}


class RaFile(Base):
    """Not polymorphic: a read of it never hands back a ``RaMemoFile``."""

    __tablename__ = "ra_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    is_published: Mapped[bool] = mapped_column(default=False)


class RaMemoFile(RaFile):
    __tablename__ = "ra_memo_file"

    id: Mapped[int] = mapped_column(ForeignKey("ra_file.id"), primary_key=True)
    note: Mapped[str] = mapped_column(String(100), default="")


SEED = [("public", True), ("secret", True), ("draft", False)]


@pytest.fixture
def orm():
    return StrawberryORM.for_sqlalchemy(
        dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
    )


class _SQLAlchemySubclassRows(AbstractTestSubclassRowsOfBaseFields):
    document_model: type
    memo_model: type

    @pytest.fixture
    def models(self):
        return SimpleNamespace(Document=self.document_model, Memo=self.memo_model)

    @pytest.fixture(autouse=True)
    def _session(self):
        # With the optimizer off, the library's own reads run in a worker thread.
        engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                self.memo_model(id=i, title=title, is_published=is_published)
                for i, (title, is_published) in enumerate(SEED, start=1)
            )
            session.commit()
            session.expunge_all()
            self.session = session
            yield

    async def run(self, schema, query):
        return await schema.execute(query, context_value={"session": self.session})

    def published_only(self, stmt):
        return stmt.where(self.document_model.is_published.is_(True))

    def no_secrets(self, stmt):
        return stmt.where(self.memo_model.title.not_like("secret%"))

    def memos(self):
        return select(self.memo_model).order_by(self.memo_model.id)


class TestPolymorphicRowsOfBaseFields(_SQLAlchemySubclassRows):
    document_model = RaDocument
    memo_model = RaMemo


class TestJoinedRowsOfBaseFields(_SQLAlchemySubclassRows):
    document_model = RaFile
    memo_model = RaMemoFile
