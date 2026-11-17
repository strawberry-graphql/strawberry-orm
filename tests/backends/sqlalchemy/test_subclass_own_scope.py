"""A subclass row is read by its own type's scope and written by its base's repo.

``select(Document)`` hands back a ``Contract`` for a contract row. A type over
``Contract`` with a ``scope_rows`` of its own decides which contracts a caller
sees, however they were read: through the base, under an interface, along a
relation, or as a row an earlier read of the base loaded. Writes take the repo
of the nearest class that has one, so a subclass row is held to its base's
``scope_query`` and ``can_*`` checks.
"""

import warnings
from types import SimpleNamespace

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

from strawberry_orm import AbstractRepo, MutationPolicy, StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from strawberry_orm.upserts import UpsertNotFoundError, _declared_fields


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


def test_ordering_through_a_base_whose_subclass_is_scoped_apart_is_refused(session):
    """A contract read through ``Comment.document`` is scoped by its own type,
    so ordering comments by their document's title would rank hidden ones."""
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)

    @orm.type(Document, order=orm.order(Document))
    class DocumentType:
        id: auto
        title: auto

    @orm.type(Contract)
    class ContractType:
        id: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return a_party_to(stmt, info)

    @orm.type(Comment, order=orm.order(Comment))
    class CommentType:
        body: auto
        document: DocumentType | None

    @strawberry.type
    class Query:
        comments: list[CommentType] = orm.field.eager()

    refusal = (
        r"Cannot order by Comment\.document: Document has subclasses scoped by "
        r"their own types' scope_rows \(Contract\)"
    )
    with pytest.raises(ValueError, match=refusal):
        orm.schema(query=Query, types=[ContractType])

    unchecked = strawberry.Schema(
        query=Query, types=[ContractType], extensions=[orm.optimizer_extension()]
    )
    result = unchecked.execute_sync(
        "{ comments(order: [{object: {document: {field: {title: ASC}}}}]) { body } }",
        context_value={"session": session, "me": ME},
    )
    assert result.data is None
    assert "Cannot order by Comment.document" in result.errors[0].message


def build_counted():
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, lazy_resolution="off"
    )

    @orm.type(Document, filters=orm.filter(Document), group=orm.group(Document))
    class DocumentNode(relay.Node):
        id: relay.NodeID[int]
        title: auto
        kind: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return mine(stmt, info)

    @orm.type(Contract)
    class ContractType:
        id: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return a_party_to(stmt, info)

    @orm.type(Folder, filters=orm.filter(Folder))
    class FolderNode(relay.Node):
        id: relay.NodeID[int]
        documents: ORMListConnection[DocumentNode] = orm.connection.eager()

    @strawberry.type
    class Query:
        documents: ORMListConnection[DocumentNode] = orm.connection.eager()
        folders: list[FolderNode] = orm.field.eager()

    return orm.schema(query=Query, types=[ContractType])


def titled(*titles):
    return [{"key": {"title": title}} for title in titles]


#: Of my documents, contracts 3 and 6 are between others, so they count nowhere.
COUNTED = {
    "{ documents(first: 1) { totalCount pageInfo { hasNextPage } } }": {
        "documents": {"totalCount": 4, "pageInfo": {"hasNextPage": True}}
    },
    "{ documents(first: 2) { edges { node { title } } } }": {
        "documents": {
            "edges": [
                {"node": {"title": "handbook"}},
                {"node": {"title": "my contract"}},
            ]
        }
    },
    "{ documents { aggregates { count } } }": {
        "documents": {"aggregates": {"count": 4}}
    },
    "{ documents(groupBy: [{field: {title: true}}]) { groups { key { title } } } }": {
        "documents": {"groups": titled("handbook", "lease", "minutes", "my contract")}
    },
    "{ documents(groupBy: [{field: {kind: true}}])"
    " { groups { items { edges { node { title } } } } } }": {
        "documents": {
            "groups": [
                {"items": {"edges": [{"node": {"title": t}} for t in titles]}}
                for titles in (["my contract", "lease"], ["handbook", "minutes"])
            ]
        }
    },
    "{ folders { documents(first: 1) { totalCount } } }": {
        "folders": [
            {"documents": {"totalCount": 2}},
            {"documents": {"totalCount": 2}},
            {"documents": {"totalCount": 0}},
        ]
    },
    '{ folders(filter: {object: {documents: {field: {title: {contains: "CEO"}}}}})'
    " { id } }": {"folders": []},
}


@pytest.mark.parametrize("query", list(COUNTED))
def test_counts_groups_and_pages_through_the_base_leave_out_hidden_subclass_rows(
    session, query
):
    assert run(build_counted(), session, query) == COUNTED[query]


class _Layered(DeclarativeBase):
    pass


class Note(_Layered):
    __tablename__ = "note"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str] = mapped_column(String(20))
    level: Mapped[int]
    kind: Mapped[str] = mapped_column(String(20))

    __mapper_args__ = {"polymorphic_on": "kind", "polymorphic_identity": "note"}


class Memo(Note):
    __mapper_args__ = {"polymorphic_identity": "memo"}


class SealedMemo(Memo):
    __mapper_args__ = {"polymorphic_identity": "sealed"}


def test_a_subclass_of_a_scoped_subclass_answers_for_its_own_rows():
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)

    @orm.type(Note)
    class NoteNode(relay.Node):
        id: relay.NodeID[int]
        label: auto

    @orm.type(Memo)
    class MemoType:
        id: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Note.level == 1)

    @orm.type(SealedMemo)
    class SealedType:
        id: auto

        @classmethod
        def scope_rows(cls, stmt, info):
            return stmt.where(Note.level == 2)

    @strawberry.type
    class Query:
        notes: ORMListConnection[NoteNode] = orm.connection.eager()

    engine = create_engine("sqlite://")
    _Layered.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                Note(id=1, label="note", level=0),
                Memo(id=2, label="memo", level=1),
                Memo(id=3, label="raised memo", level=2),
                SealedMemo(id=4, label="sealed", level=2),
                SealedMemo(id=5, label="lowered sealed", level=1),
            ]
        )
        session.commit()
        session.expunge_all()
        data = run(
            orm.schema(query=Query, types=[MemoType, SealedType]),
            session,
            "{ notes(first: 1) { totalCount } all: notes { edges { node { label } } } }",
        )

    assert data == {
        "notes": {"totalCount": 3},
        "all": {
            "edges": [
                {"node": {"label": "note"}},
                {"node": {"label": "memo"}},
                {"node": {"label": "sealed"}},
            ]
        },
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


class PartyContractRepo(DocumentRepo):
    """A contract is its parties' alone: others cannot see, edit or delete it."""

    calls: list[str] = []

    def scope_query(self, query, info):
        party = Contract.party_id == info.context["me"]
        return super().scope_query(query, info).where(party)

    def can_update(self, instance, data, info):
        self.calls.append(f"can_update {instance.id}")
        return True

    def _delete(self, instance, info):
        self.calls.append(f"_delete {instance.id}")
        AbstractRepo._delete(self, instance, info)


@pytest.fixture
def party_calls():
    PartyContractRepo.calls = []
    return PartyContractRepo.calls


OWN_REPOS = {Document: DocumentRepo, Contract: PartyContractRepo}


def test_a_subclass_row_its_own_repo_hides_cannot_be_deleted_through_the_base(
    session, party_calls
):
    schema = build_writes(OWN_REPOS)
    deleted = 'mutation { editDocuments(documents: [{delete: {id: "3"}}]) }'
    result = schema.execute_sync(deleted, context_value={"session": session, "me": ME})
    assert [error.message for error in result.errors or ()] == [
        "No row 3 in FolderType.documents"
    ]
    assert stored(session, 3) == [(3, "CEO contract", False, 1)]
    assert party_calls == []


def test_a_subclass_row_is_deleted_by_its_own_repo_through_the_base(
    session, party_calls
):
    schema = build_writes(OWN_REPOS)
    deleted = 'mutation { editDocuments(documents: [{delete: {id: "2"}}]) }'
    assert run(schema, session, deleted) == {"editDocuments": True}
    assert stored(session, 1, 2) == [(1, "handbook", False, 1)]
    assert party_calls == ["_delete 2"]


def test_an_upsert_through_the_base_is_held_to_the_rows_own_repo(session, party_calls):
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, repos=OWN_REPOS
    )
    upsert = orm.mutations.upsert_node_input(
        model=Document,
        project={
            "_meta": {
                "update": ["title"],
                "upsert": {"where": ["id"], "onMatch": "UPDATE"},
            }
        },
        name="DocumentUpsert",
    )
    where, update = (
        _declared_fields(upsert)["where"],
        _declared_fields(upsert)["update"],
    )
    info = SimpleNamespace(context={"session": session, "me": ME})

    with pytest.raises(UpsertNotFoundError, match="No row matches"):
        orm.apply_upsert(upsert(where=where(id="3"), update=update(title="x")), info)
    orm.apply_upsert(upsert(where=where(id="2"), update=update(title="ours")), info)
    session.commit()

    assert stored(session, 2, 3) == [
        (2, "ours", False, 1),
        (3, "CEO contract", False, 1),
    ]
    assert party_calls == ["can_update 2"]


class PermRepo(AbstractRepo):
    """One repo class for every model, keyed on ``self.model`` as Django's perms are."""

    def scope_query(self, query, info):
        query = query.where(Document.owner_id == info.context["me"])
        if issubclass(self.model, Contract):
            query = query.where(Contract.party_id == info.context["me"])
        return query

    def can_delete(self, instance, info):
        return f"delete_{self.model.__name__.lower()}" in info.context["perms"]


def test_one_repo_class_for_both_models_answers_for_the_rows_own(session):
    schema = build_writes({Document: PermRepo, Contract: PermRepo})
    deleted = 'mutation { editDocuments(documents: [{delete: {id: "%d"}}]) }'

    def delete(pk, *perms):
        return schema.execute_sync(
            deleted % pk,
            context_value={"session": session, "me": ME, "perms": set(perms)},
        )

    hidden = delete(3, "delete_document", "delete_contract")
    assert [error.message for error in hidden.errors] == [
        "No row 3 in FolderType.documents"
    ]
    refused = delete(2, "delete_document")
    assert [error.message for error in refused.errors] == [
        "AbstractRepo.can_delete denied the operation"
    ]
    assert stored(session, 2, 3) == [
        (2, "my contract", False, 1),
        (3, "CEO contract", False, 1),
    ]
    assert delete(2, "delete_document", "delete_contract").errors is None
    assert stored(session, 2) == []


def test_a_policy_is_told_the_rows_own_model(session):
    class NoContractDeletes(MutationPolicy):
        def can_delete(self, model, instance, info):
            return model is not Contract

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", warn_missing_scope=False, policy=NoContractDeletes()
        )
    ref = orm.ref(Document, delete=True)
    delete = _declared_fields(ref)["delete"]
    info = SimpleNamespace(context={"session": session, "me": ME})
    with pytest.raises(PermissionError, match="can_delete denied"):
        orm.apply_ref_list(
            session.get(Folder, 1), "documents", [ref(delete=delete(id="2"))], info
        )
    orm.apply_ref_list(
        session.get(Folder, 1), "documents", [ref(delete=delete(id="1"))], info
    )
    assert stored(session, 1, 2) == [(2, "my contract", False, 1)]


def test_authorize_is_asked_about_a_subclass_row_as_its_own_model(session):
    orm = StrawberryORM.for_sqlalchemy(dialect="sqlite", warn_missing_scope=False)
    seen = []

    def authorize(action, model, pk, info):
        seen.append((action, model.__name__, pk))
        return model is not Contract

    ref = orm.ref(Document, delete=True)
    delete = _declared_fields(ref)["delete"]
    info = SimpleNamespace(context={"session": session, "me": ME})
    orm.apply_ref_list(
        session.get(Folder, 1),
        "documents",
        [ref(delete=delete(id="2"))],
        info,
        authorize=authorize,
    )
    assert seen == [("delete", "Document", 2), ("delete", "Contract", 2)]
    assert stored(session, 2) == [(2, "my contract", False, 1)]


def document_upsert(orm):
    return orm.mutations.upsert_node_input(
        model=Document,
        project={"_meta": {"upsert": {"where": ["title"], "onMatch": "IGNORE"}}},
        name="DocumentByTitle",
    )


def test_an_upsert_match_leaves_out_subclass_rows_their_own_repo_hides(session):
    """Counted, the hidden contract would make a match on its title ambiguous,
    which would tell the caller a hidden row has that title."""
    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, repos=OWN_REPOS
    )
    session.add(Document(id=20, title="CEO contract", owner_id=ME))
    session.flush()
    upsert = document_upsert(orm)
    where = _declared_fields(upsert)["where"]
    info = SimpleNamespace(context={"session": session, "me": ME})
    matched = orm.apply_upsert(upsert(where=where(title="CEO contract")), info)
    assert matched.id == 20


def test_an_override_of_find_for_upsert_is_held_to_scope_query(session):
    class FindsAnyDocument(DocumentRepo):
        def find_for_upsert(self, model, query, info):
            statement = select(Document).where(Document.title == query["title"])
            return info.context["session"].scalars(statement).first()

    orm = StrawberryORM.for_sqlalchemy(
        dialect="sqlite", warn_missing_scope=False, repos={Document: FindsAnyDocument}
    )
    upsert = document_upsert(orm)
    where = _declared_fields(upsert)["where"]
    info = SimpleNamespace(context={"session": session, "me": ME})
    with pytest.raises(UpsertNotFoundError, match="No row matches"):
        orm.apply_upsert(upsert(where=where(title="their notes")), info)
    assert orm.apply_upsert(upsert(where=where(title="handbook")), info).id == 1


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
