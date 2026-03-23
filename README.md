# strawberry-orm

Backend-agnostic schema generation for [Strawberry GraphQL](https://strawberry.rocks/) on top of Django ORM, SQLAlchemy, and Tortoise ORM.

> **Warning** — `strawberry-orm` is still in **alpha**. Expect breaking changes and incomplete APIs while the package stabilizes.

## Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Backends](#backends)
- [Defining Types](#defining-types)
- [Filters and Ordering](#filters-and-ordering)
- [Custom Filters and Ordering](#custom-filters-and-ordering)
- [Mutations](#mutations)
- [Relay Integration](#relay-integration)
- [Query Optimization](#query-optimization)
- [Async Usage](#async-usage)
- [Security](#security)
- [Public Exports](#public-exports)

## Installation

```bash
uv add "strawberry-orm[sqlalchemy]"   # or [django] or [tortoise]
```

Or with pip:

```bash
pip install "strawberry-orm[sqlalchemy]"
```

Requires Python `>=3.12` and `strawberry-graphql>=0.311.0`.

## Quick Start

A blog API with users, posts, tags, and comments — covering types, relations, queryset scoping, optimizer hints, filters, ordering, object traversal, mutations, ref lists, recursive node mutations, and the query optimizer:

```python
import strawberry
from strawberry_orm import StrawberryORM, auto

orm = StrawberryORM(
    "sqlalchemy",
    dialect="postgresql",
    session_getter=lambda info: info.context["session"],
)

# -- Filters and ordering (register leaf models first) -----------------------

UserFilter = orm.filter(User)
UserOrder  = orm.order(User)
TagFilter  = orm.filter(Tag)
TagOrder   = orm.order(Tag)

CommentFilter = orm.filter(Comment)
PostFilter    = orm.filter(Post)      # picks up author/tags/comments relations
PostOrder     = orm.order(Post)

# -- Types -------------------------------------------------------------------

@orm.type(User, filters=UserFilter, order=UserOrder)
class UserType:
    id: auto
    name: auto
    email: auto
    posts: list["PostType"]

@orm.type(Tag, filters=TagFilter, order=TagOrder)
class TagType:
    id: auto
    name: auto

@orm.type(Comment, filters=CommentFilter)
class CommentType:
    id: auto
    body: auto

@orm.type(Post, filters=PostFilter, order=PostOrder)
class PostType:
    id: auto
    title: auto
    body: auto
    is_published: auto
    author: UserType
    tags: list[TagType] = orm.field(load=lambda qs: qs.order_by("name"))
    comments: list[CommentType]

    @classmethod
    def get_queryset(cls, qs, info):
        return qs.filter(is_published=True)   # works on all backends

# -- Mutations ---------------------------------------------------------------

CreatePostInput = orm.input(Post, include=["title", "body", "author_id"])
CreateTagInput  = orm.input(Tag, include=["name"])
TagRef = orm.ref(Tag, create=CreateTagInput, unlink=True, delete=True)

@strawberry.type
class Mutation:
    @strawberry.mutation
    def create_post(self, input: CreatePostInput) -> PostType:
        post = Post(title=input.title, body=input.body, author_id=input.author_id)
        ...
        return post

    @strawberry.mutation
    def set_post_tags(self, post_id: int, tags: list[TagRef]) -> PostType:
        post = ...
        orm.apply_ref_list(post, "tags", tags)
        return post

    # Recursive node mutation — creates a post with nested relations in one call
    create_node = orm.mutations.create_node()
    update_node = orm.mutations.update_node()

# -- Schema ------------------------------------------------------------------

@strawberry.type
class Query:
    users: list[UserType] = orm.field()
    posts: list[PostType] = orm.field()

schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[orm.optimizer_extension()],
)
```

That gives you:

```graphql
# Filter posts by a related author's name, ordered by title
{
  posts(
    filter: {
      all: [
        { field: { isPublished: { exact: true } } }
        { object: { author: { field: { name: { exact: "Alice" } } } } }
      ]
    }
    order: [{ field: { title: ASC } }]
  ) {
    title
    author { name }
    tags { name }
  }
}

# Manage related tags on a post
mutation {
  setPostTags(postId: 1, tags: [
    { update: { id: "2" } }
    { create: { name: "new-tag" } }
    { unlink: { id: "3" } }
    { delete: { id: "4" } }
  ]) {
    tags { id name }
  }
}

# Create a post with nested author and tags in one recursive mutation
mutation {
  createNode(input: {
    post: {
      title: "Hello"
      body: "World"
      author: { create: { name: "Alice", email: "alice@example.com" } }
      tags: [{ create: { name: "python" } }]
    }
  }) { __typename }
}
```

---

## Backends

| Backend | Constructor | Notes |
| --- | --- | --- |
| Django | `StrawberryORM("django")` | Uses Django querysets directly. |
| SQLAlchemy | `StrawberryORM("sqlalchemy", dialect="...", session_getter=...)` | Requires a `Session` or `AsyncSession` at resolve time. |
| Tortoise | `StrawberryORM("tortoise")` | Async ORM; use async Strawberry execution. |

- **Django** — sync and async schema execution both work. Custom async resolvers that touch the ORM directly still need `sync_to_async(...)`.
- **SQLAlchemy** — the session is resolved from `session_getter`, `info.context["session"]`, `info.context.session`, or `info.context.get_session()`. Both sync and async sessions are supported.
- **Tortoise** — async-first. Use `await` in resolvers and mutations.

<details>
<summary>Backend options reference</summary>

Shared options:

| Option | Default | Meaning |
| --- | --- | --- |
| `default_query_limit` | `None` | Default limit for auto-generated list queries. |
| `exclude_sensitive_fields` | `True` | Excludes sensitive-looking fields from generated input/filter/order types. |
| `warn_sensitive` | `True` | Warns when sensitive-looking fields are exposed on output types. |
| `max_filter_depth` | `10` | Caps recursive filter nesting. |
| `max_filter_branches` | `50` | Caps `all` / `any` / `oneOf` branch count. |
| `max_in_list_size` | `500` | Caps `inList` / `notInList` size. |
| `enable_regex_filters` | `False` | Enables `regex` and `iRegex` string lookups. |

SQLAlchemy-only:

| Option | Default | Meaning |
| --- | --- | --- |
| `dialect` | `"postgresql"` | SQLAlchemy dialect. |
| `session_getter` | `None` | Callable returning the session for the current request. |
| `filter_overrides` | `{}` | Maps Python types to custom lookup input types. |

</details>

---

## Defining Types

### `@orm.type(Model)`

```python
from strawberry_orm import auto

@orm.type(User)
class UserType:
    id: auto
    name: auto
    email: auto
```

`auto` is an alias for `strawberry.auto`. The backend inspects the model and resolves the Python type for each field.

Keyword arguments: `include`, `exclude`, `name`, `filters`, `order`.

```python
@orm.type(User, exclude=["password_hash", "api_key"], name="PublicUser")
class PublicUserType:
    id: auto
    name: auto
    email: auto
```

### Relations

Reference other generated types directly. The backend auto-generates resolvers for relationship fields:

```python
@orm.type(Post)
class PostType:
    id: auto
    title: auto
    tags: list[TagType]
```

If the nested type carries `filters` and/or `order`, list relations expose those arguments automatically.

### List Fields and Explicit Resolvers

`orm.field()` builds a list resolver from the model attached to the return type:

```python
@strawberry.type
class Query:
    users: list[UserType] = orm.field()
```

For custom scoping, return a backend query object from a regular Strawberry resolver:

```python
@strawberry.type
class Query:
    @strawberry.field
    def active_users(self, info: strawberry.types.Info) -> list[UserType]:
        return select(User).where(User.is_active.is_(True))  # SQLAlchemy
        # return User.objects.filter(is_active=True)         # Django
        # return User.filter(is_active=True)                 # Tortoise
```

### Type-Level Queryset Scoping

Define a `get_queryset` classmethod to scope the model query centrally:

```python
@orm.type(Post)
class PublishedPostType:
    id: auto
    title: auto

    @classmethod
    def get_queryset(cls, qs, info):
        return qs.filter(is_published=True)
```

Useful for soft-delete filtering, multi-tenant scoping, and authorization-aware model filters.

### Custom Strawberry Fields

Mix generated fields with plain Strawberry fields:

```python
@orm.type(User)
class UserType:
    id: auto
    name: auto
    email: auto

    @strawberry.field
    def display_name(self) -> str:
        return f"{self.name} <{self.email}>"
```

### `orm.input(Model)` and `orm.partial(Model)`

Generate input types from model metadata:

```python
CreateUserInput = orm.input(User, include=["name", "email"])
UpdateUserInput = orm.partial(User, include=["name", "email"])
```

`input()` and `partial()` share the same signature: `include`, `exclude`, `exclude_pk` (default `True`), `name`. Fields are optional (defaulting to `strawberry.UNSET`), skip relations, exclude primary keys by default, and exclude sensitive-looking fields unless explicitly included.

---

## Filters and Ordering

### Filters

Generate a filter input and attach it to a type:

```python
UserFilter = orm.filter(User)

@orm.type(User, filters=UserFilter)
class UserType:
    id: auto
    name: auto
    email: auto
```

List fields returning `UserType` then accept a `filter` argument:

```graphql
{
  users(filter: { field: { name: { exact: "Alice" } } }) {
    id
    name
  }
}
```

#### Filter Shape

Filters are recursive `@oneOf` trees supporting `field`, `all`, `any`, `not`, and `oneOf`:

```graphql
# OR
{ users(filter: { any: [
    { field: { name: { exact: "Alice" } } }
    { field: { name: { exact: "Bob" } } }
] }) { name } }

# AND
{ posts(filter: { all: [
    { field: { authorId: { exact: 1 } } }
    { field: { isPublished: { exact: true } } }
] }) { title } }

# NOT
{ users(filter: {
    not: { field: { email: { contains: "example.com" } } }
}) { name } }
```

<details>
<summary>Built-in lookup types</summary>

`StringLookup`, `BooleanLookup`, `IDLookup`, `IntComparisonLookup`, `FloatComparisonLookup`, `DateComparisonLookup`, `TimeComparisonLookup`, `DateTimeComparisonLookup`

Typical string lookups: `exact`, `neq`, `contains`, `iContains`, `startsWith`, `iStartsWith`, `endsWith`, `iEndsWith`, `inList`, `notInList`, `isNull`.

Regex lookups (`regex`, `iRegex`) are disabled by default. Enable with `enable_regex_filters=True`.

</details>

#### Object Traversal

When filters are registered for related models, the generated filter gains an `object` key for filtering by conditions on related objects:

```python
UserFilter = orm.filter(User)
PostFilter = orm.filter(Post)   # Post has an "author" relation to User
```

```graphql
{
  posts(filter: {
    object: { author: { field: { name: { exact: "Alice" } } } }
  }) { title }
}
```

Object traversal composes with boolean operators and supports multi-level nesting when intermediate models also have registered filters:

```graphql
# Comments on posts written by Alice
{
  comments(filter: {
    object: { post: {
      object: { author: { field: { name: { exact: "Alice" } } } }
    } }
  }) { body }
}
```

The `object` type is `@oneOf`. Relations only appear in `object` if their target model already has a registered filter at the time `orm.filter()` is called -- register leaf models first.

#### Filter Projection

Pass `project={...}` to control which relations appear in `object` and how deep traversal can go:

```python
UserFilter    = orm.filter(User)
TagFilter     = orm.filter(Tag)
CommentFilter = orm.filter(Comment)

PostFilter = orm.filter(Post, project={"author": {}})  # only author, not tags/comments
```

Sub-project dicts control nested traversal. `{}` means "include as a leaf" (no further object traversal). A non-empty dict lists reachable relations:

```python
CommentFilter = orm.filter(Comment, project={
    "post": {"author": {}},   # Comment -> post -> author (but not post -> tags)
})
```

| `project` value | Behavior |
| --- | --- |
| `None` (default) | Auto-include all relations with registered filters |
| `{}` | No `object` type (scalar lookups only) |
| `{"rel": {}}` | Include `rel` as a leaf |
| `{"rel": {"nested": {}}}` | Include `rel`, allow traversal to `nested` from it |

Projected filters are cached internally and do not overwrite the global filter registry.

### Ordering

```python
UserOrder = orm.order(User)
```

Each order entry is a `@oneOf` input with a `field` key (for scalar columns) or an `object` key (for related models). Position in the list determines tie-break priority:

```graphql
{
  users(order: [{ field: { name: ASC } }, { field: { email: DESC } }]) {
    name
    email
  }
}
```

Supported values: `ASC`, `ASC_NULLS_FIRST`, `ASC_NULLS_LAST`, `DESC`, `DESC_NULLS_FIRST`, `DESC_NULLS_LAST`.

#### Order by Related Object

When order types are registered for related models, the generated order gains an `object` key that lets you sort by fields on related objects — mirroring the [filter object traversal](#object-traversal) structure:

```graphql
{
  posts(order: [
    { object: { author: { field: { name: ASC } } } }
    { field: { title: DESC } }
  ]) {
    title
  }
}
```

Registration order matters: define related orders *before* the parent (e.g. `orm.order(User)` before `orm.order(Post)`).

---

## Custom Filters and Ordering

`orm.filter()` and `orm.order()` auto-generate types from model introspection. When you need filter logic that goes beyond column lookups — full-text search across multiple fields, subquery-based conditions, or ordering by computed values — use `orm.filter_type()` and `orm.order_type()` with the `@filter_field` and `@order_field` decorators.

### Custom Filter Types

`orm.filter_type(Model)` is a class decorator. Annotate fields with `auto` for standard lookups (identical to what `orm.filter()` generates). Add methods decorated with `@filter_field` for custom logic:

```python
from strawberry_orm import StrawberryORM, filter_field, auto

orm = StrawberryORM("sqlalchemy", dialect="postgresql", session_getter=...)

@orm.filter_type(User)
class UserFilter:
    name: auto          # standard StringLookup
    email: auto         # standard StringLookup

    @filter_field
    def search(self, value: str, query):
        """Full-text search across name and email."""
        from sqlalchemy import or_
        return query.where(
            or_(User.name.ilike(f"%{value}%"), User.email.ilike(f"%{value}%"))
        )

    @filter_field
    def has_posts(self, value: bool, query):
        """Filter users who have (or lack) any posts."""
        from sqlalchemy import func, select
        subq = (
            select(func.count(Post.id))
            .where(Post.author_id == User.id)
            .correlate(User)
            .scalar_subquery()
        )
        if value:
            return query.where(subq > 0)
        return query.where(subq == 0)
```

Each `@filter_field` method must:

- Have a `value` parameter with a **type annotation** — this becomes the GraphQL input type for the field.
- Have a `query` parameter — receives the backend's native query object (Django `QuerySet`, SQLAlchemy `Select`, or Tortoise `QuerySet`).
- Return the modified query.
- Optionally accept an `info` parameter to receive the Strawberry `Info` context.

The generated GraphQL input places custom fields as top-level keys alongside `field`, `object`, `all`, `any`, `not`, and `oneOf`:

```graphql
input UserFilter @oneOf {
  field: UserField           # auto-generated scalar lookups
  object: UserObject         # auto-generated relation lookups (if any)
  search: String             # custom
  hasPosts: Boolean          # custom
  all: [UserFilter!]
  any: [UserFilter!]
  not: UserFilter
  oneOf: [UserFilter!]
}
```

Since filters are `@oneOf`, combine custom filters with standard lookups using `all` or `any`:

```graphql
{
  users(filter: { all: [
    { search: "john" },
    { field: { email: { contains: "example.com" } } }
  ] }) {
    name
    email
  }
}
```

### Custom Order Types

`orm.order_type(Model)` works the same way. `auto` fields get the standard `Ordering` enum. Methods decorated with `@order_field` receive a `value` of type `Ordering` (ASC, DESC, etc.) and return the modified query:

```python
from strawberry_orm import order_field
from strawberry_orm.types import Ordering

@orm.order_type(User)
class UserOrder:
    name: auto          # standard Ordering (ASC/DESC/...)

    @order_field
    def post_count(self, value: Ordering, query):
        """Order users by how many posts they have."""
        from sqlalchemy import func
        query = query.outerjoin(Post, Post.author_id == User.id).group_by(User.id)
        col = func.count(Post.id)
        if "DESC" in value.value:
            return query.order_by(col.desc())
        return query.order_by(col.asc())
```

The generated GraphQL input:

```graphql
input UserOrder @oneOf {
  field: UserOrderField      # auto-generated
  object: UserOrderObject    # auto-generated (if relations exist)
  postCount: Ordering        # custom
}
```

Custom and standard orders compose naturally in the order list:

```graphql
{
  users(order: [
    { postCount: DESC },
    { field: { name: ASC } }
  ]) {
    name
  }
}
```

### Using Custom Types

Custom filter and order types are used exactly like auto-generated ones:

```python
@orm.type(User, filters=UserFilter, order=UserOrder)
class UserType:
    id: auto
    name: auto
    email: auto

@strawberry.type
class Query:
    @orm.field()
    def users(self) -> list[UserType]:
        return orm.get_default_queryset(User)
```

They also work with Relay connections and `orm.connection()`.

### Backend-Specific Examples

The query manipulation inside `@filter_field` and `@order_field` methods is backend-specific since it operates on native query objects. Here are equivalent examples for each backend:

<details>
<summary>Django</summary>

```python
from django.db.models import Q, Count, F

@orm.filter_type(User)
class UserFilter:
    name: auto

    @filter_field
    def search(self, value: str, query):
        return query.filter(Q(name__icontains=value) | Q(email__icontains=value))

@orm.order_type(User)
class UserOrder:
    name: auto

    @order_field
    def post_count(self, value: Ordering, query):
        query = query.annotate(_post_count=Count("posts"))
        dir_value = value.value
        if dir_value.startswith("DESC"):
            return query.order_by(F("_post_count").desc())
        return query.order_by(F("_post_count").asc())
```

</details>

<details>
<summary>Tortoise</summary>

```python
from tortoise.queryset import Q
from tortoise.functions import Count

@orm.filter_type(User)
class UserFilter:
    name: auto

    @filter_field
    def search(self, value: str, query):
        return query.filter(Q(name__icontains=value) | Q(email__icontains=value))

@orm.order_type(User)
class UserOrder:
    name: auto

    @order_field
    def post_count(self, value: Ordering, query):
        query = query.annotate(_post_count=Count("posts"))
        if value.value.startswith("DESC"):
            return query.order_by("-_post_count")
        return query.order_by("_post_count")
```

</details>

### Combining with `orm.filter()` / `orm.order()`

`orm.filter()` and `orm.order()` remain available for fully auto-generated types. Use `orm.filter_type()` and `orm.order_type()` only when you need custom logic. The types produced by both APIs are interchangeable in all contexts — `orm.type(Model, filters=..., order=...)`, `orm.field(filters=..., order=...)`, and `orm.connection()`.

---

## Mutations

Write plain `@strawberry.mutation` resolvers and use `strawberry-orm` for generated input types:

```python
CreatePostInput = orm.input(Post, include=["title", "body", "author_id"])

@strawberry.type
class Mutation:
    @strawberry.mutation
    def create_post(self, info: strawberry.types.Info, input: CreatePostInput) -> PostType:
        post = Post(title=input.title, body=input.body, author_id=input.author_id)
        ...
        return post
```

### Related List Inputs (`orm.ref`)

`orm.ref(...)` generates a `@oneOf` input for managing related lists:

```python
CreateTagInput = orm.input(Tag, include=["name"])

@strawberry.input
class UpdateTagInput:
    id: strawberry.ID
    name: str | None = strawberry.UNSET

TagRef = orm.ref(Tag, create=CreateTagInput, update=UpdateTagInput, unlink=True, delete=True)
```

Each ref is a `@oneOf` with these keys:

- `update` — link an existing object by ID, or update its fields. Always present (an ID-only input is auto-generated if no custom `update` type is provided).
- `create` — create a new related object (present when `create=` is provided).
- `unlink` — remove the object from the relation without deleting it (present when `unlink=True`).
- `delete` — hard-delete the related row (present when `delete=True`).

All list mutations use **patch semantics**: only the items you mention are affected; existing related objects not listed are left untouched.

Apply ref operations with `orm.apply_ref_list(parent, "relation_name", refs, info)`. An optional `authorize` callback `(action, model, obj_id, info) -> bool` can be provided for per-operation authorization.

```graphql
mutation {
  setPostTags(postId: 1, tags: [
    { update: { id: "2" } }
    { update: { id: "1", name: "python3" } }
    { create: { name: "new-tag" } }
    { unlink: { id: "3" } }
    { delete: { id: "4" } }
  ]) {
    tags { id name }
  }
}
```

> **Note:** Whether the order of items in the list affects the final ordering of the relation is an implementation detail that each backend must maintain.

### Recursive Node Mutations

`orm.mutations.create_node()` and `orm.mutations.update_node()` generate catch-all Relay `Node` mutations with recursive nested inputs:

```python
@orm.type(Post)
class PostNode(relay.Node):
    id: relay.NodeID[int]
    title: auto
    body: auto

@strawberry.type
class Mutation:
    create_node = orm.mutations.create_node()
    update_node = orm.mutations.update_node()
```

```graphql
mutation {
  createNode(input: {
    post: {
      title: "Hello"
      body: "World"
      author: { create: { name: "Alice", email: "alice@example.com" } }
      tags: [{ create: { name: "python" } }]
    }
  }) { __typename }
}
```

List relations are flat arrays of ref operations (same `@oneOf` shape as `orm.ref`). Patch semantics apply — only mentioned items are affected.

Generate only the input types (without the resolver) via `orm.mutations.create_node_input()` and `orm.mutations.update_node_input()`.

<details>
<summary>Mutation projection and policy config</summary>

Pass `project={...}` to restrict recursion depth and configure relation semantics:

```python
project = {
    "post": {
        "author": {
            "_meta": {"onReplace": ["DISCONNECT", "DELETE"]},
        },
        "comments": {
            "author": {"_meta": {"onReplace": ["DISCONNECT", "DELETE"]}},
        },
        "tags": {},
    },
    "comment": {
        "author": {"_meta": {"onReplace": ["DISCONNECT", "DELETE"]}},
    },
}

@strawberry.type
class Mutation:
    create_node = orm.mutations.create_node(project=project)
    update_node = orm.mutations.update_node(project=project)
```

Rules:

- Root keys are model names (`post`, `comment`, ...).
- Nested keys are relation names on that model.
- `_meta` configures behavior for that relation subtree.
- Omitted relations still appear as shallow inputs (one more level, then stop).

`_meta` supports:

- `onReplace` — `"DISCONNECT"` or `"DELETE"`, or an array of both to expose a choice. Controls what happens to the previous object when replacing a singular (FK) relation. Default: `DISCONNECT`.

Values can be a single string (fixes behavior, omits the GraphQL field) or an array of strings (exposes a choice to the caller).

</details>

---

## Relay Integration

`strawberry-orm` works with [Strawberry's Relay support](https://strawberry.rocks/docs/guides/relay) for cursor-based pagination and global node identification.

### Relay Node Types

Extend `relay.Node` instead of a plain Strawberry type. Use `relay.NodeID` for the id field:

```python
from strawberry import relay
from strawberry_orm import StrawberryORM, auto

orm = StrawberryORM("sqlalchemy", dialect="postgresql", session_getter=...)

UserFilter = orm.filter(User)
UserOrder  = orm.order(User)

@orm.type(User, filters=UserFilter, order=UserOrder)
class UserNode(relay.Node):
    id: relay.NodeID[int]
    name: auto
    email: auto
```

### Connection Fields

Use `orm.connection()` with `ORMListConnection` to create paginated connection fields. Filters and ordering from the node type are automatically wired in:

```python
from collections.abc import Iterable
from strawberry_orm.relay import ORMListConnection

@strawberry.type
class Query:
    @orm.connection(ORMListConnection[UserNode])
    def users_connection(self) -> Iterable[UserNode]:
        return orm.get_default_queryset(User)
```

This gives you:

```graphql
{
  usersConnection(
    filter: { field: { email: { contains: "example.com" } } }
    order: [{ field: { name: DESC } }]
    first: 10
    after: "YXJyYXljb25uZWN0aW9uOjk="
  ) {
    edges {
      cursor
      node { name email }
    }
    pageInfo {
      hasNextPage
      hasPreviousPage
      startCursor
      endCursor
    }
  }
}
```

Filters and ordering are applied *before* pagination, so the connection always slices from a correctly filtered and sorted result set.

`orm.connection()` accepts the same keyword arguments as `relay.connection()` — `name`, `description`, `deprecation_reason`, `extensions`, and `max_results`.

### Node Mutations

`orm.mutations.create_node()` and `orm.mutations.update_node()` generate catch-all Relay Node mutations with recursive nested inputs. See [Recursive Node Mutations](#recursive-node-mutations) for full documentation.

---

## Query Optimization

Add the optimizer extension to your schema:

```python
schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[orm.optimizer_extension()],
)
```

The optimizer executes backend query objects returned by resolvers, eager-loads relations based on the GraphQL selection set, applies field-level hints, and honors `get_queryset` hooks.

### Field Hints

Inside `@orm.type(...)`, `orm.field(...)` attaches optimizer metadata:

```python
@orm.type(Post)
class PostType:
    id: auto
    title: auto
    tags: list[TagType] = orm.field(load=["author"])
    body: auto = orm.field(only=["id", "title", "body"])
```

| Argument | Meaning |
| --- | --- |
| `load=[...]` | Extra eager-load paths. |
| `load=callable` | Custom queryset for a related field (see below). |
| `only=[...]` | Restrict loaded columns. |
| `compute={...}` | Computed-column hints for the optimizer store. |
| `disable_optimization=True` | Skip optimization for that field. |
| `description="..."` | Forward a field description to Strawberry. |

### Custom Querysets on Related Fields (`load=callable`)

When `load` is a callable, it receives the default queryset and returns a modified one:

```python
@orm.type(User)
class UserType:
    id: auto
    name: auto
    posts: list[PostType] = orm.field(
        load=lambda qs: qs.filter(is_published=True)
    )
```

This composes with `get_queryset` (type-level first, then field-level). The optimizer handles batching to avoid N+1 queries.

### Field Permissions

```python
from strawberry_orm import make_field

@orm.type(User)
class UserType:
    id: auto
    name: auto
    email: auto = make_field(permission_classes=[IsAuthenticated])
```

---

## Async Usage

`strawberry-orm` supports both sync and async execution. The same schema code works everywhere -- the only difference is how you call ORM APIs in resolvers:

| Backend | Pattern |
| --- | --- |
| Django | Sync by default. Wrap direct ORM calls with `sync_to_async(...)` in async resolvers. |
| SQLAlchemy | Pass a sync `Session` or `AsyncSession` via `session_getter`. Both work transparently. |
| Tortoise | Async-first. Use `async def` resolvers and `await` ORM calls. |

```python
# Tortoise example
@strawberry.type
class Query:
    @strawberry.field
    async def users(self) -> list[UserType]:
        return await User.all()

@strawberry.type
class Mutation:
    @strawberry.mutation
    async def create_post(self, input: CreatePostInput) -> PostType:
        return await Post.create(title=input.title, body=input.body)
```

`apply_ref_list` is sync for Django/sync-SQLAlchemy and awaitable for Tortoise/async-SQLAlchemy.

---

## Security

`strawberry-orm` has safety-focused defaults, but you still need to make deliberate schema choices.

**Defaults:**

- `orm.input()`, `orm.filter()`, and `orm.order()` exclude sensitive-looking fields (`password_hash`, `api_key`, `role`, `is_admin`, etc.)
- String regex filters are disabled by default
- Filter depth, branch count, and `inList` size are capped
- `orm.ref()` provides explicit `unlink` (remove from relation) and `delete` (hard-delete) operations — both opt-in via `unlink=True` and `delete=True`

**Your responsibility:**

- `orm.type()` does not auto-hide sensitive output fields — use `exclude=[...]` or permission classes
- List queries are unbounded unless you set `default_query_limit`
- `apply_ref_list()` only enforces authorization if you provide an `authorize` callback
- GraphQL introspection, auth, and query-complexity limits are your application's concern

A production-oriented configuration:

```python
orm = StrawberryORM(
    "sqlalchemy",
    dialect="postgresql",
    session_getter=lambda info: info.context["session"],
    default_query_limit=100,
    max_filter_depth=8,
    max_filter_branches=25,
    max_in_list_size=200,
)
```

---

## Public Exports

`StrawberryORM`, `auto`, `make_field`, `make_ref_type`, `Ordering`, `FieldDefinition`, `FieldHints`, `OptimizerExtension`, `OptimizerStore`, `UNSET`, `filter_field`, `order_field`, and the built-in lookup input classes from `strawberry_orm.filters`.

## License

MIT
