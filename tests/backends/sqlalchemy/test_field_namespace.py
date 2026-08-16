"""Field namespace tests for the SQLAlchemy backend."""

from dataclasses import dataclass

import pytest
import strawberry
from sqlalchemy import select

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.field_namespace import (
    AbstractTestFieldNamespace,
    AbstractTestScopeIsARowControl,
)
from tests.backends.sqlalchemy.models import Comment as SAComment
from tests.backends.sqlalchemy.models import Post as SAPost
from tests.backends.sqlalchemy.models import Tag as SATag
from tests.backends.sqlalchemy.models import User as SAUser


class TestFieldNamespace(AbstractTestFieldNamespace):
    @pytest.fixture(autouse=True)
    def _session(self, sa_session, seed):
        self._sa_session = sa_session

    def _orm(self):
        return StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
        )

    def _leaves(self, orm):
        @orm.type(SAComment)
        class CommentType:
            id: auto
            body: auto

        @orm.type(SATag)
        class TagType:
            id: auto
            name: auto

        @orm.type(SAUser)
        class UserType:
            id: auto
            name: auto

        return CommentType, TagType, UserType

    def _execute(self, orm, PostType, query):
        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.auto()

        result = orm.schema(query=Query).execute_sync(
            query, context_value={"session": self._sa_session}
        )
        assert result.errors is None, result.errors
        return result.data["posts"][0]

    # -- happy paths ---------------------------------------------------------

    def run_scoped_decorator(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.scoped
            def comments(qs, info) -> list[CommentType]:
                return qs.where(SAComment.body.like("Nice%"))

        post = self._execute(orm, PostType, "{ posts { comments { body } } }")
        return [c["body"] for c in post["comments"]]

    def run_scoped_inline(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.scoped(
                lambda qs, info: qs.where(SATag.name == "python")
            )

        post = self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    def run_custom(self):
        orm = self._orm()
        self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.custom
            def title_upper(self, info: strawberry.Info) -> str:
                return self.title.upper()

        return self._execute(orm, PostType, "{ posts { titleUpper } }")["titleUpper"]

    def run_computed(self):
        orm = self._orm()
        _, _, UserType = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

            @orm.field.computed(using=["author"])
            def byline(self, info: strawberry.Info) -> str:
                return f"by {self.author.name}"

        return self._execute(orm, PostType, "{ posts { byline } }")["byline"]

    def run_scope_reading_info(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.scoped
            def comments(qs, info) -> list[CommentType]:
                return qs.where(SAComment.body.like(info.context["prefix"] + "%"))

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.auto()

        result = orm.schema(query=Query).execute_sync(
            "{ posts { comments { body } } }",
            context_value={"session": self._sa_session, "prefix": "Nice"},
        )
        assert result.errors is None, result.errors
        return [c["body"] for c in result.data["posts"][0]["comments"]]

    def run_legacy_one_arg_scope(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.scoped(
                lambda qs: qs.where(SATag.name == "python")
            )

        post = self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    def run_eager_bare(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.eager()

        post = self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    def run_eager_scope(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.eager
            def comments(qs, info) -> list[CommentType]:
                return qs.where(SAComment.body.like("Nice%"))

        post = self._execute(orm, PostType, "{ posts { comments { body } } }")
        return [c["body"] for c in post["comments"]]

    def run_lazy_using(self):
        orm = self._orm()
        _, _, UserType = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            author: UserType

            @orm.field.lazy(using=["author"])
            def byline(self, info: strawberry.Info) -> str:
                return f"by {self.author.name}"

        return self._execute(orm, PostType, "{ posts { byline } }")["byline"]

    def run_lazy(self):
        orm = self._orm()
        self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy
            def title_upper(self, info: strawberry.Info) -> str:
                return self.title.upper()

        return self._execute(orm, PostType, "{ posts { titleUpper } }")["titleUpper"]

    def declare_eager_taking_self(self):
        orm = self._orm()

        @orm.field.eager
        def byline(self, info) -> str: ...

    def run_eager_metadata_only(self):
        orm = self._orm()
        _, TagType, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto
            tags: list[TagType] = orm.field.eager(disable_optimization=True)

        post = self._execute(orm, PostType, "{ posts { tags { name } } }")
        return [t["name"] for t in post["tags"]]

    def run_lazy_with_filters(self):
        orm = self._orm()
        CommentType, _, _ = self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: auto

            @orm.field.lazy(filters=orm.filter(SAComment))
            def searchable(self, info: strawberry.Info) -> list[CommentType]:
                return select(SAComment).where(SAComment.post_id == self.id)

        post = self._execute(
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

        @orm.type(SAPost)
        class PostType:
            id: auto
            tags = orm.field.scoped(lambda qs, info: qs)

    def declare_scoped_on_a_column(self):
        orm = self._orm()
        self._leaves(orm)

        @orm.type(SAPost)
        class PostType:
            id: auto
            title: str = orm.field.scoped(lambda qs, info: qs)


class TestScopeIsARowControl(AbstractTestScopeIsARowControl):
    @pytest.fixture(autouse=True)
    def _session2(self, sa_session, seed):
        self._sa_session = sa_session

    def _schema(self, scope):
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite", lazy_resolution="off", warn_missing_scope=False
        )

        @orm.type(SAPost, filters=orm.filter(SAPost))
        class PostType:
            id: auto
            title: auto

        @orm.type(SAUser, filters=orm.filter(SAUser))
        class UserType:
            id: auto
            name: auto
            posts: list[PostType] = orm.field.auto(scope=scope)

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()

        return orm.schema(query=Query)

    def _published_only(self, qs, info):
        return qs.where(SAPost.is_published.is_(True))

    def _run(self, scope, query):
        result = self._schema(scope).execute_sync(
            query, context_value={"session": self._sa_session}
        )
        assert result.errors is None, result.errors
        return result.data

    def read_scoped_edge(self):
        data = self._run(self._published_only, "{ users { posts { title } } }")
        return sorted(p["title"] for u in data["users"] for p in u["posts"])

    def _probe(self, title):
        data = self._run(
            self._published_only,
            "{ users(filter: { object: { posts: { field: { title: "
            f'{{ exact: "{title}" }} }} }} }} }}) {{ name }} }}',
        )
        return [u["name"] for u in data["users"]]

    def probe_hidden_through_edge(self):
        return self._probe("Draft Post")

    def probe_visible_through_edge(self):
        return self._probe("Hello World")

    def run_dataclass_scope(self):
        @dataclass
        class PublishedScope:
            published: bool = True

            def __call__(self, qs, info):
                return qs.where(SAPost.is_published.is_(self.published))

        data = self._run(PublishedScope(), "{ users { posts { title } } }")
        return sorted(p["title"] for u in data["users"] for p in u["posts"])
