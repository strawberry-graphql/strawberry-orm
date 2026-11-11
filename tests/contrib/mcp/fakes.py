"""A small schema and an operations document for the MCP plugin's tests."""

from __future__ import annotations

import datetime
import enum
from typing import Annotated

import strawberry


@strawberry.enum(description="Where a post is in its life.")
class Status(enum.Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


@strawberry.interface
class Named:
    name: str


@strawberry.type(description="Someone who writes posts.")
class Author(Named):
    id: strawberry.ID


@strawberry.type
class Post:
    title: str = strawberry.field(description="The headline.")
    status: Status
    published: datetime.date | None
    author: Author

    @strawberry.field
    def secret(self) -> str | None:
        raise PermissionError("You may not read secrets.")


@strawberry.type
class Tag:
    label: str
    uses: int
    pinned: Post


SearchResult = Annotated[Post | Tag, strawberry.union("SearchResult")]


@strawberry.input(description="Which posts to return.")
class PostFilter:
    title: str | None = None
    status: Status | None = strawberry.UNSET
    drafts: bool = strawberry.field(default=False, description="Include drafts.")
    any_of: list[PostFilter] | None = strawberry.UNSET


@strawberry.input(one_of=True)
class PostKey:
    id: strawberry.ID | None = strawberry.UNSET
    title: str | None = strawberry.UNSET


@strawberry.input
class NewTag:
    label: str
    uses: int = 0


ALICE = Author(id=strawberry.ID("1"), name="Alice")
POSTS = [
    Post(
        title="Hello",
        status=Status.PUBLISHED,
        published=datetime.date(2026, 1, 2),
        author=ALICE,
    ),
    Post(title="Draft", status=Status.DRAFT, published=None, author=ALICE),
]


def _matches(post: Post, where: PostFilter | None) -> bool:
    if where is None:
        return post.status is Status.PUBLISHED
    if where.title is not None and post.title != where.title:
        return False
    return where.drafts or post.status is Status.PUBLISHED


@strawberry.type
class Query:
    @strawberry.field
    def posts(
        self, info: strawberry.Info, filter: PostFilter | None = None, first: int = 10
    ) -> list[Post]:
        context = info.context
        if isinstance(context, dict):
            context.setdefault("seen", []).append(context.get("user"))
        return [post for post in POSTS if _matches(post, filter)][:first]

    @strawberry.field
    def posts_on(self, day: datetime.date) -> list[Post]:
        return [post for post in POSTS if post.published == day]

    @strawberry.field
    def post(self, key: PostKey) -> Post | None:
        return next((p for p in POSTS if key.title in (None, p.title)), None)

    @strawberry.field
    def named(self) -> Named:
        return ALICE

    @strawberry.field
    def search(self, text: str) -> list[SearchResult]:
        return [POSTS[0], Tag(label=text, uses=3, pinned=POSTS[0])]

    @strawberry.field
    def missing(self) -> str:
        raise LookupError("No such thing.")


RUNS = {"publish": 0}


@strawberry.type
class Mutation:
    @strawberry.mutation(description="Publish a post.")
    def publish(self, title: str) -> Post:
        RUNS["publish"] += 1
        return Post(title=title, status=Status.PUBLISHED, published=None, author=ALICE)

    @strawberry.mutation
    def tag(self, input: NewTag) -> Tag:
        return Tag(label=input.label, uses=input.uses, pinned=POSTS[0])


schema = strawberry.Schema(query=Query, mutation=Mutation)

OPERATIONS = '''
"""Published posts, or drafts too when asked."""
query ListPosts(
  """Only posts matching this."""
  $filter: PostFilter
  $first: Int = 10
) {
  posts(filter: $filter, first: $first) {
    ...PostFields
    author { name }
  }
}

query FindPost($key: PostKey!, $withStatus: Boolean!) {
  post(key: $key) {
    title
    status @include(if: $withStatus)
  }
}

query Search($text: String!) {
  search(text: $text) {
    __typename
    ... on Post { title }
    ... on Tag { label }
  }
}

query WhoIsNamed {
  named {
    name
    ... on Author { id }
  }
}

query Secrets {
  posts { title secret }
}

query Missing {
  missing
}

fragment PostFields on Post {
  title
  status
  published
}

"""Publish a post under this title."""
mutation Publish($title: String!) {
  publish(title: $title) { title }
}
'''
