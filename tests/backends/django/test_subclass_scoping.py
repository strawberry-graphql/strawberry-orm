"""Rows of a proxy or multi-table child model are scoped by their parent's type.

A ``FeaturedPost`` proxy reads the very rows ``Post`` does, and a
``PinnedArticle`` is an article with a table of its own besides. Returned for
the parent's type, both are rows of the parent, so its ``scope_rows`` has to
apply to them.
"""

import pytest
import strawberry
from django.db import connection, models

from strawberry_orm import AbstractRepo, StrawberryORM
from strawberry_orm.types import auto
from tests.abstract.mutation_node_input_relations import _schema, shape
from tests.backends.django.models import Post, User


class FeaturedPost(Post):
    class Meta:
        proxy = True
        app_label = "testapp"


class FeaturedUser(User):
    class Meta:
        proxy = True
        app_label = "testapp"


class Article(models.Model):
    title = models.CharField(max_length=100)
    is_published = models.BooleanField(default=False)

    class Meta:
        app_label = "testapp"
        db_table = "test_subclass_article"


class PinnedArticle(Article):
    note = models.CharField(max_length=100, default="")

    class Meta:
        app_label = "testapp"
        db_table = "test_subclass_pinned_article"


PUBLISHED = ["GraphQL Guide", "Hello World", "Rust Adventures"]


@pytest.fixture
def articles(setup_tables):
    existing = connection.introspection.table_names()
    with connection.schema_editor() as editor:
        for model in (Article, PinnedArticle):
            if model._meta.db_table not in existing:
                editor.create_model(model)
    PinnedArticle.objects.all().delete()
    Article.objects.all().delete()
    Article.objects.create(id=1, title="Plain draft")
    PinnedArticle.objects.create(id=2, title="Pinned draft")
    PinnedArticle.objects.create(id=3, title="Pinned article", is_published=True)


def published_only(queryset, info):
    return queryset.filter(is_published=True)


def build():
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(Post)
    class PostType:
        id: auto
        title: auto

        scope_rows = classmethod(lambda cls, qs, info: published_only(qs, info))

    @orm.type(Article)
    class ArticleType:
        id: auto
        title: auto

        scope_rows = classmethod(lambda cls, qs, info: published_only(qs, info))

    @strawberry.type
    class Query:
        @strawberry.field
        def featured(self, id: int) -> PostType | None:
            return FeaturedPost.objects.filter(pk=id).first()

        @strawberry.field
        def featured_rows(self) -> list[PostType]:
            return list(FeaturedPost.objects.order_by("id"))

        @orm.field.lazy
        def featured_query(self, info: strawberry.Info) -> list[PostType]:
            return FeaturedPost.objects.order_by("id")

        @strawberry.field
        def pinned(self, id: int) -> ArticleType | None:
            return PinnedArticle.objects.filter(pk=id).first()

        @orm.field.lazy
        def pinned_query(self, info: strawberry.Info) -> list[ArticleType]:
            return PinnedArticle.objects.order_by("id")

    return orm.schema(query=Query)


def build_with_a_relation_scope():
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(Post, filters=orm.filter(Post))
    class PostType:
        id: auto
        title: auto

    @orm.type(User, filters=orm.filter(User))
    class UserType:
        id: auto
        name: auto
        posts: list[PostType] = orm.field.auto(scope=published_only)

    @strawberry.type
    class Query:
        @strawberry.field
        def featured_users(self) -> list[UserType]:
            return list(FeaturedUser.objects.order_by("id"))

        @orm.field.lazy
        def featured_user_query(self, info: strawberry.Info) -> list[UserType]:
            return FeaturedUser.objects.order_by("id")

        @orm.field.auto()
        def featured_user_filter(self) -> list[UserType]:
            return FeaturedUser.objects.order_by("id")

    return orm.schema(query=Query)


def run(schema, query):
    result = schema.execute_sync(query)
    assert result.errors is None, result.errors
    return result.data


def titles(rows):
    return sorted(row["title"] for row in rows)


def test_a_proxy_row_is_scoped_by_its_concrete_models_type(seed):
    schema = build()
    draft = seed["posts"]["draft"].pk
    assert run(schema, f"{{ featured(id: {draft}) {{ title }} }}") == {"featured": None}
    for field in ("featuredRows", "featuredQuery"):
        assert titles(run(schema, f"{{ {field} {{ title }} }}")[field]) == PUBLISHED


def test_a_multi_table_child_is_scoped_by_its_parents_type(articles):
    schema = build()
    assert run(schema, "{ pinned(id: 2) { title } }") == {"pinned": None}
    assert run(schema, "{ pinned(id: 3) { title } }") == {
        "pinned": {"title": "Pinned article"}
    }
    data = run(schema, "{ pinnedQuery { title } }")
    assert titles(data["pinnedQuery"]) == ["Pinned article"]


def test_a_proxy_rows_relation_keeps_the_scope_its_type_gives_it(seed):
    schema = build_with_a_relation_scope()
    for field in ("featuredUsers", "featuredUserQuery"):
        users = run(schema, f"{{ {field} {{ name posts {{ title }} }} }}")[field]
        assert {user["name"]: titles(user["posts"]) for user in users} == {
            "Alice": ["GraphQL Guide", "Hello World"],
            "Bob": [],
            "Charlie": ["Rust Adventures"],
        }, field


def test_a_filter_over_proxy_rows_keeps_the_relation_scope(seed):
    schema = build_with_a_relation_scope()
    by_title = (
        '{ featuredUserFilter(filter: {object: {posts: {field: {title: {exact: "%s"}}}}})'
        " { name } }"
    )
    assert run(schema, by_title % "Draft Post") == {"featuredUserFilter": []}
    assert run(schema, by_title % "Hello World") == {
        "featuredUserFilter": [{"name": "Alice"}]
    }


class PostRepo(AbstractRepo[Post]):
    pass


class ArticleRepo(AbstractRepo[Article]):
    def scope_query(self, query, info):
        return query.filter(is_published=True)


def test_a_proxy_or_child_model_takes_its_parents_repo():
    class PinnedArticleRepo(ArticleRepo):
        pass

    inherited = StrawberryORM.for_django(
        repos={Post: PostRepo, Article: ArticleRepo}
    )._backend
    assert type(inherited.get_repo(FeaturedPost)) is PostRepo
    assert type(inherited.get_repo(PinnedArticle)) is ArticleRepo
    assert inherited.get_repo(User) is None

    explicit = StrawberryORM.for_django(
        repos={Article: ArticleRepo, PinnedArticle: PinnedArticleRepo}
    )._backend
    assert type(explicit.get_repo(PinnedArticle)) is PinnedArticleRepo


def test_a_child_models_row_is_found_for_a_write_through_its_parents_repo(articles):
    orm = StrawberryORM.for_django(
        warn_missing_scope=False, repos={Article: ArticleRepo}
    )

    @orm.type(PinnedArticle)
    class PinnedType:
        id: auto
        note: auto

    upsert = orm.mutations.upsert_node_input(
        model=PinnedArticle,
        project={
            "_meta": {
                "create": ["title"],
                "update": ["note"],
                "upsert": {"where": ["title"], "onMatch": "UPDATE"},
            }
        },
        name="PinnedUpsert",
    )

    @strawberry.type
    class Query:
        ok: bool = True

    @strawberry.type
    class Mutation:
        @strawberry.mutation
        def pin(self, info: strawberry.Info, input: upsert) -> PinnedType:
            return orm.apply_upsert(input, info)

    schema = orm.schema(query=Query, mutation=Mutation)
    edit = 'mutation { pin(input: {where: {title: "%s"}, update: {note: "edited"}}) { note } }'
    assert run(schema, edit % "Pinned article") == {"pin": {"note": "edited"}}
    hidden = schema.execute_sync(edit % "Pinned draft")
    assert hidden.data is None
    assert [error.message for error in hidden.errors or ()] == [
        "No PinnedArticle matches {'title': 'Pinned draft'} and no create was sent"
    ]
    assert dict(PinnedArticle.objects.values_list("title", "note")) == {
        "Pinned draft": "",
        "Pinned article": "edited",
    }


def test_inputs_for_a_child_model_leave_out_what_its_parents_type_hides():
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(Article, exclude=["is_published"])
    class ArticleType:
        id: auto
        title: auto

    schema = _schema(
        create=orm.mutations.create_node_input(models=[PinnedArticle], name="Create")
    )
    assert "isPublished" not in shape(schema, "CreatePinnedArticleNodeInput")
    with pytest.raises(ValueError, match=r"can write \['is_published'\]"):
        orm.input(PinnedArticle, name="PinnedInput")
