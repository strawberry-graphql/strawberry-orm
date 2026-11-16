"""A subclass row is read by its own type's scope and written by its base's repo.

``select(Document)`` hands back a ``Contract`` for a contract row. A type over
``Contract`` with a ``scope_rows`` of its own decides which contracts a caller
sees, however they were read: through the base, under an interface, along a
relation, or as a row an earlier read of the base loaded. Writes take the repo
of the nearest class that has one, so a subclass row is held to its base's
``scope_query`` and ``can_*`` checks.
"""

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import ForeignKey, String, create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)
from strawberry import relay

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto


class Base(DeclarativeBase):
    pass


class Folder(Base):
    __tablename__ = "folder"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int]
    documents: Mapped[list["Document"]] = relationship(
        order_by="Document.id", overlaps="contracts"
    )
    contracts: Mapped[list["Contract"]] = relationship(
        order_by="Contract.id", overlaps="documents"
    )


class Document(Base):
    __tablename__ = "document"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    owner_id: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))
    archived: Mapped[bool] = mapped_column(default=False)
    folder_id: Mapped[int | None] = mapped_column(ForeignKey("folder.id"))

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "document"}


class Contract(Document):
    """A document its reader must also be a party to."""

    party_id: Mapped[int | None] = mapped_column(nullable=True)
    terms: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __mapper_args__ = {"polymorphic_identity": "contract"}


class Comment(Base):
    __tablename__ = "comment"

    id: Mapped[int] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(String(100))
    document_id: Mapped[int] = mapped_column(ForeignKey("document.id"))
    document: Mapped[Document] = relationship()


ME = 7


def rows():
    return [
        Folder(id=1, owner_id=ME),
        Folder(id=2, owner_id=ME),
        Folder(id=3, owner_id=99),
        Document(id=1, title="handbook", owner_id=ME, folder_id=1),
        Contract(
            id=2,
            title="my contract",
            owner_id=ME,
            folder_id=1,
            party_id=ME,
            terms="mine",
        ),
        Contract(
            id=3,
            title="CEO contract",
            owner_id=ME,
            folder_id=1,
            party_id=99,
            terms="pay",
        ),
        Document(id=4, title="their notes", owner_id=99, folder_id=1),
        Document(id=5, title="minutes", owner_id=ME, folder_id=2),
        Contract(
            id=6,
            title="board contract",
            owner_id=ME,
            folder_id=2,
            party_id=99,
            terms="x",
        ),
        Contract(
            id=7, title="lease", owner_id=ME, folder_id=2, party_id=ME, terms="rent"
        ),
        Contract(
            id=8,
            title="their contract",
            owner_id=99,
            folder_id=3,
            party_id=99,
            terms="y",
        ),
        Comment(id=1, body="on mine", document_id=2),
        Comment(id=2, body="on the CEO's", document_id=3),
    ]


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(rows())
        session.commit()
        session.expunge_all()
        yield session


@pytest_asyncio.fixture
async def async_session():
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        session.add_all(rows())
        await session.commit()
        session.expunge_all()
        yield session
    await engine.dispose()


def mine(stmt, info):
    return stmt.where(Document.owner_id == info.context["me"])


def a_party_to(stmt, info):
    return mine(stmt, info).where(Contract.party_id == info.context["me"])


def build(lazy_resolution="off"):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, lazy_resolution=lazy_resolution
    )

    @strawberry.interface
    class Titled:
        title: str

    @orm.type(Document, filters=orm.filter(Document))
    class DocumentType(Titled):
        id: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return mine(stmt, info)

    @orm.type(Contract)
    class ContractType(Titled):
        id: auto
        terms: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return a_party_to(stmt, info)

    @orm.type(Folder)
    class FolderType:
        id: auto
        documents: list[Titled]

        @orm.field.computed(using=["documents"])
        def terms(self) -> list[str]:
            return [row.terms for row in self.documents if isinstance(row, Contract)]

    @orm.type(Folder, name="FolderRows")
    class FolderRows:
        id: auto
        documents: list[DocumentType]

    @orm.type(Comment)
    class CommentType:
        body: auto
        document: Titled | None

    @strawberry.type
    class Query:
        documents: list[DocumentType] = orm.field.eager()
        folders: list[FolderType] = orm.field.eager()
        folder_rows: list[FolderRows] = orm.field.eager()
        comments: list[CommentType] = orm.field.eager()

        @orm.field.lazy
        def titled(self, info: strawberry.Info) -> list[Titled]:
            return select(Document).order_by(Document.id)

        @strawberry.field
        def contract(self, info: strawberry.Info, id: int) -> ContractType | None:
            return info.context["session"].get(Contract, id)

        @strawberry.field
        def folder_list(self, info: strawberry.Info) -> list[FolderType]:
            statement = select(Folder).order_by(Folder.id)
            return list(info.context["session"].scalars(statement))

    return orm.schema(query=Query, types=[DocumentType, ContractType])


def run(schema, session, query):
    result = schema.execute_sync(query, context_value={"session": session, "me": ME})
    assert result.errors is None, result.errors
    return result.data


TITLED = "{ __typename title }"
HANDBOOK = {"__typename": "DocumentType", "title": "handbook"}
MY_CONTRACT = {"__typename": "ContractType", "title": "my contract"}
MINUTES = {"__typename": "DocumentType", "title": "minutes"}
LEASE = {"__typename": "ContractType", "title": "lease"}
FOLDERS = [
    {"id": 1, "documents": [HANDBOOK, MY_CONTRACT]},
    {"id": 2, "documents": [MINUTES, LEASE]},
    {"id": 3, "documents": []},
]

READS = {
    "{ titled " + TITLED + " }": {"titled": [HANDBOOK, MY_CONTRACT, MINUTES, LEASE]},
    "{ folders { id documents " + TITLED + " } }": {"folders": FOLDERS},
    '{ folderRows { id documents(filter: {field: {title: {contains: "contract"}}})'
    " { title } } }": {
        "folderRows": [
            {"id": 1, "documents": [{"title": "my contract"}]},
            {"id": 2, "documents": []},
            {"id": 3, "documents": []},
        ]
    },
    "{ comments { body document { __typename title } } }": {
        "comments": [
            {
                "body": "on mine",
                "document": {"__typename": "ContractType", "title": "my contract"},
            },
            {"body": "on the CEO's", "document": None},
        ]
    },
}


@pytest.mark.parametrize("lazy_resolution", ["off", "warn"])
@pytest.mark.parametrize("query", list(READS))
def test_a_subclass_row_read_through_its_base_is_scoped_by_its_own_type(
    session, lazy_resolution, query
):
    assert run(build(lazy_resolution), session, query) == READS[query]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", list(READS))
async def test_an_async_read_through_the_base_is_scoped_by_the_rows_own_type(
    async_session, query
):
    result = await build().execute(
        query, context_value={"session": async_session, "me": ME}
    )
    assert result.errors is None, result.errors
    assert result.data == READS[query]


@pytest.mark.parametrize("lazy_resolution", ["off", "warn"])
def test_rows_a_resolver_read_hold_only_the_subclass_rows_their_type_shows(
    session, lazy_resolution
):
    query = "{ folderList { id documents " + TITLED + " } }"
    assert run(build(lazy_resolution), session, query) == {"folderList": FOLDERS}


def test_a_field_using_a_relation_sees_only_the_subclass_rows_their_type_shows(
    session,
):
    assert run(build(), session, "{ folders { id terms } }") == {
        "folders": [
            {"id": 1, "terms": ["mine"]},
            {"id": 2, "terms": ["rent"]},
            {"id": 3, "terms": []},
        ]
    }


def test_a_read_of_the_base_does_not_vouch_for_a_subclass_row_later(session):
    """The session hands ``contract`` the very row ``documents`` read."""
    schema = build()
    query = "{ documents { id } contract(id: %d) { title } }"
    found = [{"id": 1}, {"id": 2}, {"id": 5}, {"id": 7}]
    assert run(schema, session, query % 3) == {"documents": found, "contract": None}
    assert run(schema, session, query % 2) == {
        "documents": found,
        "contract": {"title": "my contract"},
    }


def test_a_relation_with_unflushed_changes_is_read_again_under_the_scope(session):
    """Rewriting the collection would throw away a change the app has not saved."""
    schema = build()
    folder = session.get(Folder, 2)
    folder.documents.append(session.get(Contract, 3))
    with session.no_autoflush:
        data = run(schema, session, "{ folders { id documents { title } } }")
    assert data == {
        "folders": [
            {"id": 1, "documents": [{"title": "handbook"}, {"title": "my contract"}]},
            {"id": 2, "documents": [{"title": "minutes"}, {"title": "lease"}]},
            {"id": 3, "documents": []},
        ]
    }
    assert [row.id for row in folder.documents] == [5, 6, 7, 3]


def test_a_relation_page_drops_the_subclass_rows_their_type_hides(session):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, lazy_resolution="off"
    )

    @orm.type(Document, filters=orm.filter(Document), order=orm.order(Document))
    class DocumentNode(relay.Node):
        id: relay.NodeID[int]
        title: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return mine(stmt, info)

    @orm.type(Contract)
    class ContractType:
        id: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return a_party_to(stmt, info)

    @orm.type(Folder)
    class FolderNode(relay.Node):
        id: relay.NodeID[int]
        documents: ORMListConnection[DocumentNode] = orm.connection.eager()

    @strawberry.type
    class Query:
        folders: list[FolderNode] = orm.field.eager()

    schema = orm.schema(query=Query, types=[ContractType])
    query = "{ folders { documents(first: 5) { edges { node { title } } } } }"

    def page(*titles):
        return {"documents": {"edges": [{"node": {"title": t}} for t in titles]}}

    assert run(schema, session, query) == {
        "folders": [page("handbook", "my contract"), page("minutes", "lease"), page()]
    }


class DocumentRepo(AbstractRepo[Document]):
    """Callers write only their own documents, and deleting one archives it."""

    def scope_query(self, query, info):
        return query.where(Document.owner_id == info.context["me"])

    def _delete(self, instance, info):
        instance.archived = True
        self._save(instance, info)


class KeptDocumentRepo(DocumentRepo):
    def can_delete(self, instance, info):
        return False


def build_writes(repos):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, repos=repos
    )

    @orm.type(Folder)
    class FolderType:
        id: auto

    @strawberry.input
    class ContractUpdate:
        id: strawberry.ID
        title: str | None = strawberry.UNSET

    DocumentRef = orm.ref(Document, delete=True)
    ContractRef = orm.ref(Contract, update=ContractUpdate)

    @strawberry.type
    class Query:
        folders: list[FolderType] = orm.field.eager()

    @strawberry.type
    class Mutation:
        @strawberry.mutation
        def edit_documents(
            self, info: strawberry.Info, documents: list[DocumentRef]
        ) -> bool:
            session = info.context["session"]
            orm.apply_ref_list(session.get(Folder, 1), "documents", documents, info)
            session.commit()
            return True

        @strawberry.mutation
        def edit_contracts(
            self, info: strawberry.Info, contracts: list[ContractRef]
        ) -> bool:
            session = info.context["session"]
            orm.apply_ref_list(session.get(Folder, 1), "contracts", contracts, info)
            session.commit()
            return True

    return orm.schema(query=Query, mutation=Mutation)


def stored(session, *ids):
    session.expunge_all()
    statement = (
        select(Document.id, Document.title, Document.archived, Document.folder_id)
        .where(Document.id.in_(ids))
        .order_by(Document.id)
    )
    return [tuple(row) for row in session.execute(statement)]


def test_a_subclass_row_is_deleted_through_its_base_models_repo(session):
    schema = build_writes({Document: DocumentRepo})
    deleted = 'mutation { editDocuments(documents: [{delete: {id: "2"}}]) }'
    assert run(schema, session, deleted) == {"editDocuments": True}
    assert stored(session, 1, 2) == [
        (1, "handbook", False, 1),
        (2, "my contract", True, 1),
    ]


def test_a_subclass_row_is_kept_by_its_base_models_repo(session):
    schema = build_writes({Document: KeptDocumentRepo})
    deleted = 'mutation { editDocuments(documents: [{delete: {id: "2"}}]) }'
    result = schema.execute_sync(deleted, context_value={"session": session, "me": ME})
    assert result.data is None
    assert [error.message for error in result.errors or ()] == [
        "AbstractRepo.can_delete denied the operation"
    ]
    assert stored(session, 2) == [(2, "my contract", False, 1)]


def test_a_relation_to_a_subclass_is_written_through_its_base_models_repo(session):
    schema = build_writes({Document: DocumentRepo})
    edit = 'mutation { editContracts(contracts: [{update: {id: "%d", title: "%s"}}]) }'
    assert run(schema, session, edit % (2, "renamed")) == {"editContracts": True}
    run(schema, session, edit % (8, "taken"))
    assert stored(session, 2, 8) == [
        (2, "renamed", False, 1),
        (8, "their contract", False, 3),
    ]


def test_a_subclass_takes_the_repo_of_its_nearest_class_with_one():
    class ContractRepo(DocumentRepo):
        pass

    inherited = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", repos={Document: DocumentRepo}
    )._backend
    assert type(inherited.get_repo(Contract)) is DocumentRepo
    assert inherited.get_repo(Comment) is None

    explicit = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", repos={Document: DocumentRepo, Contract: ContractRepo}
    )._backend
    assert type(explicit.get_repo(Contract)) is ContractRepo
    assert type(explicit.get_repo(Document)) is DocumentRepo
