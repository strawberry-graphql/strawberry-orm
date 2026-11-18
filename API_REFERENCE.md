# API reference

Every public name `strawberry_orm` exports, with its signature.

This is a lookup table, not a guide. For what the pieces are *for* and how they fit together, read the [README](README.md); each section here links back to the relevant part of it.

## Contents

- [Constructing an ORM](#constructing-an-orm) — `for_django`, `for_sqlalchemy`, `for_tortoise`, options
- [Building the schema](#building-the-schema) — `orm.schema()`, extensions, `tags`
- [Types and inputs](#types-and-inputs) — `orm.type`, `orm.input`, `orm.partial`, `orm.ref`
- [Declaring fields](#declaring-fields) — `orm.field.eager` / `.lazy`
- [Filters, ordering, grouping](#filters-ordering-and-grouping) — generators and custom-field decorators
- [Relay](#relay) — `orm.connection.eager` / `.lazy`, `orm.node`, connection types
- [Payloads](#payloads) — `orm.payload`, `PayloadPolicy`
- [Mutations](#mutations) — node inputs, `AbstractRepo`, the deprecated `orm.create` / `update` / `delete`
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
| `warn_missing_scope` | `True` | Warn when an `@orm.type` has no `scope_rows`. `STRAWBERRY_ORM_WARN_MISSING_SCOPE=0` turns it off when the option is not given. |
| `warn_sensitive` | `True` | Warn when a sensitive-looking column is exposed. |
| `exclude_sensitive_fields` | `True` | Keep sensitive-looking columns out of generated inputs, filters, and orders. |
| `lazy_resolution` | `"warn"` | `"off"`, `"warn"`, or `"error"` when a relation resolves lazily. |
| `enable_optimizer` | `True` | Mount the optimizer extension from `orm.schema()`. |
| `strict_hints` | `True` | Raise at schema build when `using=` names a missing relation, or `scope=` sits on a non-relation. |
| `batch_relations` | `True` | Collapse per-parent relation resolvers whose queries are identical apart from the parent key into one query. |
| `default_query_limit` | `None` | Cap each connection page (its size when `first`/`last` are not given), each generated root list, and each root list an `@orm.field.eager()` or `orm.field.lazy(filters=…, order=…)` resolver returns as a query, after its filter, order and scope. Nested relation lists, and a resolver's own query on a field with no generated filter or order, are not capped. |
| `max_filter_depth` | `10` | Cap filter nesting: each `not`, `all` / `any` / `oneOf` entry and `object` hop is a level, the foreign-key shortcut included. |
| `max_filter_branches` | `50` | Cap the `all` / `any` / `oneOf` entries across one filter argument, each reference to a variable included. |
| `max_in_list_size` | `500` | Cap the `inList` / `notInList` values (two per `range`) across one filter argument, each reference to a variable included. |
| `max_regex_filters` | `3` | Cap the `regex` and `iRegex` lookups across one filter argument and across one request. |
| `max_filter_text_length` | `1000` | Cap the characters of `contains`, `startsWith` and `endsWith` text, `i` forms included, across one filter argument and across one request. |
| `max_filter_value_length` | `100000` | Cap the characters of every value a filter binds, each reference to a variable counted, across one filter argument and across one request. |
| `max_order_entries` | `10` | Cap the entries in an `order` list. |
| `max_group_by_entries` | `10` | Cap the entries in a `groupBy` list. |
| `max_groups` | `100` | Refuse a connection whose `groupBy` makes more groups. |
| `max_group_key_rows` | `100` | Refuse an `objects:` group key holding more related rows. |
| `enable_regex_filters` | `False` | Enable the `regex` and `iRegex` string lookups (raises `ValueError` on Tortoise). |
| `max_regex_text_length` | `256` | Longest value a regex is tested on, in characters (bytes on MySQL); longer values do not match. |
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
orm.schema(*, optimizer: bool | None = None, tags: str | Iterable[str] | None = None,
           schema_class: type[strawberry.Schema] = strawberry.Schema,
           **kwargs) -> strawberry.Schema
```

Takes everything `strawberry.Schema` takes. The optimizer, lazy-resolution, and batching extensions are mounted for you; `optimizer=False` opts out. See [`orm.schema()`](README.md#ormschema).

`tags` builds the schema for those tags. It keeps every field, argument and input field that is untagged or carries one of them, and the types those still reach. Building fails, naming the field, for a hidden argument or input field without a default, and for a type whose every field is hidden. `schema_class` is the class built. See [Several schemas from one set of types](README.md#several-schemas-from-one-set-of-types-tags).

```python
tags(*names: str) -> dict
TAGS_KEY = "strawberry_orm.tags"
```

`tags()` is the field metadata for something declared with Strawberry itself: `strawberry.field(metadata=tags("internal"))`, and likewise for `strawberry.mutation`, `strawberry.argument` and input fields. The ORM helpers take `tags=` directly.

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

`exclude=` cannot name a field the class inherits from a Strawberry type or interface, which Strawberry shows on every subclass; doing so raises `ValueError`. Declare such a column only on the types that show it.

`include=` has to list every field the class declares or inherits, other than one with a resolver of its own: it cannot take a field off the class, so leaving one out raises `ValueError` rather than showing it anyway.

```python
orm.input(model, *, name=None, include=None, exclude=None, exclude_pk=True)
orm.partial(model, *, name=None, include=None, exclude=None, exclude_pk=True)
```

Generate input types — `partial` makes every field optional for patch-style updates. See [`orm.input`](README.md#orminputmodel-and-ormpartialmodel).

```python
orm.ref(model, *, create=None, update=None, unlink=False, delete=False) -> type
make_ref_type(model, *, create=None, update=None, upsert=None,
              unlink=False, delete=False, name=None, descriptions=None) -> type
```

Build a reference input for writing related lists. `make_ref_type` is the standalone form and additionally supports `upsert`; `descriptions` maps variant names to GraphQL field descriptions. See [Related list inputs](README.md#related-list-inputs-ormref).

---

## Declaring fields

`orm.field` is a namespace of two forms, told apart by whether your callable receives the parent row. See [The two kinds of field](README.md#the-two-kinds-of-field).

```python
orm.field.eager(fn=None, *, scope=None, filters=None, order=None, compute=None,
                disable_optimization=False, permission_classes=None,
                description=None, deprecation_reason=None, tags=None, metadata=None)
```

A field the library resolves: one query for the whole result set. Bare, it writes the query itself; assign it to an annotated attribute. Handed a `(query, info)` callable — as `scope=`, or as a decorator — that callable narrows which rows load, running while the eager load is built and composing **after** the related type's `scope_rows`. A limit, offset or fetch it adds is refused; `.limit(0)`, `[:0]` and `.none()` hide every row.

Nothing here sees a parent row, and passing a callable that takes `self` raises. There is deliberately no `using=`: that hint discloses relations the optimizer cannot see being read, and an eager field hides nothing from it.

```python
orm.field.lazy(fn=None, *, using=None, filters=None, order=None,
               description=None, deprecation_reason=None, tags=None, metadata=None)
```

A field you resolve yourself: your callable receives `self` and runs once per parent row. Sync ORM work is moved off the event loop automatically. Name the relations it reads with `using=[...]` and they load alongside the parent under each hop's scopes. Before your callable runs, a relation holding anything else is read again under those scopes, which costs a query.

| Parameter | Applies to | Meaning |
| --- | --- | --- |
| `scope` | `eager` | Narrow rows through this relation edge; the keyword spelling of handing `eager` a `(query, info)` callable. |
| `using` | `lazy` | Relations the resolver reads, eager-loaded alongside the parent. An entry can walk several hops, `"author.posts"` or `"author__posts"`, and each hop is scoped as a selection of it would be. |
| `compute` | `eager` | Backend expressions annotated onto the query. |
| `filters` / `order` | both | Attach generated `filter` / `order` arguments to this field. On an `@orm.type`, `eager` takes them only on a to-many relation, where an argument not given falls back to the related type's; on a to-one relation, a column or an `on=` view they raise. |
| `disable_optimization` | `eager` | Skip the optimizer for this field, and silence its lazy-load warning. |
| `permission_classes` | `eager` | Strawberry permission classes. |
| `tags` | both | Schemas the field belongs to; see [`orm.schema`](#building-the-schema). Also taken by `orm.field.computed`, `orm.connection.*`, `orm.node` and `orm.payload.*`. |
| `metadata` | both | Strawberry field metadata. Tags are merged into it. |

`auto`, `scoped`, `custom`, and `computed` remain as aliases: `auto` and `scoped` for `eager`, `custom` and `computed` for `lazy`.

---

## Filters, ordering, and grouping

```python
orm.filter(model_or_type, *, name=None, include=None, exclude=None)
orm.order(model_or_type, *, name=None, include=None, exclude=None, allow_scoped_ordering=None  # names, or True for all)
orm.group(model_or_type, *, name=None, include=None, exclude=None)
orm.aggregate(model_or_type, *, name=None, include=None, exclude=None)
```

Generate argument types from a model. In a schema built with `orm.schema()`, each covers only the columns and relations shown by the types it serves there — the types it is attached to, and through `object:` the types relations return — so nothing a type hides can be filtered, sorted, grouped or aggregated on. `orm.filter_type`, `orm.order_type`, `orm.group_type`, and `orm.aggregate_type` are the class-decorator forms, for adding custom fields to a generated type. See [Filters and ordering](README.md#filters-and-ordering) and [Grouping](README.md#grouping-and-aggregation).

`filter_field`, `order_field`, `group_field`, and `aggregate_field` mark a custom field on a generated type. Each receives the client's value and the query, and returns a query:

```python
# Django
@orm.filter_type(Post)
class PostFilter:
    @filter_field
    def search(self, value: str, query):
        return query.filter(title__icontains=value)
```

A `filter_field` narrows the field's own query only at the top of the filter (alone or in a top-level `all`). Under `not`, in `any` or `oneOf`, or through `object`, it gets a fresh query over its filter's model, and the rows it keeps are matched by primary key.

See [Custom filters and ordering](README.md#custom-filters-and-ordering).

### Lookup inputs

Generated per column type, and exported so you can reference them in custom filter types.

| Lookup | Fields |
| --- | --- |
| `StringLookup` | `exact`, `neq`, `is_null`, `in_list`, `not_in_list`, `contains`, `i_contains`, `starts_with`, `i_starts_with`, `ends_with`, `i_ends_with`, `regex`, `i_regex` (not on Tortoise) |
| `StringLookupNoRegex` | as above without `regex` / `i_regex` (the default unless `enable_regex_filters=True`, and always on Tortoise) |
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

On an `@orm.type` an eager connection is served by the parent's relation, and every parent's page is taken in one query using `ROW_NUMBER() OVER (PARTITION BY ...)`, with a second grouped query for the per-parent `totalCount`. A page asked for without `first` is windowed at the size the connection returns; `last` without `first`, or a selection of `aggregates` or `groups`, is read one parent at a time instead. All three backends support this; Tortoise, which has no window expression, wraps its own parameterized SQL to do the numbering. It does need a column on the related rows identifying the parent, so a many-to-many is refused when the type is defined and pointed at `lazy`.

`lazy` takes a resolver receiving `self` that returns the rows, and runs once per parent row. Return a query rather than a list and each parent's page is cut in SQL, like any other connection's.

Either way you still get the generated `filter` / `order` / `groupBy` arguments, the grouped connection type, `totalCount`, and optimizer integration. Both also accept `name`, `description`, `deprecation_reason`, `extensions`, and `max_results`; any other keyword raises rather than being ignored. A connection class of your own, a subclass of `ORMListConnection[Node]` or a generic one parameterized by the node, is kept with its fields. The node type has to be declared before the field, named directly or by a name already bound in its module; a connection that cannot be built raises. Calling `orm.connection(...)` directly still works. See [Connection fields](README.md#connection-fields).

```python
orm.node(**kwargs)
```

A Relay `node` field.

From `strawberry_orm.relay`:

| Name | Meaning |
| --- | --- |
| `ORMListConnection` | Cursor-paginated connection over any backend. Given a query, it reads only the requested page. The usual choice. |
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
orm.payload.query(fn=None, *, name=None, permission_classes=None, tags=None, metadata=None)
orm.payload.mutation(fn=None, *, name=None, permission_classes=None, input_mutation=False,
                     tags=None, metadata=None)
orm.payload.connection(graphql_type=None, *, name=None, permission_classes=None,
                       tags=None, metadata=None)
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

**Deprecated.** Each builds a field with no resolver: it writes nothing and asks no `AbstractRepo`, so it authorizes nothing either. Write a `@strawberry.mutation` that writes through `orm.apply_upsert()` or `orm.apply_ref_list()`, which hold the write to the model's repo. Each call raises a `DeprecationWarning`. See [Mutations](README.md#mutations).

```python
orm.mutations.create_node_input(*, models=None, project=None, name=None) -> type
orm.mutations.update_node_input(*, models=None, project=None, name=None) -> type
orm.mutations.upsert_node_input(*, model, project, name=None) -> type
```

Catch-all Relay node mutation *inputs* with recursive nested refs; you write the resolver. `upsert_node_input` builds a single-model `{where, create, update}` root from a project that declares `_meta.upsert.where`; the `create` member is present only when the projection names `create`, and `update` only when matched rows are updated. See [Node mutation inputs](README.md#node-mutations) and [Upserting](README.md#upserting-with-_metaupsert).

`project` `_meta` keys:

| Key | Value |
| --- | --- |
| `create`, `update` | `True` or a list of field names. A projection naming none of `create`, `update`, `upsert`, `unlink` and `delete` offers `create` and `update`; one naming any offers only those it names, through every arm, an upsert's `create` and `update` included, and `create_node_input()`/`update_node_input()` refuse a model whose projection lacks their operation |
| `unlink`, `delete` | `True` (to-many relations only); absent unless set |
| `onReplace` | `"DISCONNECT"`, `"DELETE"` or a list of them (single links only); a replaced row is only disconnected unless `"DELETE"` is listed, and one the related repo's `scope_query` hides or `authorize` will not delete is never deleted; a delete whose cascade takes a row its repo hides or will not delete, or that `authorize` denies, is refused |
| `upsert.where` | a list of field names to match on, or `True` for caller-chosen optional fields |
| `upsert.onMatch` | `"UPDATE"` (the default with `update`; requires `update` when set), `"IGNORE"` (the default without it) or `"APPEND"` |
| `upsert.onMatchLinks` | `"ERROR"` (default) or `"IGNORE"`; only with `onMatch: "APPEND"` |
| `immutable` | `True`: no `update`, `unlink`, `delete` or `onReplace`; matches `APPEND` unless `onMatch: "IGNORE"` |
| `maxVisits` | positive integer: how many times the model may appear on one nesting path |
| `name` | a name for the projection's generated types, which also lets it recurse |

Contradictory keys raise `ValueError` when the input is built, as does naming a column that the model's `@orm.type` excludes; generated inputs leave such columns out. See [What a match does](README.md#what-a-match-does-onmatch-and-immutable).

```python
orm.apply_upsert(input, info, *, authorize=None) -> instance
```

Executes an input built by `upsert_node_input()` as one unit of work and returns the matched or created row. The unit is a savepoint of the SQLAlchemy session, `transaction.atomic()` on the database Django's router writes to, or `in_transaction()` on the connection Tortoise's router writes to. Awaitable on an async SQLAlchemy session, in an async Django context, and always on Tortoise. Raises `UpsertValidationError` (a `ValueError`) before writing when the payload breaks its projection, `UpsertNotFoundError` (a `LookupError`) when an upsert without `create` or an `id` names no row, and `PermissionError` on any denial. See [Applying an upsert](README.md#applying-an-upsert).

```python
strawberry_orm.is_writing() -> bool
```

`True` while `orm.apply_upsert()` or `orm.apply_ref_list()` runs, in the thread or task running it. Every read the write makes then goes where it writes: Django and Tortoise route them so, and on SQLAlchemy a session's `get_bind()` must, or the write raises `TypeError` before reading anything. See [Writes and read replicas](README.md#writes-and-read-replicas).

### `AbstractRepo`

Subclass per model and pass `repos={Model: MyRepo}` to authorize writes and hook the lifecycle.

| Hook | Signature |
| --- | --- |
| `model` | class attribute naming the model |
| `scope_query` | `(query, info) -> query`; every row a write finds goes through it. Writes do not apply `scope_rows`, so mirror it here. It narrows with conditions: a limit, offset or fetch it adds is refused wherever a write looks a row up, since the lookup takes its own, and `.limit(0)`, `[:0]` or `.none()` hides every row |
| `can_create` | `(data, info) -> bool` |
| `can_update` | `(instance, data, info) -> bool` |
| `can_delete` | `(instance, info) -> bool`; also asked for each row a library delete cascades to |
| `can_link` | `(parent, field, instance, info) -> bool`; also when a single link is pointed at an existing row |
| `can_unlink` | `(parent, field, instance, info) -> bool`; also for the row a re-pointed link replaces, and for the old parent of a row moved into another's list |
| `can_upsert` | `(instance, info) -> bool`, for a row an upsert matched |
| `find_for_upsert` | `(model, query, info) -> instance \| None`; may be `async` on an async session |
| `find_for_upsert_async` | `async (model, query, info) -> instance \| None` |
| `on_match` | `(instance, info) -> None`, once per matched row; may be `async` on an async session |
| `on_before_create` | `(data, info) -> data` |
| `on_after_create` | `(instance, info) -> None` |
| `on_before_update` | `(instance, data, info) -> data` |
| `on_after_update` | `(instance, info) -> None` |
| `on_before_delete` | `(instance, info) -> None` |

A `can_*` hook allows with `True` and denies with `False` or `None`. Any other truthy value still allows but emits a `DeprecationWarning`. On an async SQLAlchemy session and on Tortoise a `can_*` hook may be `async`; a sync write that gets an awaitable raises `TypeError` instead of allowing. An `authorize=` callback on `apply_ref_list` or `apply_upsert` is checked in addition to these hooks, and follows the same rules. Both are asked about every model the row belongs to (the input's model, the row's own model, and their bases), each repo once, and the row must be one each of those models' repos' `scope_query` shows.

The writes create, load, save and delete rows through `_create`, `_get`, `_save` and `_delete`, or their `*_async` variants. Override one of a pair: each `*_async` default calls an override of its sync counterpart, on an async SQLAlchemy session through `AsyncSession.run_sync`, so the override's `super()` calls work there too. A sync default called with an async session outside one raises `TypeError`; on Tortoise the sync defaults always do. The `_save` default saves a row a write read with an `UPDATE` of what the write set on it, every column assigned on it since the read and the columns the ORM stamps on a save, and refuses the write if no row has its key any more; an override that assigns a column and then calls `super()._save(instance, info)` has it saved, and one that saves the row itself has to do the same. Any other instance is saved as the ORM saves it. See [A write saves what it set](README.md#a-write-saves-what-it-set).

`MutationPolicy` is the deprecated predecessor. Pass `policy=` or `repos=`, not both; together they raise `ValueError`.

---

## Query helpers

```python
orm.optimize(data, info, *, at: str | Sequence[str] | None = None) -> Any
```

Eager-load what the current selection needs from rows already in memory. Rarely needed: a resolver's return value is optimized automatically, including rows under a payload's `data`. Accepts a query object, a model instance, or a list (lists nested in it included), and returns anything else untouched. Instances are checked against `scope_rows` first: the result leaves out rows the scope rejects, and is `None` for a single rejected instance. `at` re-roots the selection when the rows sit below the resolved field. Awaitable on async backends. See [Rows a resolver already fetched](README.md#rows-a-resolver-already-fetched).

```python
orm.get_default_queryset(model) -> query
orm.is_query_object(value) -> bool
orm.apply_filters(query, filter_input, model, info=None) -> query
orm.apply_ordering(query, order_input, model, info=None) -> query
orm.apply_ref_list(instance, field, refs, info, *, authorize=None)
```

`get_default_queryset` is cut to `default_query_limit`, when one is set; the backend's `get_default_queryset(model, limited=False)` is every row. `apply_filters` and `apply_ordering` apply a generated input to a query by hand, for resolvers that build their own. `apply_ref_list` writes a related list from `orm.ref` input as one unit of work, the same as `apply_upsert`, honouring the model's repo whether or not `authorize` is passed. `unlink` and `delete` must name rows already in the relation, or `UpsertNotFoundError` is raised. Items carrying an `upsert` arm built by `orm.mutations` are validated and applied like [`apply_upsert`](#mutations).

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

Row-level access control. Applied wherever this model's rows load — as a root query, through a relation, during filter traversal, and when relations are read off rows already in memory. One per model. A limit, offset or fetch it adds is refused; `.limit(0)`, `[:0]` and `.none()` hide every row. See [`scope_rows`](README.md#scope_rows--one-model-one-type). Reads only: `apply_upsert` and `apply_ref_list` find rows through the model's repo, so give a writable model an `AbstractRepo` whose `scope_query` does the same. See [Writes do not apply `scope_rows`](README.md#writes-do-not-apply-scope_rows).

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
| `FieldDefinition` | What `orm.field.eager` returns for a scope or metadata: `using`, `scope`, `compute`, `disable_optimization`, `permission_classes`, `description`, `declared_type`, `metadata`, `filters`, `order`, `deprecation_reason`. |
| `FieldHints` | The optimizer-relevant subset: `using`, `scope`, `compute`, `disable_optimization`. |
| `OperationInfo` | `messages: list[OperationMessage]` |
| `OperationMessage` | `kind`, `field`, `message` |
| `UNSET` | Strawberry's sentinel, re-exported to distinguish "absent" from "null" in partial inputs. |

---

## Full export list

Everything in `strawberry_orm.__all__`:

`AbstractRepo`, `BooleanLookup`, `DateComparisonLookup`, `DateGroupByInterval`, `DateGroupByOption`, `DateTimeComparisonLookup`, `FieldDefinition`, `FieldHints`, `FloatComparisonLookup`, `IDLookup`, `IntComparisonLookup`, `LazyResolutionExtension`, `MutationPolicy`, `OperationInfo`, `OperationMessage`, `OptimizerExtension`, `OptimizerStore`, `Ordering`, `PayloadPolicy`, `ReferenceLookup`, `StrawberryORM`, `StringLookup`, `StringLookupNoRegex`, `TAGS_KEY`, `TimeComparisonLookup`, `UNSET`, `UpsertNotFoundError`, `UpsertValidationError`, `aggregate_field`, `auto`, `filter_field`, `group_field`, `is_writing`, `make_ref_type`, `order_field`, `tags`

From `strawberry_orm.relay`:

`Edge`, `GlobalID`, `Node`, `NodeID`, `ORMConnection`, `ORMListConnection`, `PageInfo`
