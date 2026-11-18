from dataclasses import dataclass
from types import SimpleNamespace

import pytest
from asgiref.sync import sync_to_async
from django.db import connection, models
from django.db.models import Prefetch

from strawberry_orm import StrawberryORM
from tests.abstract.relation_value_trust import AbstractTestRelationValueTrust


class RtAuthor(models.Model):
    name = models.CharField(max_length=50)
    is_public = models.BooleanField(default=False)

    class Meta:
        app_label = "testapp"
        db_table = "rt_author"


class RtPost(models.Model):
    title = models.CharField(max_length=50)
    is_published = models.BooleanField(default=False)
    author = models.ForeignKey(
        RtAuthor, null=True, on_delete=models.CASCADE, related_name="posts"
    )

    class Meta:
        app_label = "testapp"
        db_table = "rt_post"


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
    asynchronous: bool = False
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
    "async": Filler(asynchronous=True),
    "async_prefetch_object": Filler(root="prefetch_object", asynchronous=True),
    "async_prefetch_to_attr": Filler(root="prefetch_to_attr", asynchronous=True),
    "async_pinned": Filler(root="pinned", asynchronous=True),
    "async_pinned_twice": Filler(root="pinned", before=STAFF_FIRST, asynchronous=True),
    "async_optimizer_off": Filler(asynchronous=True, optimizer=False),
}


def _app_reads(kind):
    """The rows of *kind*, both relations loaded as the app's own reads load them."""
    if kind == "posts":
        return RtPost.objects.select_related("author").prefetch_related("author__posts")
    return RtAuthor.objects.prefetch_related("posts__author")


ROOTS = {
    "select_related": {
        "posts": lambda: RtPost.objects.select_related("author"),
        "authors": lambda: RtAuthor.objects.all(),
    },
    "prefetch_related": {
        "posts": lambda: RtPost.objects.prefetch_related("author__posts"),
        "authors": lambda: RtAuthor.objects.prefetch_related("posts__author"),
    },
    "prefetch_object": {
        "posts": lambda: RtPost.objects.prefetch_related(
            Prefetch("author", RtAuthor.objects.prefetch_related("posts"))
        ),
        "authors": lambda: RtAuthor.objects.prefetch_related(
            Prefetch("posts", RtPost.objects.select_related("author"))
        ),
    },
    "prefetch_to_attr": {
        "posts": lambda: RtPost.objects.prefetch_related(
            Prefetch("author", RtAuthor.objects.all(), to_attr="writer")
        ),
        "authors": lambda: RtAuthor.objects.prefetch_related(
            Prefetch("posts", RtPost.objects.all(), to_attr="every_post")
        ),
    },
}


@pytest.fixture(params=list(FILLERS))
def filler(request):
    return request.param


@pytest.fixture
def orm():
    return StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


class TestRelationValueTrust(AbstractTestRelationValueTrust):
    @pytest.fixture(autouse=True)
    def _rows(self, setup_tables):
        existing = connection.introspection.table_names()
        with connection.schema_editor() as editor:
            for model in (RtAuthor, RtPost):
                if model._meta.db_table not in existing:
                    editor.create_model(model)
        RtPost.objects.all().delete()
        RtAuthor.objects.all().delete()
        RtAuthor.objects.create(id=1, name="Alice", is_public=True)
        RtAuthor.objects.create(id=2, name="Hidden", is_public=False)
        for pk, title, published, author in [
            (1, "p1", True, 1),
            (2, "p2", True, 2),
            (3, "draft", False, 1),
            (4, "edge", True, 1),
        ]:
            RtPost.objects.create(
                id=pk, title=title, is_published=published, author_id=author
            )

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

    def refusal(self, filler):
        # Django will not prefetch one lookup with two querysets, and says so
        # naming the lookup; the client is told only that the read failed.
        if FILLERS[filler].root in ("prefetch_related", "prefetch_object"):
            return "Invalid filter expression"
        return None

    async def run(self, filler, schema, query):
        spec = FILLERS[filler]

        def context():
            if spec.root != "pinned":
                return {}
            return {
                "pinned": {
                    "posts": list(_app_reads("posts").exclude(title="draft")),
                    "authors": list(_app_reads("authors").filter(name="Alice")),
                }
            }

        if spec.asynchronous:
            ctx = await sync_to_async(context, thread_sensitive=True)()
            return await schema.execute(query, context_value=ctx)
        return await sync_to_async(
            lambda: schema.execute_sync(query, context_value=context()),
            thread_sensitive=True,
        )()

    @staticmethod
    def one(row, name):
        return getattr(row, name)

    @staticmethod
    def many(row, name):
        return list(getattr(row, name).all())

    @staticmethod
    def public(queryset):
        return queryset.filter(is_public=True)

    @staticmethod
    def published(queryset):
        return queryset.filter(is_published=True)

    @staticmethod
    def not_edge(queryset):
        return queryset.exclude(title="edge")
