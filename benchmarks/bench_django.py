"""Serialization / execution benchmarks for strawberry-orm on Django.

Measures, for paged result sets in the hundreds of objects:
  1. Stage breakdown: parse / validate / execute / JSON-encode
  2. Encoder comparison: stdlib json vs orjson vs msgspec at several payload sizes
  3. Extension effects: ParserCache, ValidationCache, DisableValidation
  4. ORM materialization ceiling: full instances vs only() vs values()
  5. Pure graphql-core completion cost (no DB) on the same shapes
  6. Relay connection paging at first: 50/100/250/500

The per-field fast paths this benchmark originally prototyped (cached type
checks, relation-name sets, relation verdicts, scalar leaf early-exit, and
returns_orm_list verdicts) were landed in the library in v0.21.1, so the
numbers here measure the optimized code.

Run:  uv pip install orjson msgspec   (not project dependencies)
      uv run python benchmarks/bench_django.py
"""

import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import django
from django.conf import settings

# --------------------------------------------------------------------------
# Django bootstrap (same recipe as tests/backends/django/conftest.py)
# --------------------------------------------------------------------------
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
    SECRET_KEY="bench",
    USE_TZ=False,
)
django.setup()

import msgspec  # noqa: E402
import orjson  # noqa: E402
import strawberry  # noqa: E402
from django.db import connection  # noqa: E402
from graphql import parse as gql_parse  # noqa: E402
from graphql import validate as gql_validate  # noqa: E402
from strawberry import relay  # noqa: E402
from strawberry.extensions import (  # noqa: E402
    DisableValidation,
    ParserCache,
    ValidationCache,
)
from strawberry.schema.config import StrawberryConfig  # noqa: E402

from strawberry_orm import StrawberryORM  # noqa: E402
from strawberry_orm.relay import ORMListConnection  # noqa: E402
from strawberry_orm.types import auto  # noqa: E402
from tests.backends.django.models import (  # noqa: E402
    Comment,
    Post,
    Tag,
    User,
)

# --------------------------------------------------------------------------
# Schema (strawberry-orm)
# --------------------------------------------------------------------------
orm = StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@orm.type(Comment)
class CommentType:
    id: auto
    body: auto
    post_id: auto
    author_id: auto


@orm.type(Tag)
class TagType:
    id: auto
    name: auto


@orm.type(User)
class AuthorType:
    id: auto
    name: auto
    email: auto


@orm.type(Post)
class PostType:
    id: auto
    title: auto
    body: auto
    is_published: auto
    author: AuthorType
    tags: list[TagType]
    comments: list[CommentType]


@orm.type(User)
class UserType:
    id: auto
    name: auto
    email: auto
    posts: list[PostType]


@strawberry.type
class Query:
    users: list[UserType] = orm.field.auto()
    posts: list[PostType] = orm.field.auto()


def make_schema(extra_extensions=()):
    return orm.schema(query=Query, extensions=list(extra_extensions))


schema = make_schema()

# Separate registry for the relay/connection schema (one type per model).
node_orm = StrawberryORM.for_django(lazy_resolution="off", warn_missing_scope=False)


@node_orm.type(User)
class AuthorNode:
    id: auto
    name: auto
    email: auto


@node_orm.type(Post)
class PostNode(relay.Node):
    id: relay.NodeID[int]
    title: auto
    body: auto
    is_published: auto
    author: AuthorNode


@strawberry.type
class NodeQuery:
    posts_conn: ORMListConnection[PostNode] = node_orm.connection.eager()


node_schema = node_orm.schema(
    query=NodeQuery,
    config=StrawberryConfig(relay_max_results=1000),
)

# --------------------------------------------------------------------------
# Plain strawberry schema over prefetched instances (isolates graphql-core)
# --------------------------------------------------------------------------
_PREFETCHED_POSTS: list[Post] = []


@strawberry.type
class PlainAuthor:
    id: int
    name: str
    email: str


@strawberry.type
class PlainPost:
    id: int
    title: str
    body: str
    is_published: bool

    @strawberry.field
    def author(self) -> PlainAuthor:
        a = self.author  # type: ignore[attr-defined]
        return PlainAuthor(id=a.id, name=a.name, email=a.email)


@strawberry.type
class PlainQuery:
    @strawberry.field
    def posts(self, n: int) -> list[PlainPost]:
        return _PREFETCHED_POSTS[:n]  # type: ignore[return-value]


plain_schema = strawberry.Schema(query=PlainQuery)

# --------------------------------------------------------------------------
# Seed
# --------------------------------------------------------------------------
N_USERS = 50
POSTS_PER_USER = 10  # 500 posts
COMMENTS_PER_POST = 3  # 1500 comments
LOREM = (
    "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod "
    "tempor incididunt ut labore et dolore magna aliqua. "
) * 3  # ~350 chars, a realistic body


def seed():
    with connection.schema_editor() as editor:
        for model in (User, Tag, Post, Comment):
            editor.create_model(model)

    users = User.objects.bulk_create(
        User(name=f"User {i}", email=f"user{i}@example.com") for i in range(N_USERS)
    )
    tags = Tag.objects.bulk_create(Tag(name=f"tag-{i}") for i in range(5))
    posts = Post.objects.bulk_create(
        Post(
            title=f"Post {u.pk}-{j}",
            body=LOREM,
            is_published=j % 2 == 0,
            author=u,
        )
        for u in users
        for j in range(POSTS_PER_USER)
    )
    through = Post.tags.through
    through.objects.bulk_create(
        through(post_id=p.pk, tag_id=tags[i % 5].pk)
        for i, p in enumerate(posts)
        for _ in (0,)
    )
    Comment.objects.bulk_create(
        Comment(body=f"Comment {k} on {p.pk}", post=p, author=users[k % N_USERS])
        for p in posts
        for k in range(COMMENTS_PER_POST)
    )
    return posts


# --------------------------------------------------------------------------
# Timing helpers
# --------------------------------------------------------------------------
def bench(fn, *, warmup=3, runs=25) -> dict:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return {
        "median_ms": statistics.median(samples),
        "p90_ms": statistics.quantiles(samples, n=10)[8],
        "min_ms": min(samples),
    }


def run_query(s, q, variables=None):
    result = s.execute_sync(q, variable_values=variables or {})
    assert result.errors is None, result.errors
    return result.data


RESULTS: dict = {}


def record(section, name, stats, extra=None):
    row = {"name": name, **stats}
    if extra:
        row.update(extra)
    RESULTS.setdefault(section, []).append(row)
    print(
        f"  {name:<52} median {stats['median_ms']:8.2f} ms   "
        f"p90 {stats['p90_ms']:8.2f} ms"
    )


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------
Q_FLAT_POSTS = """
{ posts { id title body isPublished } }
"""

Q_POSTS_AUTHOR = """
{ posts { id title body isPublished author { id name email } } }
"""

Q_NESTED_USERS = """
{ users { id name email posts { id title isPublished comments { id body } } } }
"""

Q_CONN = """
query($first: Int!) {
  postsConn(first: $first) {
    edges { node { id title body isPublished author { id name email } } }
    pageInfo { hasNextPage endCursor }
  }
}
"""

Q_PLAIN = """
query($n: Int!) { posts(n: $n) { id title body isPublished author { id name email } } }
"""


def main():
    posts = seed()
    global _PREFETCHED_POSTS
    _PREFETCHED_POSTS = list(Post.objects.select_related("author").all())

    print(
        f"\nSeeded: {N_USERS} users, {len(posts)} posts, "
        f"{Comment.objects.count()} comments\n"
    )

    # ------------------------------------------------------------------
    print("== 1. Full request, end to end (execute_sync + stdlib json) ==")
    for label, q, vars_ in [
        ("500 posts, 4 scalar fields", Q_FLAT_POSTS, None),
        ("500 posts + author (select_related)", Q_POSTS_AUTHOR, None),
        ("50 users -> 500 posts -> 1500 comments", Q_NESTED_USERS, None),
    ]:
        data_holder = {}

        def full(q=q, vars_=vars_, h=data_holder):
            h["data"] = run_query(schema, q, vars_)
            json.dumps({"data": h["data"]})

        record("full_request", label, bench(full))

    # ------------------------------------------------------------------
    print("\n== 2. Stage breakdown: 500 posts + author ==")
    gql_schema = schema._schema  # graphql-core GraphQLSchema
    record("stages", "parse", bench(lambda: gql_parse(Q_POSTS_AUTHOR)))
    document = gql_parse(Q_POSTS_AUTHOR)
    record("stages", "validate", bench(lambda: gql_validate(gql_schema, document)))
    record(
        "stages",
        "execute (incl. DB round trips)",
        bench(lambda: run_query(schema, Q_POSTS_AUTHOR)),
    )
    data = {"data": run_query(schema, Q_POSTS_AUTHOR)}
    record("stages", "encode: json.dumps", bench(lambda: json.dumps(data)))
    record("stages", "encode: orjson.dumps", bench(lambda: orjson.dumps(data)))
    enc = msgspec.json.Encoder()
    record("stages", "encode: msgspec (bytes)", bench(lambda: enc.encode(data)))
    record(
        "stages",
        "encode: msgspec (bytes->str)",
        bench(lambda: enc.encode(data).decode()),
    )

    # ------------------------------------------------------------------
    print("\n== 3. Pure graphql-core completion (prefetched, no DB) ==")
    for n in (100, 250, 500):
        record(
            "graphql_core",
            f"complete {n} posts + author",
            bench(lambda n=n: run_query(plain_schema, Q_PLAIN, {"n": n})),
            extra={"n": n},
        )

    # ------------------------------------------------------------------
    print("\n== 4. Extensions: parser/validation caches ==")
    variants = [
        ("baseline", make_schema()),
        ("ParserCache", make_schema([ParserCache(maxsize=64)])),
        (
            "ParserCache + ValidationCache",
            make_schema([ParserCache(maxsize=64), ValidationCache(maxsize=64)]),
        ),
        (
            "ParserCache + DisableValidation",
            make_schema([ParserCache(maxsize=64), DisableValidation()]),
        ),
    ]
    for label, s in variants:
        record("extensions", label, bench(lambda s=s: run_query(s, Q_POSTS_AUTHOR)))

    # ------------------------------------------------------------------
    print("\n== 5. Relay connection paging (postsConn) ==")
    for first in (50, 100, 250, 500):
        record(
            "paging",
            f"first: {first}",
            bench(lambda f=first: run_query(node_schema, Q_CONN, {"first": f})),
            extra={"first": first},
        )

    # ------------------------------------------------------------------
    print("\n== 6. ORM materialization ceiling (500 posts + author) ==")
    record(
        "orm",
        "full instances + select_related",
        bench(lambda: list(Post.objects.select_related("author").all())),
    )
    record(
        "orm",
        "only(id,title,body,is_published) + select_related",
        bench(
            lambda: list(
                Post.objects.select_related("author").only(
                    "id",
                    "title",
                    "body",
                    "is_published",
                    "author__id",
                    "author__name",
                    "author__email",
                )
            )
        ),
    )
    record(
        "orm",
        "values() (dicts, no instances)",
        bench(
            lambda: list(
                Post.objects.values(
                    "id",
                    "title",
                    "body",
                    "is_published",
                    "author__id",
                    "author__name",
                    "author__email",
                )
            )
        ),
    )

    # ------------------------------------------------------------------
    print("\n== 7. Encoder scaling on realistic GraphQL payloads ==")

    def payload(n):
        return {
            "data": {
                "posts": [
                    {
                        "id": i,
                        "title": f"Post {i}",
                        "body": LOREM,
                        "isPublished": i % 2 == 0,
                        "author": {
                            "id": i % 50,
                            "name": f"User {i % 50}",
                            "email": f"user{i % 50}@example.com",
                        },
                    }
                    for i in range(n)
                ]
            }
        }

    for n in (100, 500, 1000, 5000):
        p = payload(n)
        record(
            "encoders",
            f"json.dumps        n={n}",
            bench(lambda p=p: json.dumps(p)),
            extra={"n": n, "enc": "json"},
        )
        record(
            "encoders",
            f"orjson.dumps      n={n}",
            bench(lambda p=p: orjson.dumps(p)),
            extra={"n": n, "enc": "orjson"},
        )
        record(
            "encoders",
            f"msgspec.encode    n={n}",
            bench(lambda p=p: enc.encode(p)),
            extra={"n": n, "enc": "msgspec"},
        )

    # ------------------------------------------------------------------
    print("\n== 8. Extension stack cost (same schema, no optimizer/batching) ==")
    bare_schema = strawberry.Schema(query=Query)  # resolvers still work; no exts
    record(
        "stack",
        "flat 500 posts: with optimizer+batching",
        bench(lambda: run_query(schema, Q_FLAT_POSTS)),
    )
    record(
        "stack",
        "flat 500 posts: bare strawberry.Schema",
        bench(lambda: run_query(bare_schema, Q_FLAT_POSTS)),
    )
    record(
        "stack",
        "500 posts + author: with optimizer (2 queries)",
        bench(lambda: run_query(schema, Q_POSTS_AUTHOR)),
    )
    record(
        "stack",
        "500 posts + author: bare (N+1: 501 queries)",
        bench(lambda: run_query(bare_schema, Q_POSTS_AUTHOR)),
    )

    # ------------------------------------------------------------------
    print("\n== 9. Profile: where execute() time goes (500 posts + author) ==")
    import cProfile
    import pstats

    prof = cProfile.Profile()
    prof.enable()
    for _ in range(10):
        run_query(schema, Q_POSTS_AUTHOR)
    prof.disable()
    stats = pstats.Stats(prof)
    total = stats.total_tt  # type: ignore[attr-defined]

    # Aggregate tottime by top-level package
    by_pkg: dict[str, float] = {}
    for (filename, _, funcname), (_, _, tt, _, _) in stats.stats.items():  # type: ignore[attr-defined]
        if "strawberry_orm" in filename:
            pkg = "strawberry_orm"
        elif "/django/" in filename:
            pkg = "django"
        elif "/graphql/" in filename:
            pkg = "graphql-core"
        elif "/strawberry/" in filename:
            pkg = "strawberry"
        elif "sqlite3" in filename or funcname in ("execute", "fetchall"):
            pkg = "sqlite3/db"
        else:
            pkg = "other/stdlib"
        by_pkg[pkg] = by_pkg.get(pkg, 0.0) + tt
    RESULTS["profile"] = []
    for pkg, tt in sorted(by_pkg.items(), key=lambda kv: -kv[1]):
        pct = 100 * tt / total
        RESULTS["profile"].append({"name": pkg, "tottime_s": tt, "pct": pct})
        print(f"  {pkg:<20} {tt:8.3f} s  ({pct:5.1f}%)")

    print("\n  Top 15 functions by tottime:")
    stats.sort_stats("tottime")
    import io

    buf = io.StringIO()
    stats.stream = buf  # type: ignore[attr-defined]
    stats.print_stats(15)
    print("\n".join(buf.getvalue().splitlines()[5:25]))

    with open("benchmarks/results_django.json", "w") as f:
        json.dump(RESULTS, f, indent=2)
    print("\nWrote benchmarks/results_django.json")


if __name__ == "__main__":
    main()
