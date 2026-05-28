"""SQLAlchemy-specific exact branch coverage."""

from dataclasses import dataclass
from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import Boolean, Integer, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from strawberry_orm import Ordering, StrawberryORM
from strawberry_orm.backends._base import AggregateMeta
from strawberry_orm.backends.sqlalchemy import (
    SQLAlchemyBackend,
    _build_lookup_clauses,
    _build_sa_ordering,
    _introspect_sa_model,
)
from strawberry_orm.filters import ReferenceLookup, StringLookup
from tests.backends.sqlalchemy.models import Base as SABase
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser


class Base(DeclarativeBase):
    pass


class FlagModel(Base):
    __tablename__ = "flag_model"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


@pytest_asyncio.fixture
async def async_session():
    pytest.importorskip("greenlet")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(SABase.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def async_seed(async_session):
    alice = SAUser(id=1, name="Alice", email="alice@example.com")
    bob = SAUser(id=2, name="Bob", email="bob@example.com")
    charlie = SAUser(id=3, name="Charlie", email="charlie@test.org")
    python = SATag(id=1, name="python")
    graphql = SATag(id=2, name="graphql")
    rust = SATag(id=3, name="rust")
    p1 = SAPost(
        id=1, title="Hello World", body="First post", is_published=True, author=alice
    )
    p2 = SAPost(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author=alice,
    )
    p3 = SAPost(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author=bob,
    )
    p4 = SAPost(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author=charlie,
    )
    p1.tags.append(python)
    p2.tags.extend([python, graphql])
    p4.tags.append(rust)
    async_session.add_all([alice, bob, charlie, python, graphql, rust, p1, p2, p3, p4])
    await async_session.commit()
    return {"post": p1, "tag_python": python}


@strawberry.type
class _AsyncAggregates:
    count: int


@strawberry.type
class _AsyncGroupKey:
    author_id: str | None = None


@strawberry.input
class _AsyncAuthorGroupField:
    author_id: bool | None = True


@strawberry.input
class _AsyncAuthorGroupBy:
    field: _AsyncAuthorGroupField | None = strawberry.UNSET


@strawberry.input
class _AsyncAuthorOrderField:
    author_id: Ordering | None = Ordering.ASC


@strawberry.input
class _AsyncAuthorOrder:
    field: _AsyncAuthorOrderField | None = strawberry.UNSET


def _async_aggregate_meta(model):
    return AggregateMeta(
        model=model,
        aggregates_type=_AsyncAggregates,
        group_key_type=_AsyncGroupKey,
    )


def _async_info_with_aggregates(session, path: str = "aggregates", **agg_fields):
    selections = []
    if agg_fields.get("count"):
        selections.append(SimpleNamespace(name="count", selections=[]))
    parts = path.split(".")
    node = SimpleNamespace(name=parts[-1], selections=selections)
    for part in reversed(parts[:-1]):
        node = SimpleNamespace(name=part, selections=[node])
    return SimpleNamespace(
        selected_fields=[node],
        context={"session": session},
        field_nodes=[],
    )


@strawberry.input
class RegexLookup:
    i_regex: str | None = strawberry.UNSET


@strawberry.input
class MissingOrder:
    missing: Ordering | None = Ordering.ASC


class TestInternalSqlalchemyExtraCoverage:
    def test_boolean_impl_and_missing_relation_type_paths(self):
        backend = StrawberryORM.for_sqlalchemy(dialect="sqlite").backend
        fields = _introspect_sa_model(FlagModel)
        assert ("enabled", bool, False, None) in fields

        filt = backend.filter(FlagModel)
        order = backend.order(FlagModel)

        @backend.type(FlagModel, filters=filt, order=order)
        class FlagType:
            id: strawberry.auto
            bogus: list["FlagType"]

        assert FlagType is not None

    @pytest.mark.asyncio
    async def test_async_execute_stmt_invalid_expression_is_sanitized(
        self, async_session
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        with pytest.raises(ValueError, match="Invalid filter expression"):
            await backend._execute_stmt_async(
                async_session, text("select * from missing_table")
            )

    def test_ordering_and_regex_helper_edges(self):
        clause = _build_lookup_clauses(
            FlagModel.enabled, RegexLookup(i_regex="x"), enable_regex=True
        )
        assert len(clause) == 1
        assert _build_sa_ordering(MissingOrder(), FlagModel) == ([], [], None)

        text_clauses = _build_lookup_clauses(
            FlagModel.id,
            StringLookup(regex="^1", i_regex="^1"),
            enable_regex=True,
        )
        assert len(text_clauses) == 2

        ref_clauses = _build_lookup_clauses(
            FlagModel.id,
            ReferenceLookup(exact="1", in_list=["1", "2"]),
        )
        assert len(ref_clauses) == 2

    @pytest.mark.asyncio
    async def test_async_aggregation_grouping_and_batch_helpers(
        self, async_session, async_seed
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        meta = _async_aggregate_meta(SAPost)
        query = select(SAPost)

        count_info = _async_info_with_aggregates(async_session, count=True)
        counted = await backend.apply_aggregation(query, count_info, meta)
        assert counted.count == 4

        group_info = _async_info_with_aggregates(
            async_session, path="groups.aggregates", count=True
        )
        groups = await backend.apply_grouping(
            query,
            _AsyncAuthorGroupBy(field=_AsyncAuthorGroupField(author_id=True)),
            group_info,
            meta,
            order_input=_AsyncAuthorOrder(
                field=_AsyncAuthorOrderField(author_id=Ordering.ASC)
            ),
        )
        assert len(groups) == 3

        @dataclass
        class ScopeKey:
            author_id: int | None = 1

        scoped = backend.scope_query_to_group(select(SAPost), ScopeKey())
        assert "author_id" in str(scoped)

        batch_info = SimpleNamespace(context={"session": async_session})
        batched = await backend.batch_group_items(
            select(SAPost),
            ["author_id"],
            batch_info,
            SAPost,
            per_group_limit=1,
        )
        assert ("1",) in batched
        assert len(batched[("1",)]) == 1

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_respects_authorize(
        self, async_session, async_seed
    ):
        backend = SQLAlchemyBackend(dialect="sqlite")
        post = await async_session.get(SAPost, 1)
        assert post is not None
        await async_session.refresh(post, ["tags"])
        before = {tag.name for tag in post.tags}

        @strawberry.input
        class CreateTagInput:
            name: str

        ref = SimpleNamespace(
            create=CreateTagInput(name="blocked-tag"),
            update=strawberry.UNSET,
            unlink=strawberry.UNSET,
            delete=strawberry.UNSET,
        )
        info = SimpleNamespace(context={"session": async_session})
        await backend.apply_ref_list(
            post,
            "tags",
            [ref],
            info,
            authorize=lambda action, model, obj_id, _info: False,
        )
        await async_session.flush()
        await async_session.refresh(post, ["tags"])
        assert {tag.name for tag in post.tags} == before

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_with_repo(self, async_session, async_seed):
        from strawberry_orm import AbstractRepo
        from tests.backends.sqlalchemy.models import Post as SAPost
        from tests.backends.sqlalchemy.models import Tag as SATag

        class TagRepo(AbstractRepo[SATag]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite", repos={SATag: TagRepo})
        post = await async_session.get(SAPost, 1)
        tag = await async_session.get(SATag, 1)
        info = SimpleNamespace(context={"session": async_session})

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        @strawberry.input
        class IdInput:
            id: strawberry.ID

        await backend.apply_ref_list(
            post,
            "tags",
            [
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=UpdateTagInput(id=strawberry.ID("1"), name="renamed"),
                    unlink=strawberry.UNSET,
                    delete=strawberry.UNSET,
                ),
            ],
            info,
        )
        await async_session.refresh(tag)
        assert tag.name == "renamed"

    def test_apply_ref_list_sync_with_repo(self, sa_session, seed):
        from strawberry_orm import AbstractRepo
        from tests.backends.sqlalchemy.models import Post as SAPost
        from tests.backends.sqlalchemy.models import Tag as SATag

        class TagRepo(AbstractRepo[SATag]):
            pass

        backend = SQLAlchemyBackend(dialect="sqlite", repos={SATag: TagRepo})
        post = sa_session.get(SAPost, 1)
        tag = sa_session.get(SATag, 1)
        info = SimpleNamespace(context={"session": sa_session})

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        @strawberry.input
        class IdInput:
            id: strawberry.ID

        backend.apply_ref_list(
            post,
            "tags",
            [
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=UpdateTagInput(id=strawberry.ID("1"), name="sync-renamed"),
                    unlink=strawberry.UNSET,
                    delete=strawberry.UNSET,
                ),
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=IdInput(id=strawberry.ID("2")),
                    delete=strawberry.UNSET,
                ),
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=strawberry.UNSET,
                    delete=IdInput(id=strawberry.ID("3")),
                ),
            ],
            info,
        )
        sa_session.flush()
        sa_session.refresh(tag)
        assert tag.name == "sync-renamed"
        assert sa_session.get(SATag, 3) is None

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_unlink_without_repo(
        self, async_session, async_seed
    ):
        from tests.backends.sqlalchemy.models import Post as SAPost

        backend = SQLAlchemyBackend(dialect="sqlite")
        post = await async_session.get(SAPost, 1)
        await async_session.refresh(post, ["tags"])
        tag_id = post.tags[0].id
        info = SimpleNamespace(context={"session": async_session})

        @strawberry.input
        class UnlinkInput:
            id: strawberry.ID

        await backend.apply_ref_list(
            post,
            "tags",
            [
                SimpleNamespace(
                    create=strawberry.UNSET,
                    update=strawberry.UNSET,
                    unlink=UnlinkInput(id=strawberry.ID(str(tag_id))),
                    delete=strawberry.UNSET,
                )
            ],
            info,
        )
        await async_session.flush()
        await async_session.refresh(post, ["tags"])
        assert tag_id not in {tag.id for tag in post.tags}
