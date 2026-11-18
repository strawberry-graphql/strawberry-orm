from typing import Any

import pytest
import strawberry
from django.db.models import FETCH_RAISE, Prefetch, QuerySet, Value
from django.db.models.expressions import RawSQL
from django.db.models.query import ModelIterable

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.batching_query_state import AbstractTestBatchingQueryState
from tests.backends.django.models import Comment, Post, User

QUERY = "{ users { name posts { title label shown writer } } }"


class WkPostProxy(Post):
    wk_label = "proxy"

    class Meta:
        app_label = "testapp"
        proxy = True


class WkLabelling(ModelIterable):
    def __iter__(self):
        for row in super().__iter__():
            row.wk_label = "iterable"
            yield row


class WkLabelledQuerySet(QuerySet):
    def _fetch_all(self):
        super()._fetch_all()
        for row in self._result_cache:
            row.wk_label = "queryset"


class WkPostQuerySet(QuerySet):
    def by(self, author_pk: int) -> QuerySet:
        return self.filter(author_id=author_pk)


class WkPrefetch(Prefetch):
    pass


class Opaque:
    """A query parameter whose repr does not show its value."""

    def __init__(self, value: Any) -> None:
        self.value = value

    def __repr__(self) -> str:
        return "Opaque()"


def posts(pk: int) -> QuerySet:
    return Post.objects.filter(author_id=pk)


def shown(mine: bool, viewer: int) -> Prefetch:
    """The author sees every comment on their posts; others only their own."""
    comments = (
        Comment.objects.all() if mine else Comment.objects.filter(author_id=viewer)
    )
    return Prefetch("comments", comments, to_attr="wk_shown")


def iterated(pk: int, mine: bool, viewer: int) -> QuerySet:
    query = posts(pk)
    if not mine:
        query._iterable_class = WkLabelling
    return query


def spoofed(pk: int, mine: bool, viewer: int) -> QuerySet:
    query = posts(pk)
    writer = User(pk=pk, name="mine" if mine else "theirs")
    query._known_related_objects = {Post._meta.get_field("author"): {pk: writer}}
    return query


def evaluated(pk: int, mine: bool, viewer: int) -> QuerySet:
    query = posts(pk)
    list(query)
    if not mine:
        query._result_cache = [row for row in query._result_cache if row.is_published]
    return query


def tagged(pk: int, mine: bool, viewer: int) -> QuerySet:
    query = posts(pk)
    query.wk_viewer = viewer if mine else None
    return query


def comments_read(pk: int, mine: bool, viewer: int) -> QuerySet:
    comments = Comment.objects.all()
    list(comments)
    if not mine:
        comments._result_cache = [row for row in comments if row.author_id == viewer]
    return posts(pk).prefetch_related(Prefetch("comments", comments, "wk_shown"))


def comments_labelled(pk: int, mine: bool, viewer: int) -> QuerySet:
    comments = Comment.objects.all()
    if not mine:
        comments._iterable_class = WkLabelling
    return posts(pk).prefetch_related(Prefetch("comments", comments, "wk_shown"))


BUILDS = {
    "prefetch_lookup": lambda pk, mine, viewer: posts(pk).prefetch_related(
        *(["comments"] if mine else [])
    ),
    "prefetch_to_attr": lambda pk, mine, viewer: posts(pk).prefetch_related(
        shown(mine, viewer)
    ),
    "prefetch_queryset_state": comments_labelled,
    "prefetch_evaluated_queryset": comments_read,
    "prefetch_class": lambda pk, mine, viewer: posts(pk).prefetch_related(
        (Prefetch if mine else WkPrefetch)("comments", to_attr="wk_shown")
    ),
    "select_related": lambda pk, mine, viewer: (
        posts(pk).select_related("author") if mine else posts(pk)
    ),
    "values": lambda pk, mine, viewer: posts(pk) if mine else posts(pk).values(),
    "values_list": lambda pk, mine, viewer: (
        posts(pk).values() if mine else posts(pk).values_list()
    ),
    "proxy_model": lambda pk, mine, viewer: (
        Post if mine else WkPostProxy
    ).objects.filter(author_id=pk),
    "queryset_class": lambda pk, mine, viewer: (
        posts(pk) if mine else WkLabelledQuerySet(Post).filter(author_id=pk)
    ),
    "iterable_class": iterated,
    "known_related_objects": spoofed,
    "result_cache": evaluated,
    "fetch_mode": lambda pk, mine, viewer: (
        posts(pk) if mine else posts(pk).fetch_mode(FETCH_RAISE)
    ),
    "database": lambda pk, mine, viewer: (
        posts(pk) if mine else posts(pk).using("replica")
    ),
    "annotation": lambda pk, mine, viewer: posts(pk).annotate(wk=Value(int(mine))),
    "extra": lambda pk, mine, viewer: posts(pk).extra(select={"wk": str(int(mine))}),
    "deferred": lambda pk, mine, viewer: posts(pk).defer("body") if mine else posts(pk),
    "opaque_param": lambda pk, mine, viewer: posts(pk).annotate(
        wk=RawSQL("%s", (Opaque(mine),))
    ),
    "app_state": tagged,
    # Siblings carry the same state.
    "plain": lambda pk, mine, viewer: posts(pk),
    "related_manager": lambda pk, mine, viewer: User.objects.get(pk=pk).posts.all(),
    "same_prefetch_to_attr": lambda pk, mine, viewer: posts(pk).prefetch_related(
        shown(False, viewer)
    ),
    "same_prefetch_lookups": lambda pk, mine, viewer: posts(pk).prefetch_related(
        "comments", "tags"
    ),
    "same_select_related": lambda pk, mine, viewer: posts(pk).select_related("author"),
    "same_annotation": lambda pk, mine, viewer: posts(pk).annotate(wk=Value(1)),
    "same_database": lambda pk, mine, viewer: posts(pk).using("default"),
    "queryset_class_adding_methods": lambda pk, mine, viewer: WkPostQuerySet(Post).by(
        pk
    ),
}

STATES = [
    "prefetch_lookup",
    "prefetch_to_attr",
    "prefetch_queryset_state",
    "prefetch_evaluated_queryset",
    "prefetch_class",
    "select_related",
    "values",
    "values_list",
    "proxy_model",
    "queryset_class",
    "iterable_class",
    "known_related_objects",
    "result_cache",
    "fetch_mode",
    "database",
    "annotation",
    "extra",
    "deferred",
    "opaque_param",
    "app_state",
]
SHARED = [
    "plain",
    "related_manager",
    "same_prefetch_to_attr",
    "same_prefetch_lookups",
    "same_select_related",
    "same_annotation",
    "same_database",
    "queryset_class_adding_methods",
]
OBSERVED = [
    "prefetch_to_attr",
    "values",
    "proxy_model",
    "queryset_class",
    "iterable_class",
    "known_related_objects",
    "plain",
    "related_manager",
    "same_prefetch_to_attr",
]


def _schema(build: Any, *, batch: bool) -> strawberry.Schema:
    orm = StrawberryORM.for_django(
        lazy_resolution="off", warn_missing_scope=False, batch_relations=batch
    )

    @orm.type(Post)
    class PostType:
        title: auto

        @strawberry.field
        def label(self) -> str:
            return getattr(self, "wk_label", "")

        @strawberry.field
        def shown(self) -> list[str]:
            return sorted(comment.body for comment in getattr(self, "wk_shown", []))

        @strawberry.field
        def writer(self) -> str:
            return self.author.name

    @orm.type(User)
    class UserType:
        name: auto

        @strawberry.field
        def posts(self, info: strawberry.Info) -> list[PostType]:
            viewer = info.context["viewer"]
            return build(self.pk, self.pk == viewer, viewer).order_by("id")

    @strawberry.type
    class Query:
        users: list[UserType] = orm.field.eager()

    return orm.schema(query=Query)


@pytest.fixture
def backend():
    return StrawberryORM.for_django(lazy_resolution="off")._backend


@pytest.fixture
def read(seed):
    Comment.objects.create(
        id=50,
        body="bob's note to charlie",
        post=seed["posts"]["rust_adventures"],
        author=seed["users"]["bob"],
    )

    def run(state: str, viewer: int, *, batch: bool) -> Any:
        result = _schema(BUILDS[state], batch=batch).execute_sync(
            QUERY, context_value={"viewer": viewer}
        )
        return result.data, sorted(error.message for error in result.errors or [])

    return run


@pytest.fixture(params=STATES)
def state(request):
    return request.param


@pytest.fixture(params=SHARED)
def shared(request):
    return request.param


@pytest.fixture(params=OBSERVED)
def observed(request):
    return request.param


class TestBatchingQueryState(AbstractTestBatchingQueryState):
    builds = BUILDS


def test_charlies_posts_are_not_read_with_alices_prefetch(read):
    """Alice reads Charlie's posts with the comments she wrote, which leaves
    out Bob's note to Charlie."""
    data, errors = read("prefetch_to_attr", 1, batch=True)
    assert errors == []
    charlie = data["users"][2]
    assert charlie["name"] == "Charlie"
    assert [post["shown"] for post in charlie["posts"]] == [[]]
