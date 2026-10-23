"""Forward-FK query shapes that exercise nested author traversal paths."""

import pytest
import strawberry

from strawberry_orm import StrawberryORM
from strawberry_orm.types import auto


class TestQueryForwardFKRuntime:
    def _build_schema(self, User, Post, *, author_scope_rows=None):
        orm = StrawberryORM.for_sqlalchemy(dialect="sqlite")

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto
            email: auto

            if author_scope_rows is not None:

                @classmethod
                def scope_rows(cls, qs, info):
                    return author_scope_rows(qs, info)

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            users: list[UserType] = orm.field.auto()
            posts: list[PostType] = orm.field.auto()

        return strawberry.Schema(query=Query, extensions=[orm.optimizer_extension()])

    def test_root_posts_can_select_forward_fk(self, sa_session, seed, User, Post):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            """
            {
                posts {
                    title
                    author { name }
                }
            }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "posts": [
                {"title": "Hello World", "author": {"name": "Alice"}},
                {"title": "GraphQL Guide", "author": {"name": "Alice"}},
                {"title": "Draft Post", "author": {"name": "Bob"}},
                {"title": "Rust Adventures", "author": {"name": "Charlie"}},
            ]
        }

    def test_prefetched_reverse_relations_can_select_nested_forward_fk(
        self, sa_session, seed, User, Post
    ):
        schema = self._build_schema(User, Post)
        result = schema.execute_sync(
            """
            {
                users {
                    name
                    posts {
                        title
                        author { name }
                    }
                }
            }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "users": [
                {
                    "name": "Alice",
                    "posts": [
                        {"title": "Hello World", "author": {"name": "Alice"}},
                        {"title": "GraphQL Guide", "author": {"name": "Alice"}},
                    ],
                },
                {
                    "name": "Bob",
                    "posts": [{"title": "Draft Post", "author": {"name": "Bob"}}],
                },
                {
                    "name": "Charlie",
                    "posts": [
                        {"title": "Rust Adventures", "author": {"name": "Charlie"}}
                    ],
                },
            ]
        }

    def test_forward_fk_respects_type_level_scope_rows(
        self, sa_session, seed, User, Post
    ):
        schema = self._build_schema(
            User,
            Post,
            author_scope_rows=lambda qs, info: qs.filter(User.email.contains("@")),
        )
        result = schema.execute_sync(
            """
            {
                posts {
                    title
                    author { name email }
                }
            }
            """,
            context_value={"session": sa_session},
        )
        assert result.errors is None
        assert result.data == {
            "posts": [
                {
                    "title": "Hello World",
                    "author": {"name": "Alice", "email": "alice@example.com"},
                },
                {
                    "title": "GraphQL Guide",
                    "author": {"name": "Alice", "email": "alice@example.com"},
                },
                {
                    "title": "Draft Post",
                    "author": {"name": "Bob", "email": "bob@example.com"},
                },
                {
                    "title": "Rust Adventures",
                    "author": {"name": "Charlie", "email": "charlie@test.org"},
                },
            ]
        }


class TestForwardFKScopingOnMaterializedParents:
    """A to-one relation must be scoped however the parent rows arrived.

    Returning rows instead of a query must not turn off ``scope_rows`` on the
    other end of the relation.
    """

    def _build_schema(self, session, User, Post, *, materialize):
        from sqlalchemy import select

        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(User.name == "Alice")

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                stmt = select(Post).order_by(Post.id)
                if materialize:
                    return list(session.execute(stmt).unique().scalars().all())
                return stmt

        return orm.schema(query=Query)

    def _run(self, session, User, Post, *, materialize):
        # A warm identity map would serve an author loaded by an earlier run.
        session.expunge_all()
        schema = self._build_schema(session, User, Post, materialize=materialize)
        result = schema.execute_sync(
            "{ posts { title author { name } } }",
            context_value={"session": session},
        )
        assert result.errors is None, result.errors
        return result.data["posts"]

    def test_materialized_parents_do_not_bypass_to_one_scoping(
        self, sa_session, seed, User, Post
    ):
        materialized = self._run(sa_session, User, Post, materialize=True)
        lazy = self._run(sa_session, User, Post, materialize=False)
        assert materialized == lazy
        assert [row["author"] for row in materialized] == [
            {"name": "Alice"},
            {"name": "Alice"},
            None,
            None,
        ]

    def test_a_scoped_to_one_is_reapplied_per_row_without_the_optimizer(
        self, sa_session, seed, User, Post
    ):
        """With no eager load to scope, the scope runs as each row is read."""
        from sqlalchemy import select

        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(User.name == "Alice")

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                stmt = select(Post).order_by(Post.id)
                return list(sa_session.execute(stmt).unique().scalars().all())

        result = orm.schema(query=Query, optimizer=False).execute_sync(
            "{ posts { title author { name } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        assert [row["author"] for row in result.data["posts"]] == [
            {"name": "Alice"},
            {"name": "Alice"},
            None,
            None,
        ]

    def test_an_unscoped_to_one_reads_straight_through(
        self, sa_session, seed, User, Post
    ):
        """With no scope there is nothing to reapply, so the row is returned."""
        from sqlalchemy import select

        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                stmt = select(Post).order_by(Post.id)
                return list(sa_session.execute(stmt).unique().scalars().all())

        result = orm.schema(query=Query).execute_sync(
            "{ posts { title author { name } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        assert [row["author"]["name"] for row in result.data["posts"]] == [
            "Alice",
            "Alice",
            "Bob",
            "Charlie",
        ]

    def test_a_null_to_one_stays_null_under_a_scope(self, sa_session, seed, Comment):
        """Nothing on the other end means nothing to scope."""
        from sqlalchemy import select

        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(Comment, name="ParentComment")
        class ParentType:
            id: auto
            body: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(Comment.body != "unreachable")

        @orm.type(Comment, name="ReplyComment")
        class ReplyType:
            id: auto
            body: auto
            parent: ParentType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def comments(self, info: strawberry.types.Info) -> list[ReplyType]:
                stmt = select(Comment).order_by(Comment.id)
                return list(sa_session.execute(stmt).unique().scalars().all())

        result = orm.schema(query=Query).execute_sync(
            "{ comments { body parent { body } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        parents = [row["parent"] for row in result.data["comments"]]
        assert parents[0] is None
        assert parents[1] == {"body": "Nice post!"}

    @pytest.mark.parametrize("optimizer", [True, False])
    def test_an_already_loaded_to_one_is_taken_as_is(
        self, sa_session, seed, User, Post, optimizer
    ):
        """A relation loaded by the resolver is re-checked against scope_rows.

        ``joinedload`` fills the cache before this library can scope it.
        Trusting that cache would return rows the scope removes.
        """
        from sqlalchemy import select
        from sqlalchemy.orm import joinedload

        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(User)
        class AuthorType:
            id: auto
            name: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(User.name == "Alice")

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto
            author: AuthorType | None

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                stmt = select(Post).order_by(Post.id).options(joinedload(Post.author))
                return list(sa_session.execute(stmt).unique().scalars().all())

        result = orm.schema(query=Query, optimizer=optimizer).execute_sync(
            "{ posts { title author { name } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        assert [row["author"] for row in result.data["posts"]] == [
            {"name": "Alice"},
            {"name": "Alice"},
            None,
            None,
        ]

    def test_a_materialized_list_drops_rows_scope_rows_rejects(
        self, sa_session, seed, Post
    ):
        """``.scalars().all()`` never reaches the optimizer's query scoping."""
        from sqlalchemy import select

        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(Post.is_published == True)  # noqa: E712

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                stmt = select(Post).order_by(Post.id)
                return list(sa_session.execute(stmt).scalars().all())

        result = orm.schema(query=Query).execute_sync(
            "{ posts { title } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        assert [row["title"] for row in result.data["posts"]] == [
            "Hello World",
            "GraphQL Guide",
            "Rust Adventures",
        ]

    def test_auto_field_without_the_optimizer_drops_hidden_rows(
        self, sa_session, seed, Post
    ):
        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(Post.is_published == True)  # noqa: E712

        @strawberry.type
        class Query:
            posts: list[PostType] = orm.field.auto()

        result = orm.schema(query=Query, optimizer=False).execute_sync(
            "{ posts { title } }",
            context_value={"session": sa_session},
        )
        if result.errors:
            assert result.data is None
        else:
            titles = [row["title"] for row in result.data["posts"]]
            assert "Draft Post" not in titles

    def test_a_prefetched_to_many_drops_rows_scope_rows_rejects(
        self, sa_session, seed, User, Post
    ):
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(Post)
        class PostType:
            id: auto
            title: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(Post.is_published == True)  # noqa: E712

        @orm.type(User)
        class UserType:
            id: auto
            name: auto
            posts: list[PostType]

        @strawberry.type
        class Query:
            @strawberry.field
            def users(self, info: strawberry.types.Info) -> list[UserType]:
                stmt = select(User).order_by(User.id).options(selectinload(User.posts))
                return list(sa_session.execute(stmt).unique().scalars().all())

        result = orm.schema(query=Query).execute_sync(
            "{ users { name posts { title } } }",
            context_value={"session": sa_session},
        )
        assert result.errors is None, result.errors
        by_name = {row["name"]: row["posts"] for row in result.data["users"]}
        assert by_name["Bob"] == []

    def test_an_unsaved_scoped_instance_raises(self, sa_session, seed, Post):
        sa_session.expunge_all()
        orm = StrawberryORM.for_sqlalchemy(
            dialect="sqlite",
            session_getter=lambda info: info.context["session"],
            warn_missing_scope=False,
        )

        @orm.type(Post)
        class PostType:
            title: auto

            @classmethod
            def scope_rows(cls, select_stmt, info):
                return select_stmt.where(Post.is_published == True)  # noqa: E712

        @strawberry.type
        class Query:
            @strawberry.field
            def posts(self, info: strawberry.types.Info) -> list[PostType]:
                return [Post(title="New", body="x", is_published=False, author_id=1)]

        result = orm.schema(query=Query).execute_sync(
            "{ posts { title } }",
            context_value={"session": sa_session},
        )
        assert result.errors
        assert "scope_rows" in result.errors[0].message
        assert "primary key" in result.errors[0].message
