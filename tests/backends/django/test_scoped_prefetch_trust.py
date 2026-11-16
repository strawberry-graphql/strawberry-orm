"""A scoped relation under a prefetch is only trusted if its own prefetch ran.

Django skips a prefetch whose relation is already cached, and a
``select_related`` in the query one level up - from that level's
``scope_rows``, an edge's ``scope=``, or the resolver's own ``Prefetch`` -
caches it unscoped. Charlie is hidden here, so his post has no author.
"""

import pytest
import strawberry
from django.db.models import Prefetch

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto
from tests.backends.django.models import Comment, Post, Tag, User


@pytest.fixture
def rows(seed):
    Comment.objects.create(
        id=4,
        body="Rusty",
        post=seed["posts"]["rust_adventures"],
        author=seed["users"]["bob"],
    )


def without_charlie(cls, queryset, info):
    return queryset.exclude(name="Charlie")


def run(schema, query):
    result = schema.execute_sync(query)
    assert result.errors is None, result.errors
    return result.data


COMMENTS = "{ comments { body post { title author { name } } } }"

ALICE = {"name": "Alice"}

COMMENTS_SEEN = {
    "comments": [
        {"body": "Nice post!", "post": {"title": "Hello World", "author": ALICE}},
        {"body": "Thanks!", "post": {"title": "Hello World", "author": ALICE}},
        {"body": "Great guide", "post": {"title": "GraphQL Guide", "author": ALICE}},
        {"body": "Rusty", "post": {"title": "Rust Adventures", "author": None}},
    ]
}


def comment_schema(post_scope=None, edge_scope=None):
    orm = StrawberryORM.for_django(warn_missing_scope=False)

    @orm.type(User)
    class UserType:
        name: auto

        scope_rows = classmethod(without_charlie)

    @orm.type(Post)
    class PostType:
        title: auto
        author: UserType | None

        if post_scope is not None:
            scope_rows = classmethod(lambda cls, qs, info: post_scope(qs))

    @orm.type(Comment)
    class CommentType:
        body: auto
        post: PostType | None = (
            orm.field.eager(scope=lambda qs, info: edge_scope(qs))
            if edge_scope is not None
            else orm.field.eager()
        )

    @strawberry.type
    class Query:
        @orm.field.lazy
        def comments(self, info: strawberry.Info) -> list[CommentType]:
            return Comment.objects.order_by("id")

    return orm.schema(query=Query)


class TestScopedPrefetchTrust:
    def test_a_scope_rows_select_related_does_not_skip_the_scope_under_it(self, rows):
        schema = comment_schema(post_scope=lambda qs: qs.select_related("author"))
        assert run(schema, COMMENTS) == COMMENTS_SEEN

    def test_a_scope_rows_select_related_of_everything_does_not_either(self, rows):
        schema = comment_schema(post_scope=lambda qs: qs.select_related())
        assert run(schema, COMMENTS) == COMMENTS_SEEN

    def test_an_edge_scope_select_related_does_not_skip_the_scope_under_it(self, rows):
        schema = comment_schema(edge_scope=lambda qs: qs.select_related("author"))
        assert run(schema, COMMENTS) == COMMENTS_SEEN

    def test_a_select_related_two_relations_deep_is_cut_where_the_scope_is(self, rows):
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(User)
        class UserType:
            name: auto

            scope_rows = classmethod(without_charlie)

        @orm.type(Post)
        class PostType:
            title: auto
            author: UserType | None

        @orm.type(Comment)
        class CommentType:
            body: auto
            author: UserType | None
            post: PostType

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.select_related("post__author").order_by("id")

        @orm.type(User)
        class Commenter:
            name: auto
            comments: list[CommentType]

        @strawberry.type
        class Query:
            @orm.field.lazy
            def users(self, info: strawberry.Info) -> list[Commenter]:
                return User.objects.filter(name="Bob")

        query = "{ users { name comments { body author { name } post { title author { name } } } } }"
        bob = {"name": "Bob"}
        assert run(orm.schema(query=Query), query) == {
            "users": [
                {
                    "name": "Bob",
                    "comments": [
                        {
                            "body": "Nice post!",
                            "author": bob,
                            "post": {"title": "Hello World", "author": ALICE},
                        },
                        {
                            "body": "Rusty",
                            "author": bob,
                            "post": {"title": "Rust Adventures", "author": None},
                        },
                    ],
                }
            ]
        }

    def test_a_using_path_through_an_unscoped_relation_keeps_the_scope(self, rows):
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(User)
        class UserType:
            name: auto

            scope_rows = classmethod(without_charlie)

        @orm.type(Post)
        class PostType:
            title: auto
            author: UserType | None

        @orm.type(Comment)
        class CommentType:
            body: auto
            post: PostType

            @orm.field.lazy(using=["post__author"])
            def headline(self, info: strawberry.Info) -> str:
                return self.post.title

        @strawberry.type
        class Query:
            @orm.field.lazy
            def comments(self, info: strawberry.Info) -> list[CommentType]:
                return Comment.objects.select_related("post__author").order_by("id")

        data = run(
            orm.schema(query=Query),
            "{ comments { body headline post { title author { name } } } }",
        )
        assert data == {
            "comments": [
                {**comment, "headline": comment["post"]["title"]}
                for comment in COMMENTS_SEEN["comments"]
            ]
        }

    def test_an_on_view_select_related_does_not_skip_the_scope_under_it(self, rows):
        orm = StrawberryORM.for_django(warn_missing_scope=False, lazy_resolution="off")

        @orm.type(User)
        class UserType:
            name: auto

            scope_rows = classmethod(without_charlie)

        @orm.type(Post)
        class PostType:
            title: auto
            author: UserType | None

        @orm.type(Tag)
        class TagType:
            name: auto
            tagged: list[PostType] = orm.field.eager(
                on="posts",
                scope=lambda qs, info: qs.select_related("author").order_by("id"),
            )

        @strawberry.type
        class Query:
            @orm.field.lazy
            def tags(self, info: strawberry.Info) -> list[TagType]:
                return Tag.objects.order_by("id")

        data = run(
            orm.schema(query=Query),
            "{ tags { name tagged { title author { name } } } }",
        )
        assert data == {
            "tags": [
                {
                    "name": "python",
                    "tagged": [
                        {"title": "Hello World", "author": ALICE},
                        {"title": "GraphQL Guide", "author": ALICE},
                    ],
                },
                {
                    "name": "graphql",
                    "tagged": [{"title": "GraphQL Guide", "author": ALICE}],
                },
                {
                    "name": "rust",
                    "tagged": [{"title": "Rust Adventures", "author": None}],
                },
            ]
        }

    def test_an_on_view_beside_a_scoped_relation_is_loaded_with_it(self, rows):
        orm = StrawberryORM.for_django(warn_missing_scope=False, lazy_resolution="off")

        @orm.type(User)
        class UserType:
            name: auto

            scope_rows = classmethod(without_charlie)

        @orm.type(Comment)
        class CommentType:
            body: auto
            author: UserType | None

        @orm.type(Post)
        class PostType:
            title: auto
            author: UserType | None
            remarks: list[CommentType] = orm.field.eager(
                on="comments", scope=lambda qs, info: qs.order_by("id")
            )

            @classmethod
            def scope_rows(cls, queryset, info):
                return queryset.select_related("author").order_by("id")

        @orm.type(Tag)
        class TagType:
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @orm.field.lazy
            def tags(self, info: strawberry.Info) -> list[TagType]:
                return Tag.objects.order_by("id")

        data = run(
            orm.schema(query=Query),
            "{ tags { name posts { title author { name } "
            "remarks { body author { name } } } } }",
        )
        bob = {"name": "Bob"}
        hello = {
            "title": "Hello World",
            "author": ALICE,
            "remarks": [
                {"body": "Nice post!", "author": bob},
                {"body": "Thanks!", "author": ALICE},
            ],
        }
        guide = {
            "title": "GraphQL Guide",
            "author": ALICE,
            "remarks": [{"body": "Great guide", "author": None}],
        }
        assert data == {
            "tags": [
                {"name": "python", "posts": [hello, guide]},
                {"name": "graphql", "posts": [guide]},
                {
                    "name": "rust",
                    "posts": [
                        {
                            "title": "Rust Adventures",
                            "author": None,
                            "remarks": [{"body": "Rusty", "author": bob}],
                        }
                    ],
                },
            ]
        }

    def test_a_relation_the_resolvers_prefetch_cached_is_checked_again(self, rows):
        orm = StrawberryORM.for_django(warn_missing_scope=False)

        @orm.type(User)
        class UserType:
            name: auto

            scope_rows = classmethod(without_charlie)

        @orm.type(Post)
        class PostType:
            title: auto
            author: UserType | None

        @orm.type(Tag)
        class TagType:
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @orm.field.lazy
            def tags(self, info: strawberry.Info) -> list[TagType]:
                return Tag.objects.order_by("id").prefetch_related(
                    Prefetch(
                        "posts",
                        queryset=Post.objects.select_related("author").order_by("id"),
                    )
                )

        data = run(
            orm.schema(query=Query), "{ tags { name posts { title author { name } } } }"
        )
        assert data == {
            "tags": [
                {
                    "name": "python",
                    "posts": [
                        {"title": "Hello World", "author": ALICE},
                        {"title": "GraphQL Guide", "author": ALICE},
                    ],
                },
                {
                    "name": "graphql",
                    "posts": [{"title": "GraphQL Guide", "author": ALICE}],
                },
                {
                    "name": "rust",
                    "posts": [{"title": "Rust Adventures", "author": None}],
                },
            ]
        }
