"""Rows of a mapped subclass are scoped by the type of the class they inherit.

``session.get(Document, id)`` hands back a ``Contract`` when the row is one,
and ``select(Contract)`` reads only contracts. Both are documents, so
``DocumentType.scope_rows`` has to apply to them as it does to any document.
"""

import pytest
import strawberry
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "document"}


class Contract(Document):
    """Single-table inheritance: a contract is a document row of another kind."""

    __mapper_args__ = {"polymorphic_identity": "contract"}


class Invoice(Document):
    """Joined inheritance: an invoice has a table of its own as well."""

    __tablename__ = "invoice"

    id: Mapped[int] = mapped_column(ForeignKey("document.id"), primary_key=True)
    amount: Mapped[int]

    __mapper_args__ = {"polymorphic_identity": "invoice"}


ME = 7
MINE = ["my notes", "my contract", "my invoice"]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Document(id=1, title="my notes", owner_id=ME),
                Document(id=2, title="their notes", owner_id=99),
                Contract(id=3, title="my contract", owner_id=ME),
                Contract(id=4, title="their contract", owner_id=99),
                Invoice(id=5, title="my invoice", owner_id=ME, amount=1),
                Invoice(id=6, title="their invoice", owner_id=99, amount=2),
            ]
        )
        session.commit()
        session.expunge_all()
        yield session


def build(**types):
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)

    @orm.type(Document)
    class DocumentType:
        id: auto
        title: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Document.owner_id == info.context["me"])

    @strawberry.type
    class Query:
        documents: list[DocumentType] = orm.field.eager()

        @strawberry.field
        def document(self, info: strawberry.Info, id: int) -> DocumentType | None:
            return info.context["session"].get(Document, id)

        @strawberry.field
        def rows(self, info: strawberry.Info, kind: str) -> list[DocumentType]:
            model = {"contract": Contract, "invoice": Invoice}[kind]
            return list(info.context["session"].scalars(select(model)))

        @orm.field.lazy
        def query(self, info: strawberry.Info, kind: str) -> list[DocumentType]:
            return select({"contract": Contract, "invoice": Invoice}[kind])

    return orm, orm.schema(query=Query)


def run(schema, session, query):
    result = schema.execute_sync(query, context_value={"session": session, "me": ME})
    assert result.errors is None, result.errors
    return result.data


def titles(rows):
    return sorted(row["title"] for row in rows)


def test_a_subclass_row_is_scoped_by_its_base_type(session):
    _orm, schema = build()
    for hidden in (2, 4, 6):
        data = run(schema, session, f"{{ document(id: {hidden}) {{ title }} }}")
        assert data == {"document": None}, hidden
    for visible, title in ((3, "my contract"), (5, "my invoice")):
        data = run(schema, session, f"{{ document(id: {visible}) {{ title }} }}")
        assert data == {"document": {"title": title}}


@pytest.mark.parametrize(
    ("kind", "mine"), [("contract", "my contract"), ("invoice", "my invoice")]
)
def test_subclass_rows_and_queries_are_scoped_by_the_base_type(session, kind, mine):
    _orm, schema = build()
    for field in ("rows", "query"):
        data = run(schema, session, f'{{ {field}(kind: "{kind}") {{ title }} }}')
        assert titles(data[field]) == [mine], field


def test_the_base_query_still_reads_every_visible_subclass(session):
    _orm, schema = build()
    data = run(schema, session, "{ documents { title } }")
    assert titles(data["documents"]) == sorted(MINE)


def test_a_subclass_type_with_its_own_scope_keeps_it(session):
    orm, _schema = build()

    @orm.type(Contract)
    class ContractType:
        id: auto
        title: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Contract.title.startswith("their"))

    @strawberry.type
    class Query:
        contracts: list[ContractType] = orm.field.eager()

    data = run(orm.schema(query=Query), session, "{ contracts { title } }")
    assert titles(data["contracts"]) == ["their contract"]


def test_a_subclass_type_without_a_scope_takes_its_bases(session):
    orm, _schema = build()

    @orm.type(Invoice)
    class InvoiceType:
        id: auto
        title: auto
        amount: auto

    @strawberry.type
    class Query:
        invoices: list[InvoiceType] = orm.field.eager()

        @strawberry.field
        def invoice(self, info: strawberry.Info, id: int) -> InvoiceType | None:
            return info.context["session"].get(Invoice, id)

    schema = orm.schema(query=Query)
    data = run(schema, session, "{ invoices { title } }")
    assert titles(data["invoices"]) == ["my invoice"]
    assert run(schema, session, "{ invoice(id: 6) { title } }") == {"invoice": None}
