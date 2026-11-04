"""A small schema and a scripted model for the MCP plugin's tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

import strawberry

from strawberry_orm.contrib.mcp import QueryAgent


@strawberry.interface
class Named:
    name: str


@strawberry.type
class Author(Named):
    id: int


@strawberry.type
class Post:
    title: str
    author: Author

    @strawberry.field
    def secret(self) -> str | None:
        raise PermissionError("You may not read secrets.")


POSTS = [
    Post(title="Hello", author=Author(id=1, name="Alice")),
    Post(title="Rust", author=Author(id=2, name="Bob")),
    Post(title="Draft", author=Author(id=1, name="Alice")),
]


@strawberry.type
class Query:
    @strawberry.field
    def posts(self, info: strawberry.Info, title: str | None = None) -> list[Post]:
        info.context.setdefault("seen", []).append(info.context.get("user"))
        return [post for post in POSTS if title in (None, post.title)]

    @strawberry.field
    def named(self) -> Named:
        return Author(id=1, name="Alice")

    @strawberry.field
    def post(self) -> Post:
        raise LookupError("No such post.")


@strawberry.type
class Mutation:
    @strawberry.mutation
    def publish(self, title: str) -> str:
        return f"published {title}"

    @strawberry.mutation
    def fail(self) -> str:
        raise RuntimeError("Publishing is down.")


schema = strawberry.Schema(query=Query, mutation=Mutation)

PUBLISH = 'mutation { publish(title: "x") }'


def call(query=None, variables=None, *, name="run_graphql", raw=None, id="c1"):
    arguments = (
        raw if raw is not None else json.dumps({"query": query, "variables": variables})
    )
    return SimpleNamespace(
        id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def ask_user(message="Which author?", fields=None, *, id="q1"):
    fields = fields or {"author": {"type": "string", "description": "Their name"}}
    return call(
        name="ask_user", raw=json.dumps({"message": message, "fields": fields}), id=id
    )


def reply(*calls, content=None):
    message = SimpleNamespace(tool_calls=list(calls) or None, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class Model:
    """Replays scripted replies and keeps what each request was sent."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    async def __call__(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        return self.replies.pop(0)

    def tool_replies(self, request=-1):
        return [
            json.loads(m["content"])
            for m in self.requests[request]["messages"]
            if m["role"] == "tool"
        ]

    def tools(self, request=-1):
        return [t["function"]["name"] for t in self.requests[request]["tools"]]


def agent(*replies, **options):
    model = Model(*replies)
    return QueryAgent(schema, model="test:model", completion=model, **options), model
