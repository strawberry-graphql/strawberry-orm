# strawberry-orm

Backend-agnostic schema generation for [Strawberry GraphQL](https://strawberry.rocks/) on top of Django ORM, SQLAlchemy, and Tortoise ORM.

`strawberry-orm` helps you keep one Strawberry schema style across multiple ORMs. It focuses on:

- model-backed Strawberry types
- generated input, filter, and order types
- list fields that expose filtering and ordering automatically
- query optimization hooks to reduce N+1 queries
- helpers for related-list mutation inputs

## Installation

```bash
# Base package
uv add strawberry-orm

# With a backend
uv add strawberry-orm[django]
uv add strawberry-orm[sqlalchemy]
uv add strawberry-orm[tortoise]
```

You can do the same with `pip`:

```bash
pip install "strawberry-orm[sqlalchemy]"
```

Requirements:

- Python `>=3.10`
- `strawberry-graphql>=0.311.0`

## Quick Start

1. Create a backend instance.
2. Generate Strawberry types from ORM models with `@orm.type(...)`.
3. Generate filter/order types from the same models.
4. Expose list fields with `orm.field()`.
5. Add `orm.optimizer_extension()` to the schema.

```python
import strawberry

from strawberry_orm import StrawberryORM, auto

orm = StrawberryORM(
    "sqlalchemy",
    dialect="postgresql",
    session_getter=lambda info: info.context["session"],
)

UserFilter = orm.filter(User)
UserOrder = orm.order(User)


@orm.type(User, filters=UserFilter, order=UserOrder)
class UserType:
    id: auto
    name: auto
    email: auto


@strawberry.type
class Query:
    users: list[UserType] = orm.field()


schema = strawberry.Schema(
    query=Query,
    extensions=[orm.optimizer_extension()],
)
```

That single `users` field will:

- start from the backend's default queryset for `User`
- accept `filter` and `order` arguments automatically because `UserType` carries them
- let the optimizer eagerly load related data based on the GraphQL selection set

---

## Backends

| Backend | Constructor | Notes |
| --- | --- | --- |
| Django | `StrawberryORM("django")` | Uses Django querysets directly. |
| SQLAlchemy | `StrawberryORM("sqlalchemy", dialect="postgresql", session_getter=...)` | Requires a SQLAlchemy session at resolve time. |
| Tortoise | `StrawberryORM("tortoise")` | Async ORM; use Strawberry in async mode. |

### Django

```python
orm = StrawberryORM("django")
```

### SQLAlchemy

```python
orm = StrawberryORM(
    "sqlalchemy",
    dialect="postgresql",
    session_getter=lambda info: info.context["session"],
)
```

SQLAlchemy needs a session when a query is executed. `strawberry-orm` can obtain it from either:

- `session_getter=...`
- `info.context["session"]`
- `info.context.session`
- `info.context.get_session()`

If your context stores a callable session factory, pass a `session_getter` instead of putting the callable directly on `info.context`.

### Tortoise

```python
orm = StrawberryORM("tortoise")
```

### Backend Options

Shared options:

| Option | Default | Meaning |
| --- | --- | --- |
| `default_query_limit` | `None` | Adds a default limit to list queries created from the backend default queryset. |
| `exclude_sensitive_fields` | `True` | Excludes sensitive-looking fields from generated input/filter/order types. |
| `warn_sensitive` | `True` | Emits warnings when sensitive-looking fields are exposed on generated output types. |
| `hard_delete_refs` | `False` | Makes `apply_ref_list(..., delete=...)` delete related rows instead of only unlinking them. |
| `max_filter_depth` | `10` | Caps recursive filter nesting. |
| `max_filter_branches` | `50` | Caps the total number of `all` / `any` / `oneOf` branches. |
| `max_in_list_size` | `500` | Caps `inList` / `notInList` filter size. |
| `enable_regex_filters` | `False` | Enables `regex` and `iRegex` string lookups. |

SQLAlchemy-only options:

| Option | Default | Meaning |
| --- | --- | --- |
| `dialect` | `"postgresql"` | Chooses SQLAlchemy dialect-specific behavior. |
| `session_getter` | `None` | Returns the session for the current request. |
| `filter_overrides` | `{}` | Maps Python types to custom lookup input types. |

---

## Defining Types

### `@orm.type(Model)`

Use `@orm.type(Model)` to turn an ORM model into a Strawberry object type.

```python
from strawberry_orm import auto


@orm.type(User)
class UserType:
    id: auto
    name: auto
    email: auto
```

`auto` is an alias for `strawberry.auto`. The backend inspects the model and fills in the Python type for each field.

Keyword arguments:

- `include=[...]`
- `exclude=[...]`
- `name="CustomGraphQLTypeName"`
- `filters=UserFilter`
- `order=UserOrder`

```python
@orm.type(User, include=["id", "name"], name="PublicUser")
class PublicUserType:
    id: auto
    name: auto


@orm.type(User, exclude=["password_hash", "api_key"])
class SafeUserType:
    id: auto
    name: auto
    email: auto
```

### Relations

Reference other generated types directly. The backend auto-generates resolvers for relationship fields:

```python
@orm.type(Tag)
class TagType:
    id: auto
    name: auto


@orm.type(Post)
class PostType:
    id: auto
    title: auto
    tags: list[TagType]
```

If the nested type carries `filters` and/or `order`, list relations expose those arguments too.

### Custom Strawberry Fields

You can mix generated fields with plain Strawberry fields:

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

### Type-Level Queryset Scoping

Define a `get_queryset` classmethod on a type to scope the model query centrally. When the optimizer extension is installed and a resolver returns a backend query object, `get_queryset` is applied automatically.

```python
@orm.type(Post)
class PublishedPostType:
    id: auto
    title: auto
    is_published: auto

    @classmethod
    def get_queryset(cls, qs, info):
        return qs.filter(is_published=True)  # Django / Tortoise style
        # return qs.where(Post.is_published == True)        # SQLAlchemy style
```

This is useful for soft-delete filtering, multi-tenant scoping, "published only" content types, and reusable authorization-aware model filters.

### `orm.input(Model)`

Generates a Strawberry input type from model metadata.

```python
CreateUserInput = orm.input(User, include=["name", "email"])
```

Generated input fields are optional (defaulting to `strawberry.UNSET`), skip relations, exclude primary keys by default, and exclude sensitive-looking fields unless explicitly included.

Keyword arguments: `include`, `exclude`, `exclude_pk=False`, `name`.

### `orm.partial(Model)`

```python
UpdateUserInput = orm.partial(User, include=["name", "email"])
```

Same logic as `input()` with a default name like `UserPartialInput`. Useful for patch-style update payloads.

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
# OR condition
{
  users(filter: {
    any: [
      { field: { name: { exact: "Alice" } } }
      { field: { name: { exact: "Bob" } } }
    ]
  }) { name }
}

# AND condition
{
  posts(filter: {
    all: [
      { field: { authorId: { exact: 1 } } }
      { field: { isPublished: { exact: true } } }
    ]
  }) { title }
}

# NOT condition
{
  users(filter: {
    not: { field: { email: { contains: "example.com" } } }
  }) { name }
}
```

#### Built-in Lookup Types

The package exports reusable lookup inputs:

`StringLookup`, `BooleanLookup`, `IDLookup`, `IntComparisonLookup`, `FloatComparisonLookup`, `DateComparisonLookup`, `TimeComparisonLookup`, `DateTimeComparisonLookup`

Typical string lookups: `exact`, `neq`, `contains`, `iContains`, `startsWith`, `iStartsWith`, `endsWith`, `iEndsWith`, `inList`, `notInList`, `isNull`.

Regex lookups (`regex`, `iRegex`) are disabled by default. Enable with `enable_regex_filters=True`.

### Ordering

Generate an order input:

```python
UserOrder = orm.order(User)
```

The generated type is a `@oneOf` input — each entry specifies exactly one column.
The `order` argument is a **list**, where position determines tie-break priority:

```graphql
{
  users(order: [{ name: ASC }, { email: DESC }]) {
    name
    email
  }
}
```

This sorts by `name` ascending first, then breaks ties by `email` descending.

Supported values from the `Ordering` enum: `ASC`, `ASC_NULLS_FIRST`, `ASC_NULLS_LAST`, `DESC`, `DESC_NULLS_FIRST`, `DESC_NULLS_LAST`.

Filters and ordering can be combined:

```graphql
{
  posts(
    filter: { field: { isPublished: { exact: true } } }
    order: [{ title: DESC }]
  ) {
    title
  }
}
```

---

## Queries

### Automatic List Fields

If a field returns `list[SomeType]`, `orm.field()` builds the resolver from the model attached to that type:

```python
@orm.type(User, filters=UserFilter, order=UserOrder)
class UserType:
    id: auto
    name: auto
    email: auto


@strawberry.type
class Query:
    users: list[UserType] = orm.field()
```

You can also supply filter and order types explicitly:

```python
@strawberry.type
class Query:
    users: list[UserType] = orm.field(filters=UserFilter, order=UserOrder)
```

### Explicit Resolvers

For custom scoping, join logic, or backend-specific behavior, return a backend query object from a normal Strawberry resolver:

```python
@strawberry.type
class Query:
    @strawberry.field
    def active_users(self, info: strawberry.types.Info) -> list[UserType]:
        return select(User).where(User.is_active.is_(True))  # SQLAlchemy
        # return User.objects.filter(is_active=True)         # Django
        # return User.filter(is_active=True)                 # Tortoise
```

This works with the optimizer extension and with type-level `get_queryset` hooks.

---

## Mutations

Write plain `@strawberry.mutation` resolvers and use `strawberry-orm` for the generated input types:

```python
CreatePostInput = orm.input(Post, include=["title", "body", "author_id"])


@strawberry.input
class UpdatePostInput:
    id: int
    title: str | None = strawberry.UNSET
    body: str | None = strawberry.UNSET


@strawberry.type
class Mutation:
    @strawberry.mutation
    def create_post(self, info: strawberry.types.Info, input: CreatePostInput) -> PostType:
        post = Post(title=input.title, body=input.body, author_id=input.author_id)
        ...
        return post

    @strawberry.mutation
    def update_post(self, info: strawberry.types.Info, input: UpdatePostInput) -> PostType | None:
        ...
```

### Related List Inputs (`orm.ref`)

`orm.ref(...)` generates a `@oneOf` input for managing related lists:

```python
CreateTagInput = orm.input(Tag, include=["name"])


@strawberry.input
class UpdateTagInput:
    id: strawberry.ID
    name: str


TagRef = orm.ref(Tag, create=CreateTagInput, update=UpdateTagInput, delete=True)
```

Each ref in the list can be one of:

- `{ id: "1" }` — link an existing row
- `{ create: { ... } }` — create a related row inline
- `{ update: { id: "...", ... } }` — update an existing related row
- `{ delete: { id: "..." } }` — unlink (or delete if `hard_delete_refs=True`)

Apply ref operations in a mutation:

```python
@strawberry.mutation
def set_post_tags(self, info: strawberry.types.Info, post_id: int, tags: list[TagRef]) -> PostType | None:
    post = ...
    orm.apply_ref_list(post, "tags", tags, info)
    return post
```

```graphql
mutation {
  setPostTags(postId: 1, tags: [
    { id: "2" }
    { update: { id: "1", name: "python3" } }
    { create: { name: "new-tag" } }
    { delete: { id: "3" } }
  ]) {
    id
    tags { id name }
  }
}
```

`apply_ref_list` supports `mode="replace"` (default, replaces the entire list) and `mode="patch"` (only touches mentioned items). An optional `authorize` callback receives `(action, model, obj_id, info)` and returns `bool`.

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

The optimizer:

- executes backend query objects returned by your resolvers
- eager-loads relations based on the GraphQL selection set
- applies field-level hints registered through `orm.field(...)`
- honors type-level `get_queryset` hooks

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
| `load=[...]` | Extra eager-load paths to apply. |
| `load=callable` | A callable that customises the queryset for a related field (see below). |
| `only=[...]` | Restrict loaded columns. |
| `compute={...}` | Register computed-column hints for the optimizer store. |
| `disable_optimization=True` | Skip optimization for that field. |
| `description="..."` | Forward a field description to Strawberry. |

### Custom Querysets on Related Fields (`load=callable`)

When `load` is a callable instead of a list, it receives the default queryset for the related model and returns a modified queryset. This lets you filter, reorder, or limit related objects from the parent level:

```python
@orm.type(User)
class UserType:
    id: auto
    name: auto
    posts: list[PostType] = orm.field(
        load=lambda qs: qs.filter(is_published=True)
    )
```

How each backend applies the callable:

- **Django** — wraps the relation in a `Prefetch` object with the custom queryset.
- **SQLAlchemy** — extracts `WHERE` criteria from the modified `select()` and applies them via `relationship.and_(...)`.
- **Tortoise** — performs a separate batch query filtered by parent IDs and assigns results back to each parent instance.

This composes with type-level `get_queryset`. If the related type defines `get_queryset` *and* the field has a `load` callable, both are applied (type-level first, then the field-level callable):

```python
@orm.type(Post)
class PublishedPostType:
    id: auto
    title: auto

    @classmethod
    def get_queryset(cls, qs, info):
        return qs.filter(is_published=True)


@orm.type(User)
class UserType:
    id: auto
    name: auto
    posts: list[PublishedPostType] = orm.field(
        load=lambda qs: qs.order_by("-created_at")
    )
```

The optimizer handles batching, so this avoids N+1 queries even with custom filtering.

### Field Permissions

Use `make_field(...)` to attach Strawberry permission classes to a generated field:

```python
from strawberry_orm import make_field


@orm.type(User)
class UserType:
    id: auto
    name: auto
    email: auto = make_field(permission_classes=[IsAuthenticated])
```

---

## Security

`strawberry-orm` has safety-focused defaults, but you still need to make deliberate schema choices.

**Defaults:**

- `orm.input()`, `orm.filter()`, and `orm.order()` exclude sensitive-looking fields such as `password_hash`, `api_key`, `role`, and `is_admin` by default
- String regex filters are disabled by default
- Filter depth, branch count, and `inList` size are capped by default
- `orm.ref(..., delete=True)` unlinks by default; hard deletes require `hard_delete_refs=True`

**Caveats:**

- `orm.type()` does not auto-hide sensitive output fields. It warns by default, but you must still use `exclude=[...]` or permission classes to protect them.
- List queries are unbounded unless you set `default_query_limit=...`
- `apply_ref_list()` only enforces authorization if you provide an `authorize` callback
- GraphQL introspection, auth, and query-complexity limits are still your application's responsibility

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

Top-level exports from `strawberry_orm`:

`StrawberryORM`, `auto`, `make_field`, `make_ref_type`, `Ordering`, `FieldDefinition`, `FieldHints`, `OptimizerExtension`, `OptimizerStore`, `UNSET`, and the built-in lookup input classes from `strawberry_orm.filters`.

## License

MIT
