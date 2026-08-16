"""Field namespace tests for the Tortoise backend."""

from dataclasses import dataclass

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.field_namespace import (
    AbstractTestFieldNamespaceAsync,
    AbstractTestScopeIsARowControlAsync,
)
from tests.backends.tortoise.models import Comment as DjComment
from tests.backends.tortoise.models import Post as DjPost
from tests.backends.tortoise.models import Tag as DjTag
from tests.backends.tortoise.models import User as DjUser


class TestFieldNamespace(AbstractTestFieldNamespaceAsync):
    @pytest.fixture(autouse=True)
    def _seed(self, seed):
        pass

    def _orm(self):
        return StrawberryORM.for_tortoise(
            lazy_resolution="off", warn_missing_scope=False
        )

    def _leaves(self, orm):
        @orm.type(DjComment)
        class CommentType:
            id: auto
            body: auto

        @orm.type(DjTag)
        class TagType:
            id: auto
            name: auto

        @orm.type(DjUser)
        class UserType:
            id: auto
            name: auto

        return CommentType, TagType, UserType

    async def _execute(self, orm, PostType, query, context=None):
        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.auto()

        result = await orm.schema(query=Query).execute(query, context_value=context)
        assert result.errors is None, result.errors
        return result.data["posts"][0]

    # -- happy paths ---------------------------------------------------------

    async def run_scoped_decorator(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.scoped
            def comments(qs, info) -> list[CommentType]:
                return qs.filter(body__startswith="Nice")

        post = await self._execute(orm, PostType, "{ posts { comments { body } } }")
        return [c["body"] for c in post["comments"]]

    async def run_scoped_inline(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.scoped(
                lambda qs, info: qs.filter(name="python")
            )

        post = await self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    async def run_custom(self):
        orm = self._orm()
        self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.custom
            def title_upper(self, info: strawberry.Info) -> str:
                return self.title.upper()

        post = await self._execute(orm, PostType, "{ posts { titleUpper } }")
        return post["titleUpper"]

    async def run_computed(self):
        orm = self._orm()
        _, _, UserType = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

            @orm.field.computed(using=["author"])
            def byline(self, info: strawberry.Info) -> str:
                return f"by {self.author.name}"

        post = await self._execute(orm, PostType, "{ posts { byline } }")
        return post["byline"]

    async def run_scope_reading_info(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.scoped
            def comments(qs, info) -> list[CommentType]:
                return qs.filter(body__startswith=info.context["prefix"])

        post = await self._execute(
            orm, PostType, "{ posts { comments { body } } }", {"prefix": "Nice"}
        )
        return [c["body"] for c in post["comments"]]

    async def run_legacy_one_arg_scope(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.scoped(
                lambda qs, info: qs.filter(name="python")
            )

        post = await self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    async def run_eager_bare(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.eager()

        post = await self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    async def run_eager_scope(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.eager
            def comments(qs, info) -> list[CommentType]:
                return qs.filter(body__startswith="Nice")

        post = await self._execute(orm, PostType, "{ posts { comments { body } } }")
        return [c["body"] for c in post["comments"]]

    async def run_lazy_using(self):
        orm = self._orm()
        _, _, UserType = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

            @orm.field.lazy(using=["author"])
            def byline(self, info: strawberry.Info) -> str:
                return f"by {self.author.name}"

        post = await self._execute(orm, PostType, "{ posts { byline } }")
        return post["byline"]

    async def run_lazy(self):
        orm = self._orm()
        self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy
            def title_upper(self, info: strawberry.Info) -> str:
                return self.title.upper()

        post = await self._execute(orm, PostType, "{ posts { titleUpper } }")
        return post["titleUpper"]

    def declare_eager_taking_self(self):
        orm = self._orm()

        @orm.field.eager
        def byline(self, info) -> str: ...

    async def run_eager_metadata_only(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.eager(disable_optimization=True)

        post = await self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    async def run_lazy_with_filters(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(filters=orm.filter(DjComment))
            def searchable(self, info: strawberry.Info) -> list[CommentType]:
                return DjComment.filter(post_id=self.id)

        post = await self._execute(
            orm,
            PostType,
            "{ posts { searchable(filter: { field: { body: "
            '{ startsWith: "Nice" } } }) { body } } }',
        )
        return [c["body"] for c in post["searchable"]]

    # -- rejected shapes -----------------------------------------------------

    def declare_scoped_taking_self(self):
        orm = self._orm()

        @orm.field.scoped
        def comments(self, info) -> list[str]: ...

    def declare_custom_without_self(self):
        orm = self._orm()

        @orm.field.custom
        def title_upper(qs, info) -> str: ...

    def declare_scoped_without_annotation(self):
        orm = self._orm()
        self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            tags = orm.field.scoped(lambda qs, info: qs)

    def declare_scoped_on_a_column(self):
        orm = self._orm()
        self._leaves(orm)

        @orm.type(DjPost)
        class PostType:
            id: auto
            title: str = orm.field.scoped(lambda qs, info: qs)


class TestScopeIsARowControl(AbstractTestScopeIsARowControlAsync):
    @pytest.fixture(autouse=True)
    def _seed2(self, seed):
        pass

    def _schema(self, scope):
        orm = StrawberryORM.for_tortoise(
            lazy_resolution="off", warn_missing_scope=False
        )

        @orm.type(DjPost, filters=orm.filter(DjPost))
        class PostType:
            id: auto
            title: auto

        @orm.type(DjUser, filters=orm.filter(DjUser))
        class UserType:
            id: auto
            name: auto
            posts: list[PostType] = orm.field.auto(scope=scope)

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()

        return orm.schema(query=Query)

    def _published_only(self, qs, info):
        return qs.filter(is_published=True)

    async def _run(self, scope, query):
        result = await self._schema(scope).execute(query)
        assert result.errors is None, result.errors
        return result.data

    async def read_scoped_edge(self):
        data = await self._run(self._published_only, "{ users { posts { title } } }")
        return sorted(p["title"] for u in data["users"] for p in u["posts"])

    async def _probe(self, title):
        data = await self._run(
            self._published_only,
            "{ users(filter: { object: { posts: { field: { title: "
            f'{{ exact: "{title}" }} }} }} }} }}) {{ name }} }}',
        )
        return [u["name"] for u in data["users"]]

    async def probe_hidden_through_edge(self):
        return await self._probe("Draft Post")

    async def probe_visible_through_edge(self):
        return await self._probe("Hello World")

    async def run_dataclass_scope(self):
        @dataclass
        class PublishedScope:
            published: bool = True

            def __call__(self, qs, info):
                return qs.filter(is_published=self.published)

        data = await self._run(PublishedScope(), "{ users { posts { title } } }")
        return sorted(p["title"] for u in data["users"] for p in u["posts"])
