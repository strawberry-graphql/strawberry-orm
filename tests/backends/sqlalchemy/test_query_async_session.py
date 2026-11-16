"""AsyncSession-specific tests for the SQLAlchemy backend."""

from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from strawberry import relay

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.relay import ORMListConnection
from strawberry_orm.types import auto
from strawberry_orm.upserts import _declared_fields
from tests.backends.sqlalchemy.fixtures import main_schema
from tests.backends.sqlalchemy.models import (
    Base as SABase,
)
from tests.backends.sqlalchemy.models import (
    Post as SAPost,
)
from tests.backends.sqlalchemy.models import (
    Tag as SATag,
)
from tests.backends.sqlalchemy.models import (
    User as SAUser,
)


@pytest_asyncio.fixture
async def async_session():
    pytest.importorskip("greenlet")

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
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
    return async_session


class TestAsyncSessionExecution:
    @pytest.mark.asyncio
    async def test_async_scope_rows_checks_materialized_rows_and_relations(
        self, async_seed
    ):
        from sqlalchemy import select

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(SAPost)
        class PostType:
            title: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(SAPost.is_published == True)  # noqa: E712

        @orm.type(SAUser)
        class UserType:
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            async def posts(self, info: strawberry.types.Info) -> list[PostType]:
                result = await async_seed.scalars(select(SAPost).order_by(SAPost.id))
                return list(result.all())

            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserType]:
                return select(SAUser).order_by(SAUser.id)  # type: ignore[return-value]

        schema = orm.schema(query=Query)
        listed = await schema.execute(
            "{ posts { title } }",
            context_value={"session": async_seed},
        )
        assert listed.errors is None, listed.errors
        assert [row["title"] for row in listed.data["posts"]] == [
            "Hello World",
            "GraphQL Guide",
            "Rust Adventures",
        ]

        loaded = await schema.execute(
            "{ users { name posts { title } } }",
            context_value={"session": async_seed},
        )
        assert loaded.errors is None, loaded.errors
        by_name = {row["name"]: row["posts"] for row in loaded.data["users"]}
        assert by_name["Bob"] == []

    @pytest.mark.asyncio
    async def test_async_schema_execute_supports_async_session(self, async_seed):
        result = await main_schema.execute(
            "{ users { name posts { title } } }",
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }

    @pytest.mark.asyncio
    async def test_async_session_supports_relation_ordering(self, async_seed):
        result = await main_schema.execute(
            """
            {
                users {
                    name
                    posts(order: [{ field: { title: DESC } }]) {
                        title
                    }
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World"},
                        {"title": "GraphQL Guide"},
                    ],
                },
                {"name": "Bob", "posts": [{"title": "Draft Post"}]},
                {"name": "Charlie", "posts": [{"title": "Rust Adventures"}]},
            ]
        }

    async def test_materialized_rows_are_eager_loaded_on_an_async_session(
        self, async_seed
    ):
        """The automatic pass has to await its loads on an AsyncSession.

        A resolver that returns rows gives the optimizer nothing to add loads
        to, so it re-selects them with the eager load attached - which on an
        async session is a coroutine, not a result.
        """
        from sqlalchemy import select

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            async def users(self, info: strawberry.types.Info) -> list[UserType]:
                stmt = select(SAUser).order_by(SAUser.id)
                rows = await info.context["session"].execute(stmt)
                return list(rows.unique().scalars().all())

        schema = orm.schema(query=Query)
        result = await schema.execute(
            "{ users { name posts { title } } }",
            context_value={"session": async_seed},
        )
        assert result.errors is None, result.errors
        assert [row["name"] for row in result.data["users"]] == [
            "Alice",
            "Bob",
            "Charlie",
        ]
        assert [post["title"] for post in result.data["users"][0]["posts"]] == [
            "Hello World",
            "GraphQL Guide",
        ]

    async def test_a_page_without_the_optimizer_is_read_on_an_async_session(
        self, async_seed
    ):
        """With no optimizer, the field reads its own rows: on an async
        session a coroutine, awaited on the loop the session belongs to."""
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(SAPost, filters=orm.filter(SAPost))
        class PostNode(relay.Node):
            id: relay.NodeID[int]
            title: auto

        @strawberry.type
        class Query:
            posts: ORMListConnection[PostNode] = orm.connection.eager()

        schema = orm.schema(query=Query, optimizer=False)
        result = await schema.execute(
            '{ posts(first: 5, filter: {field: {title: {contains: "o"}}}) '
            "{ edges { node { title } } } }",
            context_value={"session": async_seed},
        )
        assert result.errors is None, result.errors
        assert [e["node"]["title"] for e in result.data["posts"]["edges"]] == [
            "Hello World",
            "Draft Post",
        ]


class TestAsyncSessionMutations:
    @pytest.mark.asyncio
    async def test_apply_ref_list_can_be_awaited_with_async_session(self, async_seed):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        TagRef = orm.ref(SATag)

        @strawberry.type
        class Query:
            @strawberry.field
            def ping(self) -> str:
                return "pong"

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def set_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await info.context["session"].get(SAPost, post_id)
                assert post is not None
                await orm.apply_ref_list(post, "tags", tags, info)
                await info.context["session"].commit()
                await info.context["session"].refresh(post, ["tags"])
                return post.tags  # type: ignore[return-value]

        schema = strawberry.Schema(query=Query, mutation=Mutation)
        result = await schema.execute(
            """
            mutation {
                setPostTags(postId: 3, tags: [{ update: { id: "1" } }, { update: { id: "3" } }]) {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert sorted(result.data["setPostTags"], key=lambda tag: tag["name"]) == [
            {"name": "python"},
            {"name": "rust"},
        ]

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_supports_create_update_and_delete(
        self, async_seed
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @strawberry.input
        class CreateTagInput:
            name: str

        @strawberry.input
        class UpdateTagInput:
            id: strawberry.ID
            name: str | None = strawberry.UNSET

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType]

        TagRef = orm.ref(
            SATag,
            create=CreateTagInput,
            update=UpdateTagInput,
            unlink=True,
            delete=True,
        )

        @strawberry.type
        class Query:
            tags: list[TagType] = orm.field.auto()
            posts: list[PostType] = orm.field.auto()

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def modify_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await info.context["session"].get(SAPost, post_id)
                assert post is not None
                await orm.apply_ref_list(post, "tags", tags, info)
                await info.context["session"].commit()
                await info.context["session"].refresh(post, ["tags"])
                return post.tags  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Query,
            mutation=Mutation,
            extensions=[orm.optimizer_extension()],
        )

        result = await schema.execute(
            """
            mutation {
                modifyPostTags(
                    postId: 2
                    tags: [
                        { update: { id: "1", name: "python-async" } }
                        { create: { name: "fresh-async" } }
                        { delete: { id: "2" } }
                    ]
                ) {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert sorted(result.data["modifyPostTags"], key=lambda tag: tag["name"]) == [
            {"name": "fresh-async"},
            {"name": "python-async"},
        ]

        query_result = await schema.execute(
            """
            {
                tags {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert query_result.errors is None
        all_tag_names = sorted(tag["name"] for tag in query_result.data["tags"])
        assert "graphql" not in all_tag_names
        assert "fresh-async" in all_tag_names
        assert "python-async" in all_tag_names

    @pytest.mark.asyncio
    async def test_apply_ref_list_async_supports_delete_and_create(self, async_seed):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @strawberry.input
        class CreateTagInput:
            name: str

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType]

        TagRef = orm.ref(SATag, create=CreateTagInput, delete=True)

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.auto()
            tags: list[TagType] = orm.field.auto()

        @strawberry.type
        class Mutation:
            @strawberry.mutation
            async def modify_post_tags(
                self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]
            ) -> list[TagType]:
                post = await info.context["session"].get(SAPost, post_id)
                assert post is not None
                await orm.apply_ref_list(post, "tags", tags, info)
                await info.context["session"].commit()
                await info.context["session"].refresh(post, ["tags"])
                return post.tags  # type: ignore[return-value]

        schema = strawberry.Schema(
            query=Query,
            mutation=Mutation,
            extensions=[orm.optimizer_extension()],
        )
        result = await schema.execute(
            """
            mutation {
                modifyPostTags(
                    postId: 1
                    tags: [
                        { delete: { id: "1" } }
                        { create: { name: "patched-async" } }
                    ]
                ) {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert result.errors is None
        assert result.data["modifyPostTags"] == [{"name": "patched-async"}]

        query_result = await schema.execute(
            """
            {
                tags {
                    name
                }
            }
            """,
            context_value={"session": async_seed},
        )
        assert query_result.errors is None
        all_tag_names = sorted(tag["name"] for tag in query_result.data["tags"])
        assert "python" not in all_tag_names
        assert "patched-async" in all_tag_names


class SyncCrudTagRepo(AbstractRepo[SATag]):
    """Overrides only the sync CRUD, calling the defaults through ``super()``."""

    saved: list[str] = []

    def _create(self, model, data, info):
        return super()._create(model, {**data, "name": f"{data['name']}!"}, info)

    def _get(self, model, pk, info):
        row = super()._get(model, pk, info)
        return None if row is not None and row.name == "rust" else row

    def _save(self, instance, info):
        self.saved.append(instance.name)
        super()._save(instance, info)

    def _delete(self, instance, info):
        instance.name = f"deleted:{instance.name}"
        self._save(instance, info)


class TestSyncRepoOverridesOnAnAsyncSession:
    @pytest.mark.asyncio
    async def test_a_ref_list_creates_and_deletes_through_them(self, async_seed):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", repos={SATag: SyncCrudTagRepo}
        )

        @strawberry.input
        class CreateTagInput:
            name: str

        ref = orm.ref(SATag, create=CreateTagInput, delete=True)
        post = await async_seed.get(SAPost, 1)
        info = SimpleNamespace(context={"session": async_seed})
        delete = _declared_fields(ref)["delete"]
        await orm.apply_ref_list(
            post,
            "tags",
            [ref(delete=delete(id="1")), ref(create=CreateTagInput(name="new"))],
            info,
        )
        await async_seed.commit()
        names = await async_seed.scalars(select(SATag.name).order_by(SATag.id))
        assert list(names) == ["deleted:python", "graphql", "rust", "new!"]

    @pytest.mark.asyncio
    async def test_the_async_defaults_call_them(self, async_seed):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        repo = SyncCrudTagRepo(orm.backend)
        info = SimpleNamespace(context={"session": async_seed})
        assert await repo._get_async(SATag, 3, info) is None
        python = await repo._get_async(SATag, 1, info)
        await repo._save_async(python, info)
        assert SyncCrudTagRepo.saved[-1:] == ["python"]

    @pytest.mark.asyncio
    async def test_a_sync_default_outside_them_refuses_an_async_session(
        self, async_seed
    ):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")
        info = SimpleNamespace(context={"session": async_seed})
        with pytest.raises(TypeError, match="call its \\*_async variant"):
            AbstractRepo(orm.backend)._get(SATag, 1, info)
