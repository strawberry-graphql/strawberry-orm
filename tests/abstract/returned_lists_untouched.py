"""The lists a resolver hands back are the app's: hiding rows never changes them.

Rows a resolver already read reach the library as a list, and the ones
``scope_rows`` rejects are left out of what the client sees. The list is the
app's. It may be a relation's own collection, which an ORM records every
change to against the row holding it, or one the app hands every request.
Changing it, or a copy still bound to what it is bound to, hides the rows
from the next request, whose scope may allow them, or writes the change
back: under SQLAlchemy, posts taken out of a user's collection have their
author set to NULL at the next flush.

Each way rows reach a list field - a relation's own collection, a list of
them, a connection over one, a list the app keeps at the root - is crossed
with the optimizer on and off. After each query every list holds what it
held, and the database holds what it held once the session is written.

A backend provides ``orm`` and ``models`` (``User``, ``Post``), and on the
test class:

- ``run(schema, query)``;
- ``load_users()``: every user with ``posts`` loaded, in id order;
- ``collection(user)``: the list the ORM holds for ``user.posts``;
- ``published_only(qs)``;
- ``written()``: each post's author id by title, read from the database once
  what the session holds is written.

Seed: Alice wrote "Hello World" and "GraphQL Guide", Bob the unpublished
"Draft Post", Charlie "Rust Adventures".
"""

import pytest
import strawberry

from strawberry_orm import auto
from strawberry_orm.relay import ORMListConnection

SHOWN = {
    "Alice": ["GraphQL Guide", "Hello World"],
    "Bob": [],
    "Charlie": ["Rust Adventures"],
}

PLACES = {
    "collection": ("users", "own { title }"),
    "list of collections": ("users", "grouped { title }"),
    "connection": ("users", "page { edges { node { title } } }"),
    "kept root list": ("posts", "title"),
}


def _titles(rows):
    return sorted(row.title for row in rows)


def _shown(data, root):
    if root == "posts":
        return sorted(row["title"] for row in data["posts"])
    shown = {}
    for user in data["users"]:
        if "own" in user:
            rows = user["own"]
        elif "grouped" in user:
            rows = [row for group in user["grouped"] for row in group]
        else:
            rows = [edge["node"] for edge in user["page"]["edges"]]
        shown[user["name"]] = sorted(row["title"] for row in rows)
    return shown


class AbstractTestReturnedListsUntouched:
    async def schema(self, orm, models, users, optimizer):
        collection, published_only = self.collection, self.published_only
        kept_posts = [post for user in users for post in collection(user)]

        @orm.type(models.Post)
        class PostType:
            title: auto

            @classmethod
            def scope_rows(cls, qs, info):
                return published_only(qs)

        def page_rows(root, info: strawberry.Info) -> list[PostType]:
            return collection(root)

        @orm.type(models.User)
        class UserType:
            name: auto

            @strawberry.field
            def own(self) -> list[PostType]:
                return collection(self)

            @strawberry.field
            def grouped(self) -> list[list[PostType]]:
                return [collection(self)]

            page: ORMListConnection[PostType] = orm.connection(
                ORMListConnection[PostType], resolver=page_rows
            )

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self) -> list[UserType]:
                return users

            @strawberry.field
            def posts(self) -> list[PostType]:
                return kept_posts

        return orm.schema(query=Query, optimizer=optimizer)

    @pytest.mark.parametrize("optimizer", [True, False], ids=["optimizer", "off"])
    @pytest.mark.parametrize("place", list(PLACES))
    async def test_hiding_rows_leaves_every_list_as_it_was(
        self, orm, models, place, optimizer
    ):
        users = await self.load_users()
        held = {user.name: _titles(self.collection(user)) for user in users}
        written = await self.written()
        schema = await self.schema(orm, models, users, optimizer)
        root, selection = PLACES[place]
        key = "" if root == "posts" else "name"

        result = await self.run(schema, f"{{ {root} {{ {key} {selection} }} }}")

        assert result.errors is None, result.errors
        expected = (
            sorted(title for titles in SHOWN.values() for title in titles)
            if root == "posts"
            else SHOWN
        )
        assert _shown(result.data, root) == expected
        assert {user.name: _titles(self.collection(user)) for user in users} == held
        assert await self.written() == written
