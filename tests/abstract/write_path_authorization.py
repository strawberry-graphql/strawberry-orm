"""Writes the library performs stay inside what the app authorized.

``orm.apply_ref_list()`` and ``orm.apply_upsert()`` write on the caller's
behalf, so the repo, the ``authorize`` callback and the projection are all that
stands between a client and rows it does not own. Each class covers one way a
client could get past them.

Each backend supplies a ``writes`` fixture: a :class:`WriteDb` over its own
blog models, in every mode the backend writes in - a sync and an async
SQLAlchemy session, Django from sync and async code, and Tortoise.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import strawberry
from strawberry import relay

from strawberry_orm import (
    AbstractRepo,
    MutationPolicy,
    UpsertNotFoundError,
    UpsertValidationError,
)
from strawberry_orm.mutations import RelationRemovalPolicy
from strawberry_orm.types import auto
from strawberry_orm.upserts import _declared_fields


async def resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@strawberry.input
class TagCreate:
    name: str


@strawberry.input
class TagUpdate:
    id: strawberry.ID
    name: str | None = strawberry.UNSET


@strawberry.input
class CommentUpdate:
    id: strawberry.ID
    body: str | None = strawberry.UNSET


def item(ref_type: type, operation: str, **values: Any) -> Any:
    """One *ref_type* list item that selects *operation*."""
    return ref_type(**{operation: _declared_fields(ref_type)[operation](**values)})


def member(input_type: type, *path: str) -> type:
    """The generated input type reached from *input_type* through *path*."""
    for name in path:
        input_type = _declared_fields(input_type)[name]
    return input_type


def fields_of(input_type: type) -> set[str]:
    return set(input_type.__dataclass_fields__)


@dataclass
class WriteDb:
    """One backend's database, as the tests below drive it."""

    m: SimpleNamespace
    #: Whether repo hooks and ``authorize`` may be ``async`` in this mode.
    async_hooks: bool

    @property
    def context(self) -> dict[str, Any]:
        return {}

    @property
    def info(self) -> Any:
        return SimpleNamespace(context=self.context)

    async def write(self, call: Callable[[], Any]) -> Any:
        """Run a library write the way this mode's resolvers would."""
        return await resolve(call())

    async def refs(
        self, orm: Any, instance: Any, field: str, refs: list[Any], **kwargs: Any
    ) -> None:
        await self.write(
            lambda: orm.apply_ref_list(instance, field, refs, self.info, **kwargs)
        )

    async def upsert(self, orm: Any, payload: Any, **kwargs: Any) -> Any:
        return await self.write(lambda: orm.apply_upsert(payload, self.info, **kwargs))

    # -- Per backend -----------------------------------------------------------

    def new_orm(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    def only(self, query: Any, model: type, pks: list[int]) -> Any:
        """*query* narrowed to the rows of *model* whose key is in *pks*."""
        raise NotImplementedError

    async def create(self, model: type, **values: Any) -> Any:
        raise NotImplementedError

    async def add_related(self, instance: Any, name: str, *rows: Any) -> None:
        raise NotImplementedError

    async def rows(self, model: type) -> list[Any]:
        """Every row of *model*, by primary key."""
        raise NotImplementedError

    async def reload(self, instance: Any) -> Any:
        raise NotImplementedError

    async def related(self, instance: Any, name: str) -> list[Any]:
        """The rows in *instance*'s relation *name*, by primary key."""
        raise NotImplementedError


class Blog:
    """Alice, Bob and Carol; Alice's post Hello and Bob's post Other.

    Hello is tagged python and go, Other rust. Alice wrote every comment:
    ``mine`` and ``loose`` on Hello, ``theirs`` on Other, and one reply each
    under ``mine`` and ``theirs``.
    """

    @pytest_asyncio.fixture
    async def blog(self, writes):
        m = writes.m
        alice = await writes.create(m.User, id=1, name="Alice", email="a@example.com")
        bob = await writes.create(m.User, id=2, name="Bob", email="b@example.com")
        carol = await writes.create(m.User, id=3, name="Carol", email="c@example.com")
        python = await writes.create(m.Tag, id=1, name="python")
        rust = await writes.create(m.Tag, id=2, name="rust")
        go = await writes.create(m.Tag, id=3, name="go")
        hello = await writes.create(m.Post, id=1, title="Hello", body="B", author=alice)
        other = await writes.create(m.Post, id=2, title="Other", body="B", author=bob)
        await writes.add_related(hello, "tags", python, go)
        await writes.add_related(other, "tags", rust)

        async def comment(pk: int, body: str, post: Any, parent: Any = None) -> Any:
            return await writes.create(
                m.Comment, id=pk, body=body, post=post, author=alice, parent=parent
            )

        mine = await comment(1, "mine", hello)
        theirs = await comment(2, "theirs", other)
        reply = await comment(3, "reply", hello, mine)
        await comment(4, "their reply", other, theirs)
        await comment(5, "loose", hello)
        return SimpleNamespace(
            alice=alice,
            bob=bob,
            carol=carol,
            hello=hello,
            other=other,
            mine=mine,
            theirs=theirs,
            reply=reply,
        )


async def names(writes: WriteDb, instance: Any, relation: str) -> list[str]:
    return [row.name for row in await writes.related(instance, relation)]


async def ids(writes: WriteDb, instance: Any, relation: str) -> list[int]:
    return [row.id for row in await writes.related(instance, relation)]


async def tag_names(writes: WriteDb) -> list[str]:
    return [tag.name for tag in await writes.rows(writes.m.Tag)]


def allow(action, model, pk, info):
    return True


# -- Repos -----------------------------------------------------------------------


class DenyAllRepo(AbstractRepo):
    def can_create(self, data, info):
        return False

    def can_update(self, instance, data, info):
        return False

    def can_delete(self, instance, info):
        return False

    def can_link(self, parent, field, instance, info):
        return False

    def can_unlink(self, parent, field, instance, info):
        return False


class AsyncDenyAllRepo(AbstractRepo):
    async def can_delete(self, instance, info):
        return False

    async def can_update(self, instance, data, info):
        return False

    async def can_upsert(self, instance, info):
        return False


class ShoutingRepo(AbstractRepo):
    def on_before_create(self, data, info):
        return {**data, "name": data["name"].upper()}


class SoftDeleteRepo(AbstractRepo):
    def _delete(self, instance, info):
        instance.name = f"deleted:{instance.name}"
        self._save(instance, info)

    async def _delete_async(self, instance, info):
        instance.name = f"deleted:{instance.name}"
        await self._save_async(instance, info)


def scoped_repo(writes: WriteDb, visible: list[int]) -> type:
    class ScopedRepo(AbstractRepo):
        def scope_query(self, query, info):
            return writes.only(query, self.model, visible)

    return ScopedRepo


def label(row: Any) -> Any:
    for attr in ("name", "title", "body"):
        if hasattr(row, attr):
            return getattr(row, attr)
    raise AssertionError(row)  # pragma: no cover


def link_recorder(calls: list[tuple[Any, ...]], *, locked: set[str]) -> type:
    """A repo that records link checks; a *locked* row stays where it is.

    A locked row cannot be linked anywhere, and nothing can leave it or be
    taken from it.
    """

    class LinkRecorder(AbstractRepo):
        def can_link(self, parent, field, instance, info):
            calls.append(("can_link", label(parent), field, label(instance)))
            return label(instance) not in locked

        def can_unlink(self, parent, field, instance, info):
            calls.append(("can_unlink", label(parent), field, label(instance)))
            return not {label(parent), label(instance)} & locked

    return LinkRecorder


# -- Ref-list delete and unlink act only on members --------------------------------


class AbstractTestRefListMembership(Blog):
    @pytest.fixture(params=["without a repo", "with a repo"])
    def orm(self, request, writes):
        if request.param == "without a repo":
            return writes.new_orm()
        m = writes.m
        return writes.new_orm(repos={m.Tag: AbstractRepo, m.Comment: AbstractRepo})

    async def test_delete_refuses_a_tag_outside_the_relation(self, writes, blog, orm):
        ref = orm.ref(writes.m.Tag, unlink=True, delete=True)
        with pytest.raises(UpsertNotFoundError, match=r"No Tag 2 in Post\.tags"):
            await writes.refs(orm, blog.hello, "tags", [item(ref, "delete", id="2")])
        assert await names(writes, blog.other, "tags") == ["rust"]

    async def test_unlink_refuses_a_tag_outside_the_relation(self, writes, blog, orm):
        ref = orm.ref(writes.m.Tag, unlink=True, delete=True)
        with pytest.raises(UpsertNotFoundError, match=r"No Tag 2 in Post\.tags"):
            await writes.refs(orm, blog.hello, "tags", [item(ref, "unlink", id="2")])
        assert await names(writes, blog.other, "tags") == ["rust"]

    async def test_delete_refuses_a_comment_on_another_post(self, writes, blog, orm):
        ref = orm.ref(writes.m.Comment, delete=True)
        with pytest.raises(
            UpsertNotFoundError, match=r"No Comment 2 in Post\.comments"
        ):
            await writes.refs(
                orm, blog.hello, "comments", [item(ref, "delete", id="2")]
            )
        assert await ids(writes, blog.other, "comments") == [2, 4]

    async def test_delete_and_unlink_refuse_another_comments_reply(
        self, writes, blog, orm
    ):
        ref = orm.ref(writes.m.Comment, unlink=True, delete=True)
        for operation in ("delete", "unlink"):
            with pytest.raises(
                UpsertNotFoundError, match=r"No Comment 4 in Comment\.replies"
            ):
                await writes.refs(
                    orm, blog.mine, "replies", [item(ref, operation, id="4")]
                )
        assert await ids(writes, blog.theirs, "replies") == [4]

    async def test_a_missing_row_is_refused_like_a_non_member(self, writes, blog, orm):
        ref = orm.ref(writes.m.Tag, unlink=True, delete=True)
        for operation in ("delete", "unlink"):
            with pytest.raises(UpsertNotFoundError, match=r"No Tag 99 in Post\.tags"):
                await writes.refs(
                    orm, blog.hello, "tags", [item(ref, operation, id="99")]
                )

    async def test_member_tags_can_still_be_unlinked_and_deleted(
        self, writes, blog, orm
    ):
        ref = orm.ref(writes.m.Tag, unlink=True, delete=True)
        await writes.refs(
            orm,
            blog.hello,
            "tags",
            [item(ref, "unlink", id="3"), item(ref, "delete", id="1")],
        )
        assert await names(writes, blog.hello, "tags") == []
        assert await tag_names(writes) == ["rust", "go"]

    async def test_a_member_comment_can_still_be_deleted(self, writes, blog, orm):
        ref = orm.ref(writes.m.Comment, delete=True)
        await writes.refs(orm, blog.hello, "comments", [item(ref, "delete", id="5")])
        assert await ids(writes, blog.hello, "comments") == [1, 3]

    async def test_member_replies_can_still_be_unlinked_and_deleted(
        self, writes, blog, orm
    ):
        ref = orm.ref(writes.m.Comment, unlink=True, delete=True)
        await writes.refs(orm, blog.mine, "replies", [item(ref, "unlink", id="3")])
        assert await ids(writes, blog.mine, "replies") == []
        assert (await writes.reload(blog.reply)).parent_id is None
        await writes.refs(orm, blog.theirs, "replies", [item(ref, "delete", id="4")])
        assert [c.id for c in await writes.rows(writes.m.Comment)] == [1, 2, 3, 5]

    async def test_a_required_foreign_key_cannot_be_unlinked(self, writes, blog, orm):
        ref = orm.ref(writes.m.Comment, unlink=True)
        with pytest.raises(UpsertValidationError, match="not nullable"):
            await writes.refs(
                orm, blog.hello, "comments", [item(ref, "unlink", id="5")]
            )
        assert await ids(writes, blog.hello, "comments") == [1, 3, 5]


# -- authorize= is layered on the repo, not instead of it ----------------------------


class AbstractTestAuthorizeKeepsTheRepo(Blog):
    async def test_repo_denials_stand_when_authorize_allows(self, writes, blog):
        orm = writes.new_orm(repos={writes.m.Tag: DenyAllRepo})
        ref = orm.ref(
            writes.m.Tag, create=TagCreate, update=TagUpdate, unlink=True, delete=True
        )
        attempts = [
            (item(ref, "create", name="new"), "can_create"),
            (item(ref, "update", id="3", name="renamed"), "can_update"),
            (item(ref, "unlink", id="3"), "can_unlink"),
            (item(ref, "delete", id="3"), "can_delete"),
        ]
        for ref_item, check in attempts:
            with pytest.raises(PermissionError, match=f"{check} denied"):
                await writes.refs(orm, blog.hello, "tags", [ref_item], authorize=allow)
        assert await tag_names(writes) == ["python", "rust", "go"]
        assert await names(writes, blog.hello, "tags") == ["python", "go"]

    async def test_scope_query_still_hides_rows(self, writes, blog):
        orm = writes.new_orm(repos={writes.m.Tag: scoped_repo(writes, [1, 3])})
        ref = orm.ref(writes.m.Tag, update=TagUpdate, delete=True)
        await writes.refs(
            orm,
            blog.hello,
            "tags",
            [item(ref, "update", id="2", name="pwned")],
            authorize=allow,
        )
        with pytest.raises(UpsertNotFoundError):
            await writes.refs(
                orm, blog.other, "tags", [item(ref, "delete", id="2")], authorize=allow
            )
        assert await names(writes, blog.hello, "tags") == ["python", "go"]
        assert await names(writes, blog.other, "tags") == ["rust"]

    async def test_lifecycle_hooks_still_run(self, writes, blog):
        orm = writes.new_orm(repos={writes.m.Tag: ShoutingRepo})
        ref = orm.ref(writes.m.Tag, create=TagCreate)
        await writes.refs(
            orm, blog.hello, "tags", [item(ref, "create", name="new")], authorize=allow
        )
        assert await names(writes, blog.hello, "tags") == ["python", "go", "NEW"]

    async def test_authorize_still_skips_what_it_denies(self, writes, blog):
        orm = writes.new_orm(repos={writes.m.Tag: AbstractRepo})
        ref = orm.ref(writes.m.Tag, delete=True)
        await writes.refs(
            orm,
            blog.hello,
            "tags",
            [item(ref, "delete", id="1")],
            authorize=lambda action, model, pk, info: action != "delete",
        )
        assert await names(writes, blog.hello, "tags") == ["python", "go"]


# -- Permission checks that are coroutines, or not booleans --------------------------


async def deny(action, model, pk, info):
    return False


class AbstractTestPermissionResults(Blog):
    @staticmethod
    def rename_python(orm: Any, m: SimpleNamespace) -> Any:
        upsert = orm.mutations.upsert_node_input(
            model=m.Tag,
            project={
                "_meta": {
                    "create": ["name"],
                    "update": ["name"],
                    "upsert": {"where": ["name"], "onMatch": "UPDATE"},
                }
            },
            name="TagUpsertInput",
        )
        return upsert(
            where=member(upsert, "where")(name="python"),
            update=member(upsert, "update")(name="py"),
        )

    async def test_an_async_repo_denial_is_a_denial(self, writes, blog):
        orm = writes.new_orm(repos={writes.m.Tag: AsyncDenyAllRepo})
        ref = orm.ref(writes.m.Tag, update=TagUpdate, delete=True)
        for ref_item, check in (
            (item(ref, "delete", id="1"), "can_delete"),
            (item(ref, "update", id="1", name="py"), "can_update"),
        ):
            if writes.async_hooks:
                with pytest.raises(PermissionError, match=f"{check} denied"):
                    await writes.refs(orm, blog.hello, "tags", [ref_item])
            else:
                with pytest.raises(TypeError, match="returned an awaitable"):
                    await writes.refs(orm, blog.hello, "tags", [ref_item])
        assert await names(writes, blog.hello, "tags") == ["python", "go"]

    async def test_an_async_authorize_denial_is_a_denial(self, writes, blog):
        orm = writes.new_orm()
        ref = orm.ref(writes.m.Tag, delete=True)
        refs = [item(ref, "delete", id="1")]
        if writes.async_hooks:
            await writes.refs(orm, blog.hello, "tags", refs, authorize=deny)
        else:
            with pytest.raises(TypeError, match="returned an awaitable"):
                await writes.refs(orm, blog.hello, "tags", refs, authorize=deny)
        assert await names(writes, blog.hello, "tags") == ["python", "go"]

    async def test_an_async_repo_denial_stops_an_upsert(self, writes, blog):
        orm = writes.new_orm(repos={writes.m.Tag: AsyncDenyAllRepo})
        payload = self.rename_python(orm, writes.m)
        if writes.async_hooks:
            with pytest.raises(PermissionError, match="can_upsert denied"):
                await writes.upsert(orm, payload)
        else:
            with pytest.raises(TypeError, match="returned an awaitable"):
                await writes.upsert(orm, payload)
        assert await tag_names(writes) == ["python", "rust", "go"]

    async def test_an_async_authorize_denial_stops_an_upsert(self, writes, blog):
        orm = writes.new_orm()
        payload = self.rename_python(orm, writes.m)
        if writes.async_hooks:
            with pytest.raises(PermissionError, match="authorize denied the upsert"):
                await writes.upsert(orm, payload, authorize=deny)
        else:
            with pytest.raises(TypeError, match="returned an awaitable"):
                await writes.upsert(orm, payload, authorize=deny)
        assert await tag_names(writes) == ["python", "rust", "go"]

    async def test_an_async_authorize_that_allows_lets_the_write_through(
        self, writes, blog
    ):
        async def allow_async(action, model, pk, info):
            return True

        orm = writes.new_orm()
        payload = self.rename_python(orm, writes.m)
        if writes.async_hooks:
            await writes.upsert(orm, payload, authorize=allow_async)
            assert await tag_names(writes) == ["py", "rust", "go"]
        else:
            with pytest.raises(TypeError, match="returned an awaitable"):
                await writes.upsert(orm, payload, authorize=allow_async)

    async def test_a_truthy_result_that_is_not_a_bool_allows_with_a_warning(
        self, writes, blog
    ):
        class RowReturningRepo(AbstractRepo):
            def can_delete(self, instance, info):
                return instance

        orm = writes.new_orm(repos={writes.m.Tag: RowReturningRepo})
        ref = orm.ref(writes.m.Tag, delete=True)
        with pytest.warns(DeprecationWarning, match="can_delete returned Tag"):
            await writes.refs(orm, blog.hello, "tags", [item(ref, "delete", id="1")])
        assert await names(writes, blog.hello, "tags") == ["go"]

    async def test_none_is_a_denial(self, writes, blog):
        class SilentRepo(AbstractRepo):
            def can_delete(self, instance, info):
                return None

        orm = writes.new_orm(repos={writes.m.Tag: SilentRepo})
        ref = orm.ref(writes.m.Tag, delete=True)
        with pytest.raises(PermissionError, match="can_delete denied"):
            await writes.refs(orm, blog.hello, "tags", [item(ref, "delete", id="1")])
        assert await names(writes, blog.hello, "tags") == ["python", "go"]


# -- An upsert miss writes only what create allows -----------------------------------


SCOOP = {
    "_meta": {
        "create": ["title", "body"],
        "upsert": {"where": True, "onMatch": "IGNORE"},
    },
    "author": {"_meta": {"update": ["name"]}},
}


class AbstractTestUpsertSeed(Blog):
    @pytest.fixture
    def orm(self, writes):
        return writes.new_orm()

    @pytest.fixture
    def upsert(self, orm, writes):
        return orm.mutations.upsert_node_input(
            model=writes.m.Post, project=SCOOP, name="ScoopInput"
        )

    @staticmethod
    def by_alice(upsert: type, **where: Any) -> Any:
        link = member(upsert, "create", "author")
        return upsert(
            where=member(upsert, "where")(**where),
            create=member(upsert, "create")(
                body="B", author=link(update=member(link, "update")(id="1"))
            ),
        )

    async def test_a_miss_cannot_write_a_where_field_create_does_not_allow(
        self, writes, blog, orm, upsert
    ):
        payload = self.by_alice(upsert, title="Scoop", is_published=True)
        with pytest.raises(UpsertValidationError, match=r"where\.is_published"):
            await writes.upsert(orm, payload)
        assert [post.title for post in await writes.rows(writes.m.Post)] == [
            "Hello",
            "Other",
        ]

    async def test_a_miss_is_seeded_from_where_fields_create_allows(
        self, writes, blog, orm, upsert
    ):
        created = await writes.upsert(orm, self.by_alice(upsert, title="Scoop"))
        assert (created.title, created.is_published) == ("Scoop", False)

    async def test_a_where_id_finds_rows_but_never_names_a_new_one(
        self, writes, blog, orm, upsert
    ):
        created = await writes.upsert(
            orm, self.by_alice(upsert, id="99", title="Scoop")
        )
        assert created.title == "Scoop"
        assert created.id != 99

    async def test_where_alone_may_still_match_on_any_offered_field(
        self, writes, blog, orm, upsert
    ):
        payload = upsert(
            where=member(upsert, "where")(title="Other", is_published=False)
        )
        assert (await writes.upsert(orm, payload)).id == 2


# -- Node inputs follow the type's exclude= -----------------------------------------


def hide_email(orm: Any, m: SimpleNamespace) -> None:
    @orm.type(m.User, exclude=["email"])
    class UserNode(relay.Node):
        id: relay.NodeID[int]
        name: auto


class AbstractTestNodeInputsFollowTypeExclude:
    async def test_generated_inputs_leave_out_an_excluded_column(self, writes):
        orm = writes.new_orm()
        m = writes.m
        hide_email(orm, m)
        create = orm.mutations.create_node_input(models=[m.User], name="CreateNode")
        update = orm.mutations.update_node_input(models=[m.User], name="UpdateNode")
        upsert = orm.mutations.upsert_node_input(
            model=m.User,
            project={
                "_meta": {"create": True, "update": True, "upsert": {"where": True}}
            },
            name="UserUpsertInput",
        )
        for generated in (
            member(create, "user"),
            member(update, "user"),
            member(upsert, "where"),
            member(upsert, "create"),
            member(upsert, "update"),
        ):
            assert "name" in fields_of(generated)
            assert "email" not in fields_of(generated)

    async def test_a_projection_cannot_name_an_excluded_column(self, writes):
        orm = writes.new_orm()
        m = writes.m
        hide_email(orm, m)
        for meta in (
            {"create": ["name", "email"], "upsert": {"where": ["name"]}},
            {"update": ["email"], "upsert": {"where": ["name"]}},
            {"upsert": {"where": ["email"]}},
        ):
            with pytest.raises(ValueError, match=r"\['email'\].*excludes"):
                orm.mutations.upsert_node_input(model=m.User, project={"_meta": meta})

    async def test_declaring_the_type_after_a_node_input_that_writes_it_is_refused(
        self, writes
    ):
        orm = writes.new_orm()
        m = writes.m
        orm.mutations.upsert_node_input(
            model=m.User,
            project={"_meta": {"name": "EarlyUser", "upsert": {"where": ["name"]}}},
        )
        with pytest.raises(ValueError, match=r"EarlyUserCreate can still write"):
            hide_email(orm, m)


# -- Projections opt in to destructive operations -------------------------------------


class AbstractTestProjectionDefaults(Blog):
    async def test_an_implicit_projection_offers_no_destructive_operation(self, writes):
        orm = writes.new_orm()
        m = writes.m
        for project in (None, {"post": {"tags": {}, "comments": {}, "author": {}}}):
            create = orm.mutations.create_node_input(models=[m.Post], project=project)
            post = member(create, "post")
            for relation in ("tags", "comments"):
                items = member(post, relation)
                assert items.__relation_policy__["allowed_ops"] == {"create", "update"}
                assert not {"unlink", "delete"} & fields_of(items)
            author = member(post, "author")
            assert author.__relation_policy__["on_replace_options"] == ("DISCONNECT",)
            assert "on_replace" not in fields_of(author)

    async def test_destructive_operations_can_be_opted_into(self, writes):
        orm = writes.new_orm()
        m = writes.m
        create = orm.mutations.create_node_input(
            models=[m.Post],
            project={
                "post": {
                    "tags": {"_meta": {"update": True, "unlink": True, "delete": True}},
                    "author": {
                        "_meta": {"create": True, "onReplace": ["DISCONNECT", "DELETE"]}
                    },
                }
            },
        )
        post = member(create, "post")
        assert {"unlink", "delete"} <= fields_of(member(post, "tags"))
        assert "on_replace" in fields_of(member(post, "author"))

    async def test_on_replace_delete_is_refused_unless_opted_into(self, writes, blog):
        orm = writes.new_orm()
        m = writes.m
        upsert = orm.mutations.upsert_node_input(
            model=m.Post,
            project={
                "_meta": {
                    "create": True,
                    "update": True,
                    "upsert": {"where": ["title"]},
                },
                "author": {},
            },
            name="PostUpsertInput",
        )
        link_type = member(upsert, "update", "author")
        link = link_type(update=member(link_type, "update")(id="1"))
        link.on_replace = RelationRemovalPolicy.DELETE
        payload = upsert(
            where=member(upsert, "where")(title="Other"),
            update=member(upsert, "update")(author=link),
        )
        with pytest.raises(UpsertValidationError, match="onReplace DELETE"):
            await writes.upsert(orm, payload)
        assert [user.name for user in await writes.rows(m.User)] == [
            "Alice",
            "Bob",
            "Carol",
        ]


# -- Re-pointing a link asks both sides ------------------------------------------------


POST_WITH_AUTHOR = {
    "_meta": {
        "create": ["title", "body"],
        "update": ["title"],
        "upsert": {"where": ["title"], "onMatch": "UPDATE"},
    },
    "author": {
        "_meta": {"update": ["name"], "upsert": {"where": ["id"], "onMatch": "IGNORE"}}
    },
    "comments": {"_meta": {"update": ["body"]}},
}


class AbstractTestLinkChecks(Blog):
    @pytest.fixture
    def calls(self):
        return []

    @pytest.fixture
    def orm(self, writes, calls):
        m = writes.m
        recorder = link_recorder(calls, locked={"Bob", "Other"})
        return writes.new_orm(repos={m.User: recorder, m.Comment: recorder})

    @pytest.fixture
    def upsert(self, orm, writes):
        return orm.mutations.upsert_node_input(
            model=writes.m.Post, project=POST_WITH_AUTHOR, name="PostUpsertInput"
        )

    @staticmethod
    def edit_post(upsert: type, title: str, **update: Any) -> Any:
        return upsert(
            where=member(upsert, "where")(title=title),
            update=member(upsert, "update")(**update),
        )

    @staticmethod
    def author_by_id(upsert: type, pk: str, operation: str = "update") -> Any:
        link_type = member(upsert, "create", "author")
        if operation == "update":
            return link_type(update=member(link_type, "update")(id=pk))
        target = member(link_type, "upsert")
        return link_type(upsert=target(where=member(target, "where")(id=pk)))

    async def test_linking_a_new_row_to_an_existing_target_asks_can_link(
        self, writes, blog, orm, upsert, calls
    ):
        for operation in ("update", "upsert"):
            payload = upsert(
                where=member(upsert, "where")(title="New"),
                create=member(upsert, "create")(
                    body="B", author=self.author_by_id(upsert, "2", operation)
                ),
            )
            with pytest.raises(PermissionError, match="can_link denied"):
                await writes.upsert(orm, payload)
        assert calls == [("can_link", "New", "author", "Bob")] * 2
        assert [post.title for post in await writes.rows(writes.m.Post)] == [
            "Hello",
            "Other",
        ]

    async def test_re_pointing_a_link_asks_can_unlink_of_the_previous_target(
        self, writes, blog, orm, upsert, calls
    ):
        payload = self.edit_post(upsert, "Other", author=self.author_by_id(upsert, "1"))
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await writes.upsert(orm, payload)
        assert calls == [
            ("can_link", "Other", "author", "Alice"),
            ("can_unlink", "Other", "author", "Bob"),
        ]
        assert (await writes.reload(blog.other)).author_id == 2

    async def test_an_allowed_re_point_asks_both_sides(
        self, writes, blog, orm, upsert, calls
    ):
        payload = self.edit_post(upsert, "Hello", author=self.author_by_id(upsert, "3"))
        await writes.upsert(orm, payload)
        assert calls == [
            ("can_link", "Hello", "author", "Carol"),
            ("can_unlink", "Hello", "author", "Alice"),
        ]
        assert (await writes.reload(blog.hello)).author_id == 3

    async def test_keeping_the_same_target_asks_nothing(
        self, writes, blog, orm, upsert, calls
    ):
        payload = self.edit_post(upsert, "Hello", author=self.author_by_id(upsert, "1"))
        await writes.upsert(orm, payload)
        assert calls == []

    async def test_moving_a_comment_off_another_post_asks_that_post(
        self, writes, blog, orm, upsert, calls
    ):
        moved = item(member(upsert, "update", "comments"), "update", id="2")
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await writes.upsert(orm, self.edit_post(upsert, "Hello", comments=[moved]))
        assert calls == [
            ("can_link", "Hello", "comments", "theirs"),
            ("can_unlink", "Other", "comments", "theirs"),
        ]
        assert await ids(writes, blog.other, "comments") == [2, 4]

    async def test_a_ref_list_moving_a_comment_asks_its_old_post(
        self, writes, blog, orm, calls
    ):
        ref = orm.ref(writes.m.Comment, update=CommentUpdate)
        with pytest.raises(PermissionError, match="can_unlink denied"):
            await writes.refs(
                orm, blog.hello, "comments", [item(ref, "update", id="2")]
            )
        assert calls == [
            ("can_link", "Hello", "comments", "theirs"),
            ("can_unlink", "Other", "comments", "theirs"),
        ]
        assert await ids(writes, blog.other, "comments") == [2, 4]

    async def test_a_ref_list_can_move_a_comment_both_sides_allow(
        self, writes, blog, calls
    ):
        m = writes.m
        orm = writes.new_orm(repos={m.Comment: link_recorder(calls, locked=set())})
        ref = orm.ref(m.Comment, update=CommentUpdate)
        await writes.refs(
            orm,
            blog.hello,
            "comments",
            [item(ref, "update", id="2"), item(ref, "update", id="1", body="kept")],
        )
        assert calls == [
            ("can_link", "Hello", "comments", "theirs"),
            ("can_unlink", "Other", "comments", "theirs"),
            ("can_link", "Hello", "comments", "mine"),
        ]
        assert await ids(writes, blog.hello, "comments") == [1, 2, 3, 5]
        assert await ids(writes, blog.other, "comments") == [4]

    async def test_attaching_a_loose_reply_asks_only_can_link(
        self, writes, blog, calls
    ):
        m = writes.m
        orm = writes.new_orm(repos={m.Comment: link_recorder(calls, locked=set())})
        ref = orm.ref(m.Comment, update=CommentUpdate)
        await writes.refs(orm, blog.mine, "replies", [item(ref, "update", id="5")])
        assert calls == [("can_link", "mine", "replies", "loose")]
        assert await ids(writes, blog.mine, "replies") == [3, 5]


# -- A ref list is one unit of work ----------------------------------------------------


class AbstractTestRefListIsAtomic(Blog):
    async def test_a_denial_part_way_leaves_nothing_written(self, writes, blog):
        class NotRustRepo(AbstractRepo):
            def can_update(self, instance, data, info):
                return instance.name != "rust"

        orm = writes.new_orm(repos={writes.m.Tag: NotRustRepo})
        ref = orm.ref(writes.m.Tag, create=TagCreate, update=TagUpdate)
        refs = [
            item(ref, "create", name="orphan"),
            item(ref, "update", id="1", name="renamed"),
            item(ref, "update", id="2", name="pwned"),
        ]
        with pytest.raises(PermissionError, match="can_update denied"):
            await writes.refs(orm, blog.hello, "tags", refs)
        assert await tag_names(writes) == ["python", "rust", "go"]
        assert await names(writes, blog.hello, "tags") == ["python", "go"]

    async def test_an_item_naming_two_operations_is_refused_before_any_write(
        self, writes, blog
    ):
        orm = writes.new_orm()
        ref = orm.ref(writes.m.Tag, create=TagCreate, delete=True)
        fields = _declared_fields(ref)
        refs = [
            item(ref, "create", name="orphan"),
            ref(create=fields["create"](name="decoy"), delete=fields["delete"](id="1")),
        ]
        with pytest.raises(UpsertValidationError, match="exactly one of"):
            await writes.refs(orm, blog.hello, "tags", refs)
        assert await tag_names(writes) == ["python", "rust", "go"]


# -- policy= and repos= together --------------------------------------------------------


class AbstractTestPolicyWithRepos:
    async def test_policy_and_repos_cannot_be_combined(self, writes):
        with pytest.raises(ValueError, match="either policy= or repos="):
            writes.new_orm(policy=MutationPolicy(), repos={writes.m.Tag: AbstractRepo})


# -- A ref-list delete goes through the repo ---------------------------------------------


class AbstractTestRefListDeleteUsesTheRepo(Blog):
    async def test_a_soft_delete_override_is_honoured(self, writes, blog):
        orm = writes.new_orm(repos={writes.m.Tag: SoftDeleteRepo})
        ref = orm.ref(writes.m.Tag, delete=True)
        await writes.refs(orm, blog.hello, "tags", [item(ref, "delete", id="1")])
        assert await tag_names(writes) == ["deleted:python", "rust", "go"]
