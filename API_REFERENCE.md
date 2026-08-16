# API reference

Every public name `strawberry_orm` exports, with its signature.

This is a lookup table, not a guide. For what the pieces are *for* and how they fit together, read the [README](README.md); each section here links back to the relevant part of it.

## Contents

- [Constructing an ORM](#constructing-an-orm) — `for_django`, `for_sqlalchemy`, `for_tortoise`, options
- [Building the schema](#building-the-schema) — `orm.schema()`, extensions
- [Types and inputs](#types-and-inputs) — `orm.type`, `orm.input`, `orm.partial`, `orm.ref`
- [Declaring fields](#declaring-fields) — `orm.field.eager` / `.lazy`
- [Filters, ordering, grouping](#filters-ordering-and-grouping) — generators and custom-field decorators
- [Relay](#relay) — `orm.connection.eager` / `.lazy`, `orm.node`, connection types
- [Payloads](#payloads) — `orm.payload`, `PayloadPolicy`
- [Mutations](#mutations) — `orm.create` / `update` / `delete`, node inputs, `AbstractRepo`
- [Query helpers](#query-helpers) — `orm.optimize`, `apply_filters`, `apply_ordering`
- [Class hooks](#class-hooks-you-implement) — `scope_rows`, `resolve_node`
- [Types and enums](#types-and-enums) — `auto`, `Ordering`, `FieldHints`, lookups
- [Full export list](#full-export-list)

---

## Constructing an ORM

```python
StrawberryORM.for_django(**options) -> StrawberryORM
StrawberryORM.for_sqlalchemy(*, dialect="postgresql", session_getter=None, **options) -> StrawberryORM
StrawberryORM.for_tortoise(**options) -> StrawberryORM
```

`orm.backend` exposes the configured backend instance (mostly of interest to the library itself).

### Options

Accepted by all three constructors.

| Option | Default | Meaning |
| --- | --- | --- |
| `payload` | `None` | A [`PayloadPolicy`](#payloads); required before using `orm.payload`. |
| `repos` | `{}` | `{model: AbstractRepo subclass}` for mutation authorization and lifecycle hooks. |
| `policy` | `None` | Deprecated `MutationPolicy`; use `repos` instead. |
| `warn_missing_scope` | `True` | Warn when an `@orm.type` has no `scope_rows`. |
| `warn_sensitive` | `True` | Warn when a sensitive-looking column is exposed. |
| `exclude_sensitive_fields` | `True` | Keep sensitive-looking columns out of generated inputs, filters, and orders. |
| `lazy_resolution` | `"warn"` | `"off"`, `"warn"`, or `"error"` when a relation resolves lazily. |
| `enable_optimizer` | `True` | Mount the optimizer extension from `orm.schema()`. |
| `strict_hints` | `True` | Raise at schema build when `using=` names a missing relation, or `scope=` sits on a non-relation. |
| `batch_relations` | `True` | Collapse per-parent relation resolvers into one query per shape. |
| `default_query_limit` | `None` | Cap rows returned by generated list fields. |
| `max_filter_depth` | `10` | Cap recursive filter nesting. |
| `max_filter_branches` | `50` | Cap `all` / `any` / `oneOf` branch count. |
| `max_in_list_size` | `500` | Cap `inList` / `notInList` size. |
| `enable_regex_filters` | `False` | Enable the `regex` and `iRegex` string lookups. |
| `filter_overrides` | `{}` | `{python_type: lookup_type}` to change the generated lookup for a column type. |

Backend-specific:

| Option | Backend | Default | Meaning |
| --- | --- | --- | --- |
| `dialect` | SQLAlchemy | `"postgresql"` | SQLAlchemy dialect. |
| `session_getter` | SQLAlchemy | `None` | Callable returning the session for the current request. |
| `django_async_safe` | Django | `True` | Offload sync ORM resolvers with `sync_to_async(thread_sensitive=True)`. |

See [Backend options](README.md#backend-options).

---

## Building the schema

```python
orm.schema(*, optimizer: bool | None = None, **kwargs) -> strawberry.Schema
```

Takes everything `strawberry.Schema` takes. The optimizer, lazy-resolution, and batching extensions are mounted for you; `optimizer=False` opts out. See [`orm.schema()`](README.md#ormschema).

The extensions are also available individually, for a hand-built `strawberry.Schema`:

```python
orm.optimizer_extension(**kwargs) -> type[SchemaExtension]
orm.lazy_resolution_extension(**kwargs) -> type[SchemaExtension]
orm.batching_extension() -> type[SchemaExtension]
```

`OptimizerExtension`, `LazyResolutionExtension`, and `OptimizerStore` are exported for typing and introspection.

---

## Types and inputs

```python
orm.type(model, *, name=None, include=None, exclude=None,
         filters=None, order=None, group=None, aggregate=None)
```

Class decorator mapping a model to a GraphQL type. `filters` / `order` / `group` / `aggregate` attach generated argument types to fields of this type. See [Defining types](README.md#ormtypemodel).

```python
orm.input(model, *, name=None, include=None, exclude=None, exclude_pk=True)
orm.partial(model, *, name=None, include=None, exclude=None, exclude_pk=True)
```

Generate input types — `partial` makes every field optional for patch-style updates. See [`orm.input`](README.md#orminputmodel-and-ormpartialmodel).

```python
orm.ref(model, *, create=None, update=None, unlink=False, delete=False) -> type
make_ref_type(model, *, create=None, update=None, upsert=None,
              unlink=False, delete=False, name=None) -> type
```

Build a reference input for writing related lists. `make_ref_type` is the standalone form and additionally supports `upsert`. See [Related list inputs](README.md#related-list-inputs-ormref).

---

## Declaring fields

`orm.field` is a namespace of two forms, told apart by whether your callable receives the parent row. See [The two kinds of field](README.md#the-two-kinds-of-field).

```python
orm.field.eager(fn=None, *, scope=None, filters=None, order=None, compute=None,
                disable_optimization=False, permission_classes=None,
                description=None, deprecation_reason=None)
```

A field the library resolves: one query for the whole result set. Bare, it writes the query itself; assign it to an annotated attribute. Handed a `(query, info)` callable — as `scope=`, or as a decorator — that callable narrows which rows load, running while the eager load is built and composing **after** the related type's `scope_rows`.

Nothing here sees a parent row, and passing a callable that takes `self` raises. There is deliberately no `using=`: that hint discloses relations the optimizer cannot see being read, and an eager field hides nothing from it.

```python
orm.field.lazy(fn=None, *, using=None, filters=None, order=None,
               description=None, deprecation_reason=None)
```

A field you resolve yourself: your callable receives `self` and runs once per parent row. Sync ORM work is moved off the event loop automatically. Name the relations it reads with `using=[...]` and they load alongside the parent, so those reads cost no extra query.

| Parameter | Applies to | Meaning |
| --- | --- | --- |
| `scope` | `eager` | Narrow rows through this relation edge; the keyword spelling of handing `eager` a `(query, info)` callable. |
| `using` | `lazy` | Relation names the resolver reads; they are eager-loaded alongside the parent. |
| `compute` | `eager` | Backend expressions annotated onto the query. |
| `filters` / `order` | both | Attach generated `filter` / `order` arguments to this field. |
| `disable_optimization` | `eager` | Skip the optimizer for this field, and silence its lazy-load warning. |
| `permission_classes` | `eager` | Strawberry permission classes. |

`auto`, `scoped`, `custom`, and `computed` remain as aliases: `auto` and `scoped` for `eager`, `custom` and `computed` for `lazy`.

---

## Filters, ordering, and grouping

```python
orm.filter(model_or_type, *, name=None, include=None, exclude=None)
orm.order(model_or_type, *, name=None, include=None, exclude=None, allow_scoped_ordering=None  # names, or True for all)
orm.group(model_or_type, *, name=None, include=None, exclude=None)
orm.aggregate(model_or_type, *, name=None, include=None, exclude=None)
```

Generate argument types from a model. `orm.filter_type`, `orm.order_type`, `orm.group_type`, and `orm.aggregate_type` are the class-decorator forms, for adding custom fields to a generated type. See [Filters and ordering](README.md#filters-and-ordering) and [Grouping](README.md#grouping-and-aggregation).

`filter_field`, `order_field`, `group_field`, and `aggregate_field` mark a custom field on a generated type. Each receives the client's value and the query, and returns a query:

```python
# Django
@orm.filter_type(Post)
class PostFilter:
    @filter_field
    def search(self, value: str, query):
        return query.filter(title__icontains=value)
```

See [Custom filters and ordering](README.md#custom-filters-and-ordering).

### Lookup inputs

Generated per column type, and exported so you can reference them in custom filter types.

| Lookup | Fields |
| --- | --- |
| `StringLookup` | `exact`, `neq`, `is_null`, `in_list`, `not_in_list`, `contains`, `i_contains`, `starts_with`, `i_starts_with`, `ends_with`, `i_ends_with`, `regex`, `i_regex` |
| `StringLookupNoRegex` | as above without `regex` / `i_regex` (the default unless `enable_regex_filters=True`) |
| `IntComparisonLookup`, `FloatComparisonLookup` | `exact`, `neq`, `is_null`, `in_list`, `not_in_list`, `gt`, `gte`, `lt`, `lte`, `range` |
| `DateComparisonLookup`, `DateTimeComparisonLookup`, `TimeComparisonLookup` | as the numeric lookups |
| `BooleanLookup` | `exact`, `neq`, `is_null` |
| `IDLookup`, `ReferenceLookup` | `exact`, `neq`, `is_null`, `in_list`, `not_in_list` |

---

## Relay

```python
orm.connection.eager(graphql_type=None, *, scope=None, **kwargs)
orm.connection.lazy(graphql_type=None, *, resolver=None, **kwargs)
```

A Relay connection over a model, split on the same question as fields: does your callable need the parent row?

`eager` has the library build the query, optionally narrowed by a `(query, info)` scope. The scope applies to `totalCount` and `aggregates` as well as `edges`, since those are computed from the query rather than the returned rows.

On an `@orm.type` an eager connection is served by the parent's relation, and every parent's page is taken in one query using `ROW_NUMBER() OVER (PARTITION BY ...)`, with a second grouped query for the per-parent `totalCount`. That needs a window function and a column on the related rows identifying the parent, so it is refused when the type is defined on Tortoise and for many-to-many relations, both of which point at `lazy`.

`lazy` takes a resolver receiving `self` that returns the rows, and runs once per parent row.

Either way you still get the generated `filter` / `order` / `groupBy` arguments, the grouped connection type, `totalCount`, and optimizer integration. Both also accept `name`, `description`, `deprecation_reason`, `extensions`, and `max_results`; any other keyword raises rather than being ignored. Calling `orm.connection(...)` directly still works. See [Connection fields](README.md#connection-fields).

```python
orm.node(**kwargs)
```

A Relay `node` field.

From `strawberry_orm.relay`:

| Name | Meaning |
| --- | --- |
| `ORMListConnection` | Cursor-paginated connection over any backend. The usual choice. |
| `ORMConnection` | Adds `totalCount` for the full filtered result set. |
| `Node`, `NodeID`, `GlobalID`, `Edge`, `PageInfo` | Re-exported from Strawberry for convenience. |

---

## Payloads

Resolvers that answer with `data` and `errors` instead of raising. Needs `payload=` on the ORM. See [Data / errors payloads](README.md#data--errors-payloads).

### `PayloadPolicy`

```python
PayloadPolicy(
    errors: type,
    on_error: Callable[[BaseException], Any],
    handles: tuple[type[BaseException], ...] = (Exception,),
    suffix: str = "Payload",
    types: str | None = None,
)
```

| Field | Required | Meaning |
| --- | --- | --- |
| `errors` | yes | The GraphQL type of the `errors` field. Any `@strawberry.type`, or its name when `types` is set. |
| `on_error` | yes | Called with the caught exception; returns a value of `errors`. |
| `handles` | no | Which exception types are caught at all. Default `(Exception,)`. |
| `suffix` | no | Appended to the resolver name to name the generated type. |
| `types` | no | Module where `errors` and a return annotation's named types are looked up, for names the resolver's own module never imported. |

### How a failure becomes `errors`

Exactly one of `data` and `errors` is ever set. The sequence when a resolver raises:

1. **Is the exception in `handles`?** If not, nothing is caught. The field fails as it normally would and the client gets a GraphQL error. This is how you keep a bug a bug.
2. **`on_error(exc)` is called.** Whatever it returns becomes `errors`, and `data` is null.
3. **Unless `on_error` re-raises.** An exception from inside `on_error` propagates, so the field fails with a GraphQL error instead. This is the escape hatch for a converter that only recognises some of your errors.

There is no partial state: a resolver that raises never produces a `data` value, and a resolver that returns never produces `errors`.

### A converter

The common shape is a class method on the errors type that maps known application errors and re-raises the rest:

```python
# Django
@strawberry.type
class ErrorsObject:
    message: str
    field: str | None = None

    @classmethod
    def from_exception(cls, exc: BaseException) -> "ErrorsObject":
        if isinstance(exc, ValidationError):
            return cls(message=str(exc), field=exc.field)
        if isinstance(exc, AuthorizationError):
            return cls(message="Not allowed")
        raise exc          # anything unrecognised stays a GraphQL error

orm = StrawberryORM.for_django(
    payload=PayloadPolicy(errors=ErrorsObject, on_error=ErrorsObject.from_exception),
)
```

`handles` is the alternative to that final `raise`. Naming the types you convert means an unexpected error is never routed through the converter at all, so it keeps its own traceback rather than being chained through one:

```python
# Django
PayloadPolicy(
    errors=ErrorsObject,
    on_error=ErrorsObject.from_exception,
    handles=(ValidationError, AuthorizationError),
)
```

Which to use is a question of where the list of known errors should live. `handles` keeps it next to the policy; re-raising keeps it inside the converter.

### What the client sees

```graphql
{ createPost(title: "") { data { id title } errors { message field } } }
```

```json
{ "data": { "createPost": { "data": null,
                            "errors": { "message": "Title is required",
                                        "field": "title" } } } }
```

An exception outside `handles`, or one `on_error` re-raises, produces the ordinary shape instead — `createPost` is null and the response carries a top-level `errors` array.

### Decorators

```python
orm.payload.query(fn=None, *, name=None, permission_classes=None)
orm.payload.mutation(fn=None, *, name=None, permission_classes=None, input_mutation=False)
orm.payload.connection(graphql_type=None, *, name=None, permission_classes=None)
```

The payload type is generated from the resolver's return annotation and named after the resolver (`recent_users` → `RecentUsersPayload`), overridable with `name=`. `input_mutation=True` collapses the arguments into a generated `input` argument.

`orm.payload.connection` takes the connection type, or derives it from the resolver's return annotation when omitted. It differs on failure: `data` is an **empty connection** rather than null, so a client renders the same shape whether or not the call succeeded.

Rows placed under `data` are eager-loaded for the payload's own selection.

Sync resolvers are moved off the event loop under async, decided per call, so the same resolver is correct under `execute_sync` and under an ASGI server. Async resolvers are awaited directly.

---

## Mutations

```python
orm.create(input_type, **kwargs)
orm.update(input_type, **kwargs)
orm.delete(**kwargs)
```

Generated mutation fields. See [Mutations](README.md#mutations).

```python
orm.mutations.create_node_input(*, models=None, project=None, name=None) -> type
orm.mutations.update_node_input(*, models=None, project=None, name=None) -> type
```

Catch-all Relay node mutation *inputs* with recursive nested refs; you write the resolver. See [Node mutation inputs](README.md#node-mutations).

### `AbstractRepo`

Subclass per model and pass `repos={Model: MyRepo}` to authorize writes and hook the lifecycle.

| Hook | Signature |
| --- | --- |
| `model` | class attribute naming the model |
| `scope_query` | `(query, info) -> query` |
| `can_create` | `(data, info) -> bool` |
| `can_update` | `(instance, data, info) -> bool` |
| `can_delete` | `(instance, info) -> bool` |
| `can_link` | `(parent, field, instance, info) -> bool` |
| `can_unlink` | `(parent, field, instance, info) -> bool` |
| `on_before_create` | `(data, info) -> data` |
| `on_after_create` | `(instance, info) -> None` |
| `on_before_update` | `(instance, data, info) -> data` |
| `on_after_update` | `(instance, info) -> None` |
| `on_before_delete` | `(instance, info) -> None` |

`MutationPolicy` is the deprecated predecessor.

---

## Query helpers

```python
orm.optimize(data, info, *, at: str | Sequence[str] | None = None) -> Any
```

Eager-load what the current selection needs from rows already in memory. Rarely needed: a resolver's return value is optimized automatically, including rows under a payload's `data`. Accepts a query object, a model instance, or a list, and returns anything else untouched. `at` re-roots the selection when the rows sit below the resolved field. Awaitable on async backends. See [Rows a resolver already fetched](README.md#rows-a-resolver-already-fetched).

```python
orm.get_default_queryset(model) -> query
orm.is_query_object(value) -> bool
orm.apply_filters(query, filter_input, model, info=None) -> query
orm.apply_ordering(query, order_input, model, info=None) -> query
orm.apply_ref_list(instance, field, refs, info, *, authorize=None)
```

`apply_filters` and `apply_ordering` apply a generated input to a query by hand, for resolvers that build their own. `apply_ref_list` writes a related list from `orm.ref` input, honouring the model's repo.

---

## Class hooks you implement

Defined on an `@orm.type` class, not called by you.

```python
# Django / Tortoise
@classmethod
def scope_rows(cls, queryset, info):
    return queryset.filter(is_published=True)
```

```python
# SQLAlchemy
@classmethod
def scope_rows(cls, select, info):
    return select.where(Post.is_published.is_(True))
```

Row-level access control. Applied wherever this model's rows load — as a root query, through a relation, during filter traversal, and when relations are read off rows already in memory. One per model. See [`scope_rows`](README.md#scope_rows--one-model-one-type).

```python
@classmethod
def resolve_node(cls, node_id, *, info, **kwargs):
    ...
```

Strawberry's Relay node resolution hook.

---

## Types and enums

| Name | Meaning |
| --- | --- |
| `auto` | Annotation asking the library to infer the field type from the column. |
| `Ordering` | `ASC`, `DESC`, `ASC_NULLS_FIRST`, `ASC_NULLS_LAST`, `DESC_NULLS_FIRST`, `DESC_NULLS_LAST` |
| `DateGroupByInterval` | `DAY`, `WEEK`, `MONTH`, `QUARTER`, `YEAR` |
| `DateGroupByOption` | Input pairing a date column with an interval. |
| `FieldDefinition` | What `orm.field.eager` returns for a scope or metadata: `using`, `scope`, `compute`, `disable_optimization`, `permission_classes`, `description`, `declared_type`. |
| `FieldHints` | The optimizer-relevant subset: `using`, `scope`, `compute`, `disable_optimization`. |
| `OperationInfo` | `messages: list[OperationMessage]` |
| `OperationMessage` | `kind`, `field`, `message` |
| `UNSET` | Strawberry's sentinel, re-exported to distinguish "absent" from "null" in partial inputs. |

---

## Full export list

Everything in `strawberry_orm.__all__`:

`AbstractRepo`, `BooleanLookup`, `DateComparisonLookup`, `DateGroupByInterval`, `DateGroupByOption`, `DateTimeComparisonLookup`, `FieldDefinition`, `FieldHints`, `FloatComparisonLookup`, `IDLookup`, `IntComparisonLookup`, `LazyResolutionExtension`, `MutationPolicy`, `OperationInfo`, `OperationMessage`, `OptimizerExtension`, `OptimizerStore`, `Ordering`, `PayloadPolicy`, `ReferenceLookup`, `StrawberryORM`, `StringLookup`, `StringLookupNoRegex`, `TimeComparisonLookup`, `UNSET`, `aggregate_field`, `auto`, `filter_field`, `group_field`, `make_ref_type`, `order_field`

From `strawberry_orm.relay`:

`Edge`, `GlobalID`, `Node`, `NodeID`, `ORMConnection`, `ORMListConnection`, `PageInfo`
