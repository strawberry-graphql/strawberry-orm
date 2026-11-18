"""A relation's rows reach a reader only as this execution's scoped load left them.

A relation on a row can be filled in many ways before anything reads it: by
the library's own scoped load, by the mapper (``lazy="joined"``), by the app's
resolver (``selectinload``, ``prefetch_related``, a ``Prefetch``), by a sibling
root field reading the same rows, by an earlier selection of a row the app
keeps, or by nothing at all. Whichever it was, a relation field returns, and a
``using=`` resolver reads, only the rows the related type's ``scope_rows`` and
the edge's ``scope=`` allow.

A backend provides ``orm`` (a fresh ORM for the filler) and on the test class:

- ``filler``, a fixture naming how the relations get filled;
- ``models_for(filler)``: ``Author`` and ``Post``;
- ``root(filler, models, kind)``: the body of the root field *kind* (``posts``,
  ``authors``, ``warm_posts``, ``warm_authors``) as a callable taking ``info``,
  or ``None`` for the library's own field;
- ``around(filler)``: root selections run before and after the reader's;
- ``optimizer(filler)``: whether the schema runs the optimizer;
- ``run(filler, schema, query)``, executing against the seeded rows;
- ``one(row, name)`` and ``many(row, name)``: a relation read the way an app's
  resolver reads it, the related row or a list of them, or an awaitable of it;
- ``public(q)``, ``published(q)`` and ``not_edge(q)``: the scopes;
- optionally ``refusal(filler)``: what an error says when the backend refuses
  the filler's own loads outright, which shows the client nothing.

Seed: Alice (public) wrote "p1", "draft" (unpublished) and "edge", which the
``scope=`` on ``AuthorType.posts`` hides; Hidden (not public) wrote "p2".
"""

from collections.abc import Iterable
from inspect import isawaitable

import pytest
import strawberry
from strawberry import relay

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

ALICE = {"name": "Alice"}
ALICE_POSTS = [{"title": "p1"}]

#: What each root row shows, by root field, for each selection a reader makes.
READERS = {
    "to_one": (
        "posts",
        "author { name }",
        {"p1": {"author": ALICE}, "p2": {"author": None}, "edge": {"author": ALICE}},
    ),
    "using_to_one": (
        "posts",
        "byline",
        {
            "p1": {"byline": "by Alice"},
            "p2": {"byline": "anonymous"},
            "edge": {"byline": "by Alice"},
        },
    ),
    "lazy_using_to_one": (
        "posts",
        "lazyByline",
        {
            "p1": {"lazyByline": "by Alice"},
            "p2": {"lazyByline": "anonymous"},
            "edge": {"lazyByline": "by Alice"},
        },
    ),
    "using_two_hops": (
        "posts",
        "colleagues",
        {
            "p1": {"colleagues": ["p1"]},
            "p2": {"colleagues": []},
            "edge": {"colleagues": ["p1"]},
        },
    ),
    "to_many_below_to_one": (
        "posts",
        "author { posts { title } }",
        {
            "p1": {"author": {"posts": ALICE_POSTS}},
            "p2": {"author": None},
            "edge": {"author": {"posts": ALICE_POSTS}},
        },
    ),
    "using_below_to_one": (
        "posts",
        "author { titles }",
        {
            "p1": {"author": {"titles": ["p1"]}},
            "p2": {"author": None},
            "edge": {"author": {"titles": ["p1"]}},
        },
    ),
    # "p1" and "edge" share Alice, whose readers of one relation run side by
    # side: each must see the scoped rows, and none keep the others waiting.
    "usings_below_shared_to_one": (
        "posts",
        "author { titles lazyTitles posts { title } }",
        {
            "p1": {
                "author": {
                    "titles": ["p1"],
                    "lazyTitles": ["p1"],
                    "posts": ALICE_POSTS,
                }
            },
            "p2": {"author": None},
            "edge": {
                "author": {
                    "titles": ["p1"],
                    "lazyTitles": ["p1"],
                    "posts": ALICE_POSTS,
                }
            },
        },
    ),
    # A model property, read by Strawberry off the row, behind the deprecated
    # ``orm.field.auto(using=...)``.
    "property_using_to_one": (
        "posts",
        "propByline",
        {
            "p1": {"propByline": "by Alice"},
            "p2": {"propByline": "anonymous"},
            "edge": {"propByline": "by Alice"},
        },
    ),
    "property_using_below_to_one": (
        "posts",
        "author { propTitles }",
        {
            "p1": {"author": {"propTitles": ["p1"]}},
            "p2": {"author": None},
            "edge": {"author": {"propTitles": ["p1"]}},
        },
    ),
    "to_many": ("authors", "posts { title }", {"Alice": {"posts": ALICE_POSTS}}),
    "using_to_many": ("authors", "titles", {"Alice": {"titles": ["p1"]}}),
    "property_using_to_many": (
        "authors",
        "propTitles",
        {"Alice": {"propTitles": ["p1"]}},
    ),
    "lazy_using_to_many": ("authors", "lazyTitles", {"Alice": {"lazyTitles": ["p1"]}}),
    "using_below_to_many": (
        "authors",
        "posts { title fellows }",
        {"Alice": {"posts": [{"title": "p1", "fellows": ["p1"]}]}},
    ),
    "using_below_ordered": (
        "authors",
        "posts(order: [{field: {title: DESC}}]) { title fellows }",
        {"Alice": {"posts": [{"title": "p1", "fellows": ["p1"]}]}},
    ),
    "filtered": (
        "authors",
        'posts(filter: {field: {title: {contains: "d"}}}) { title }',
        {"Alice": {"posts": []}},
    ),
    "connection_to_one": (
        "pages",
        "author { name }",
        {"p1": {"author": ALICE}, "p2": {"author": None}, "edge": {"author": ALICE}},
    ),
    "connection_using_to_one": (
        "pages",
        "byline",
        {
            "p1": {"byline": "by Alice"},
            "p2": {"byline": "anonymous"},
            "edge": {"byline": "by Alice"},
        },
    ),
    "connection_using_two_hops": (
        "pages",
        "colleagues",
        {
            "p1": {"colleagues": ["p1"]},
            "p2": {"colleagues": []},
            "edge": {"colleagues": ["p1"]},
        },
    ),
    "connection_property_using_to_one": (
        "pages",
        "propByline",
        {
            "p1": {"propByline": "by Alice"},
            "p2": {"propByline": "anonymous"},
            "edge": {"propByline": "by Alice"},
        },
    ),
}


def then(value, use):
    """``use(value)``, awaiting *value* and what *use* returns as needed."""
    if not isawaitable(value):
        return use(value)

    async def later():
        result = use(await value)
        while isawaitable(result):
            result = await result
        return result

    return later()


def _titles(rows):
    return [row.title for row in sorted(rows, key=lambda row: row.id)]


def _shown(data, root):
    """Each root row's fields, less the one naming it, by that name."""
    if root == "pages":
        rows = [edge["node"] for edge in data["pages"]["edges"]]
    else:
        rows = data[root]
    key = "name" if root == "authors" else "title"
    return {row[key]: {k: v for k, v in row.items() if k != key} for row in rows}


class AbstractTestRelationValueTrust:
    def schema(self, orm, filler):
        models = self.models_for(filler)
        Author, Post = models.Author, models.Post
        one, many = self.one, self.many
        public, published, not_edge = self.public, self.published, self.not_edge
        PostFilter = orm.filter(Post)
        PostOrder = orm.order(Post)

        def byline(author):
            return "anonymous" if author is None else f"by {author.name}"

        Author.prop_titles = property(lambda row: then(many(row, "posts"), _titles))
        Post.prop_byline = property(lambda row: then(one(row, "author"), byline))

        @orm.type(Post, name="RtPostLeaf", filters=PostFilter, order=PostOrder)
        class PostLeafType:
            title: auto

            # No author field: the hop's type is whichever type reads authors.
            @orm.field.computed(using=["author.posts"])
            def fellows(self) -> list[str]:
                return then(
                    one(self, "author"),
                    lambda author: (
                        [] if author is None else then(many(author, "posts"), _titles)
                    ),
                )

        @orm.type(Author, name="RtAuthor")
        class AuthorType:
            name: auto
            posts: list[PostLeafType] = orm.field.eager(
                scope=lambda qs, info: not_edge(qs)
            )

            @orm.field.computed(using=["posts"])
            def titles(self) -> list[str]:
                return then(many(self, "posts"), _titles)

            @orm.field.lazy(using=["posts"])
            def lazy_titles(self, info: strawberry.Info) -> list[str]:
                return then(many(self, "posts"), _titles)

            prop_titles: list[str] = orm.field.auto(using=["posts"])

            @classmethod
            def scope_rows(cls, qs, info):
                return public(qs)

        @orm.type(Post, name="RtPost")
        class PostType(relay.Node):
            id: relay.NodeID[int]
            title: auto
            author: AuthorType | None
            prop_byline: str = orm.field.auto(using=["author"])

            @orm.field.computed(using=["author"])
            def byline(self) -> str:
                return then(one(self, "author"), byline)

            @orm.field.lazy(using=["author"])
            def lazy_byline(self, info: strawberry.Info) -> str:
                return then(one(self, "author"), byline)

            @orm.field.computed(using=["author.posts"])
            def colleagues(self) -> list[str]:
                return then(
                    one(self, "author"),
                    lambda author: (
                        [] if author is None else then(many(author, "posts"), _titles)
                    ),
                )

            @classmethod
            def scope_rows(cls, qs, info):
                return published(qs)

        # Over the same models with no edge scope: what it loads, a stricter
        # field must not take.
        @orm.type(Author, name="RtStaffAuthor")
        class StaffAuthorType:
            name: auto
            posts: list[PostLeafType]

        @orm.type(Post, name="RtStaffPost")
        class StaffPostType:
            title: auto
            author: StaffAuthorType | None

        returned = {
            "posts": list[PostType],
            "authors": list[AuthorType],
            "staff_posts": list[StaffPostType],
            "staff_authors": list[StaffAuthorType],
            "warm_posts": list[PostType],
            "warm_authors": list[AuthorType],
        }
        rooted = {"staff_posts": "posts", "staff_authors": "authors"}
        namespace: dict = {"__annotations__": {}}
        for name, annotation in returned.items():
            body = self.root(filler, models, rooted.get(name, name))
            namespace["__annotations__"][name] = annotation
            namespace[name] = self._root_field(orm, body, annotation)
        namespace["__annotations__"]["pages"] = ORMListConnection[PostType]
        body = self.root(filler, models, "posts")
        if body is None:
            namespace["pages"] = orm.connection()
        else:

            def pages(info: strawberry.Info) -> Iterable[PostType]:
                return body(info)

            namespace["pages"] = orm.connection(
                ORMListConnection[PostType], resolver=pages
            )
        Query = strawberry.type(type("Query", (), namespace))
        return orm.schema(query=Query, optimizer=self.optimizer(filler))

    def refusal(self, filler):
        """What the backend says when it refuses the filler's loads, if it may."""
        return None

    @staticmethod
    def _root_field(orm, body, annotation):
        if body is None:
            return orm.field.eager()

        def resolver(self, info: strawberry.Info):
            return body(info)

        resolver.__annotations__["return"] = annotation
        return orm.field.lazy(resolver)

    @pytest.mark.parametrize("reader", list(READERS))
    async def test_a_reader_sees_only_rows_the_scopes_allow(self, orm, filler, reader):
        root, selection, expected = READERS[reader]
        before, after = self.around(filler)
        if root == "pages":
            inner = f"pages {{ edges {{ node {{ title {selection} }} }} }}"
        else:
            key = "name" if root == "authors" else "title"
            inner = f"{root} {{ {key} {selection} }}"
        query = f"{{ {before} {inner} {after} }}"
        result = await self.run(filler, self.schema(orm, filler), query)
        refusal = self.refusal(filler)
        if result.errors and refusal is not None:
            assert all(refusal in str(error) for error in result.errors), result.errors
            return
        assert result.errors is None, result.errors
        shown = _shown(result.data, root)
        assert shown == expected, shown
