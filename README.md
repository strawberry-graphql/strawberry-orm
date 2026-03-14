# strawberry-orm

Unified, backend-agnostic ORM abstraction for [Strawberry GraphQL](https://strawberry.rocks/). Write your GraphQL schema once, swap between Django, SQLAlchemy, and Tortoise ORM via optional dependency extras.

## Installation

```bash
# Base (no ORM -- just shared types)
uv add strawberry-orm

# With Django
uv add strawberry-orm[django]

# With SQLAlchemy
uv add strawberry-orm[sqlalchemy]

# With Tortoise ORM
uv add strawberry-orm[tortoise]
```

## Quick Start

```python
from strawberry_orm import StrawberryORM

# Choose your backend
orm = StrawberryORM("sqlalchemy", dialect="postgresql", session_getter=get_session)
# orm = StrawberryORM("django")
# orm = StrawberryORM("tortoise")
```

### Type Generation

```python
UserType   = orm.type(User)
UserInput  = orm.input(User)
UserFilter = orm.filter(User)
UserOrder  = orm.order(User)
```

### Queries with Filtering, Ordering, and Optimizer Hints

```python
import strawberry

@strawberry.type
class Query:
    users: list[UserType] = orm.field(
        filter_input=UserFilter,
        order_by=UserOrder,
        pagination=True,
        load=["profile", "posts"],
        only=["id", "name", "email"],
    )
```

### Mutations

```python
@strawberry.type
class Mutation:
    create_user: UserType = orm.create(UserInput)
    update_user: UserType = orm.update(UserInput, filter_input=UserFilter)
    delete_user: bool     = orm.delete(filter_input=UserFilter)
```

### Related List Mutations

Manage related lists declaratively -- send the desired final state:

```python
CreateTagInput = orm.input(Tag, include=["name"])
UpdateTagInput = orm.input(Tag, include=["id", "name"])
TagRef = orm.ref(Tag, create=CreateTagInput, update=UpdateTagInput, delete=True)

@orm.input(Post)
class UpdatePostInput:
    title: str | None = None
    tags: list[TagRef] | None = None
```

```graphql
mutation {
  updatePost(id: "1", input: {
    tags: [
      { id: "tag-2" },
      { update: { id: "tag-1", name: "renamed" } },
      { create: { name: "new-tag" } },
      { delete: { id: "tag-3" } }
    ]
  })
}
```

### Relay Connections

```python
@strawberry.type
class Query:
    user_connection = orm.connection(filter_input=UserFilter, order_by=UserOrder)
    user_node       = orm.node()
```

### Schema with Optimizer

```python
schema = strawberry.Schema(
    query=Query,
    mutation=Mutation,
    extensions=[orm.optimizer_extension()],
)
```

## Queryset Overrides

### Type-level scoping

```python
@orm.type(User)
class UserType:
    id: auto
    name: auto

    @classmethod
    def get_queryset(cls, queryset, info):
        return queryset.filter(is_deleted=False)
```

### Root-level resolver returning a query object

```python
@strawberry.type
class Query:
    @orm.field
    def active_users(self, info) -> list[UserType]:
        # Django:     return User.objects.filter(is_active=True)
        # SQLAlchemy: return select(User).where(User.is_active == True)
        # Tortoise:   return User.filter(is_active=True)
        ...
```

## Filter Spec

Filters use a recursive `@oneOf` expression tree:

```graphql
{ posts(filter: { field: { title: { contains: "hello" } } }) }

{ posts(filter: {
    all: [
      { field: { title: { contains: "hello" } } }
      { field: { isActive: { exact: true } } }
    ]
}) }

{ posts(filter: {
    not: { field: { author: { name: { exact: "Alice" } } } }
}) }
```

## License

MIT
