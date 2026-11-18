from dataclasses import dataclass
from inspect import isawaitable
from types import SimpleNamespace

import pytest
import pytest_asyncio
import strawberry
from tortoise import Tortoise, fields
from tortoise.models import Model
from tortoise.query_utils import Prefetch

from strawberry_orm import StrawberryORM, auto
from tests.abstract.relation_value_trust import AbstractTestRelationValueTrust


class RtAuthor(Model):
    id = fields.IntField(primary_key=True)
    name = fields.CharField(max_length=50)
    is_public = fields.BooleanField(default=False)

    class Meta:
        table = "rt_author"


class RtPost(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50)
    is_published = fields.BooleanField(default=False)
    author = fields.ForeignKeyField(
        "rt_relation_trust.RtAuthor", related_name="posts", null=True
    )

    class Meta:
        table = "rt_post"


MODELS = SimpleNamespace(Author=RtAuthor, Post=RtPost)

STAFF_FIRST = (
    "staffP: staffPosts { title author { name posts { title } } } "
    "staffA: staffAuthors { name posts { title } }"
)


@dataclass(frozen=True)
class Filler:
    #: How the root fields read their rows: ``None`` is the library's field.
    root: str | None = None
    before: str = ""
    optimizer: bool = True


FILLERS = {
    "plain": Filler(),
    "select_related": Filler(root="select_related"),
    "prefetch_related": Filler(root="prefetch_related"),
    "prefetch_object": Filler(root="prefetch_object"),
    "prefetch_to_attr": Filler(root="prefetch_to_attr"),
    "pinned": Filler(root="pinned"),
    "pinned_twice": Filler(root="pinned", before=STAFF_FIRST),
    "sibling_options": Filler(
        before="warmP: warmPosts { title } warmA: warmAuthors { name }"
    ),
    "sibling_staff": Filler(before=STAFF_FIRST),
    "optimizer_off": Filler(optimizer=False),
    "optimizer_off_pinned": Filler(root="pinned", optimizer=False),
}


def _app_reads(kind):
    """The rows of *kind*, both relations loaded as the app's own reads load them."""
    if kind == "posts":
        return RtPost.all().prefetch_related("author__posts")
    return RtAuthor.all().prefetch_related("posts__author")


ROOTS = {
    "select_related": {
        "posts": lambda: RtPost.all().select_related("author"),
        "authors": lambda: RtAuthor.all(),
    },
    "prefetch_related": {
        "posts": lambda: _app_reads("posts"),
        "authors": lambda: _app_reads("authors"),
    },
    "prefetch_object": {
        "posts": lambda: RtPost.all().prefetch_related(
            Prefetch("author", RtAuthor.all().prefetch_related("posts"))
        ),
        "authors": lambda: RtAuthor.all().prefetch_related(
            Prefetch("posts", RtPost.all().prefetch_related("author"))
        ),
    },
    "prefetch_to_attr": {
        "posts": lambda: RtPost.all().prefetch_related(
            Prefetch("author", RtAuthor.all(), to_attr="writer")
        ),
        "authors": lambda: RtAuthor.all().prefetch_related(
            Prefetch("posts", RtPost.all().prefetch_related("author"), to_attr="every")
        ),
    },
}


@pytest.fixture(params=list(FILLERS))
def filler(request):
    return request.param


@pytest.fixture
def orm():
    return StrawberryORM.for_tortoise(lazy_resolution="off", warn_missing_scope=False)


@pytest_asyncio.fixture
async def rows():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"rt_relation_trust": [__name__]}
    )
    await Tortoise.generate_schemas()
    await RtAuthor.create(id=1, name="Alice", is_public=True)
    await RtAuthor.create(id=2, name="Hidden", is_public=False)
    for pk, title, published, author in [
        (1, "p1", True, 1),
        (2, "p2", True, 2),
        (3, "draft", False, 1),
        (4, "edge", True, 1),
    ]:
        await RtPost.create(
            id=pk, title=title, is_published=published, author_id=author
        )
    yield
    await Tortoise.close_connections()


@pytest.mark.usefixtures("rows")
class TestRelationValueTrust(AbstractTestRelationValueTrust):
    def models_for(self, filler):
        return MODELS

    def root(self, filler, models, kind):
        spec = FILLERS[filler]
        if kind == "warm_posts":
            return lambda info: _app_reads("posts")
        if kind == "warm_authors":
            return lambda info: _app_reads("authors")
        if spec.root is None:
            return None
        if spec.root == "pinned":
            return lambda info: list(info.context["pinned"][kind])
        read = ROOTS[spec.root][kind]
        return lambda info: read()

    def around(self, filler):
        return FILLERS[filler].before, ""

    def optimizer(self, filler):
        return FILLERS[filler].optimizer

    async def run(self, filler, schema, query):
        context = {}
        if FILLERS[filler].root == "pinned":
            context["pinned"] = {
                "posts": await _app_reads("posts").exclude(title="draft"),
                "authors": await _app_reads("authors").filter(name="Alice"),
            }
        return await schema.execute(query, context_value=context)

    @staticmethod
    def one(row, name):
        return getattr(row, name)

    @staticmethod
    def many(row, name):
        related = getattr(row, name)
        return list(related) if related._fetched else related.all()

    @staticmethod
    def public(queryset):
        return queryset.filter(is_public=True)

    @staticmethod
    def published(queryset):
        return queryset.filter(is_published=True)

    @staticmethod
    def not_edge(queryset):
        return queryset.exclude(title="edge")


@pytest.mark.usefixtures("rows")
async def test_an_async_resolver_sees_only_what_its_scope_allows(orm):
    """The root query prefetches every author; ``byline`` awaits as it reads."""

    @orm.type(RtAuthor, name="RtAuthor")
    class AuthorType:
        name: auto

        @classmethod
        def scope_rows(cls, qs, info):
            return qs.filter(is_public=True)

    @orm.type(RtPost, name="RtPost")
    class PostType:
        title: auto

        @orm.field.lazy(using=["author"])
        async def byline(self, info: strawberry.Info) -> str:
            author = self.author
            if isawaitable(author):
                author = await author
            return "anonymous" if author is None else f"by {author.name}"

    @strawberry.type
    class Query:
        @orm.field.lazy
        def posts(self, info: strawberry.Info) -> list[PostType]:
            return RtPost.all().prefetch_related(Prefetch("author", RtAuthor.all()))

    result = await orm.schema(query=Query).execute("{ posts { title byline } }")
    assert result.errors is None, result.errors
    assert {post["title"]: post["byline"] for post in result.data["posts"]} == {
        "p1": "by Alice",
        "p2": "anonymous",
        "draft": "by Alice",
        "edge": "by Alice",
    }
