"""CLI for inspecting strawberry-orm test schemas via GraphiQL."""

from __future__ import annotations

from enum import Enum
from typing import Any

import typer

app = typer.Typer(help="strawberry-orm schema inspector")


class Backend(str, Enum):
    django = "django"
    sqlalchemy = "sqlalchemy"


SCHEMA_NAMES = ("main", "self_model", "get_queryset", "multi_type")


# ---------------------------------------------------------------------------
# Django helpers
# ---------------------------------------------------------------------------


def _configure_django() -> None:
    import django
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "tests.backends.django.app.TestAppConfig",
            ],
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
            SECRET_KEY="cli-secret-key",
            USE_TZ=False,
        )
        django.setup()


def _seed_django() -> None:
    from django.db import connection

    from tests.backends.django.models import (
        Comment as DjComment,
    )
    from tests.backends.django.models import (
        Post as DjPost,
    )
    from tests.backends.django.models import (
        Tag as DjTag,
    )
    from tests.backends.django.models import (
        User as DjUser,
    )

    with connection.schema_editor() as editor:
        for model in (DjUser, DjTag, DjPost, DjComment):
            try:
                editor.create_model(model)
            except Exception:
                pass

    alice = DjUser.objects.create(id=1, name="Alice", email="alice@example.com")
    bob = DjUser.objects.create(id=2, name="Bob", email="bob@example.com")
    charlie = DjUser.objects.create(id=3, name="Charlie", email="charlie@test.org")

    py = DjTag.objects.create(id=1, name="python")
    gql = DjTag.objects.create(id=2, name="graphql")
    rs = DjTag.objects.create(id=3, name="rust")

    p1 = DjPost.objects.create(
        id=1, title="Hello World", body="First post", is_published=True, author=alice
    )
    p2 = DjPost.objects.create(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author=alice,
    )
    DjPost.objects.create(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author=bob,
    )
    p4 = DjPost.objects.create(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author=charlie,
    )

    p1.tags.add(py)
    p2.tags.add(py, gql)
    p4.tags.add(rs)

    DjComment.objects.create(id=1, body="Nice post!", post=p1, author=bob)
    DjComment.objects.create(id=2, body="Thanks!", post=p1, author=alice, parent_id=1)
    DjComment.objects.create(id=3, body="Great guide", post=p2, author=charlie)


# ---------------------------------------------------------------------------
# SQLAlchemy helpers
# ---------------------------------------------------------------------------


def _seed_sqlalchemy() -> Any:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from tests.backends.sqlalchemy.models import Base, Comment, Post, Tag, User

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    alice = User(id=1, name="Alice", email="alice@example.com")
    bob = User(id=2, name="Bob", email="bob@example.com")
    charlie = User(id=3, name="Charlie", email="charlie@test.org")
    session.add_all([alice, bob, charlie])
    session.flush()

    py, gql, rs = (
        Tag(id=1, name="python"),
        Tag(id=2, name="graphql"),
        Tag(id=3, name="rust"),
    )
    session.add_all([py, gql, rs])
    session.flush()

    p1 = Post(
        id=1, title="Hello World", body="First post", is_published=True, author_id=1
    )
    p2 = Post(
        id=2,
        title="GraphQL Guide",
        body="Learn GraphQL",
        is_published=True,
        author_id=1,
    )
    p3 = Post(
        id=3,
        title="Draft Post",
        body="Not published yet",
        is_published=False,
        author_id=2,
    )
    p4 = Post(
        id=4,
        title="Rust Adventures",
        body="Systems programming",
        is_published=True,
        author_id=3,
    )
    session.add_all([p1, p2, p3, p4])
    session.flush()

    p1.tags.append(py)
    p2.tags.extend([py, gql])
    p4.tags.append(rs)
    session.flush()

    session.add_all(
        [
            Comment(id=1, body="Nice post!", post_id=1, author_id=2),
            Comment(id=2, body="Thanks!", post_id=1, author_id=1, parent_id=1),
            Comment(id=3, body="Great guide", post_id=2, author_id=3),
        ]
    )
    session.commit()
    return session


# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------


def _load_schema(backend: Backend, name: str) -> Any:
    # Use the node-mutation demo schema for the main example so GraphiQL exposes
    # create_node/update_node alongside the standard query surface.
    attr = "node_mutation_schema" if name == "main" else f"{name}_schema"

    if backend == Backend.django:
        _configure_django()
        from tests.backends.django import fixtures as mod
    else:
        from tests.backends.sqlalchemy import fixtures as mod

    schema = getattr(mod, attr, None)
    if schema is None:
        available = sorted(
            a.removesuffix("_schema") for a in dir(mod) if a.endswith("_schema")
        )
        typer.echo(
            f"Unknown schema '{name}'. Available: {', '.join(available)}", err=True
        )
        raise typer.Exit(1)
    return schema


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


@app.command()
def show(
    schema_name: str | None = typer.Argument(
        None,
        help=f"Schema to display. Choices: {', '.join(SCHEMA_NAMES)}. "
        "Omit to list available schemas.",
    ),
    backend: Backend = typer.Option(
        Backend.django, "--backend", "-b", help="ORM backend."
    ),
    port: int = typer.Option(8420, "--port", "-p", help="Port to serve on."),
    no_seed: bool = typer.Option(False, "--no-seed", help="Skip seeding demo data."),
) -> None:
    """Open GraphiQL for a schema in the browser."""
    if schema_name is None:
        typer.echo(f"Available schemas ({backend.value}):")
        for name in SCHEMA_NAMES:
            typer.echo(f"  {name}")
        raise typer.Exit()

    import uvicorn
    from strawberry.asgi import GraphQL

    schema = _load_schema(backend, schema_name)

    if backend == Backend.sqlalchemy:
        if not no_seed:
            session = _seed_sqlalchemy()
        else:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            from tests.backends.sqlalchemy.models import Base

            engine = create_engine("sqlite:///:memory:")
            Base.metadata.create_all(engine)
            session = sessionmaker(bind=engine)()

        class App(GraphQL):
            async def get_context(self, request, response=None):
                return {"session": session}

        graphql_app = App(schema)
    else:
        if not no_seed:
            _seed_django()
        graphql_app = GraphQL(schema)

    typer.echo(f"Serving {backend.value}/{schema_name} on http://localhost:{port}")
    uvicorn.run(graphql_app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    app()
