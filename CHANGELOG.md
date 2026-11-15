# Changelog

## Unreleased

### Security

- **A ref list could unlink or delete rows outside the parent's relation.** `{unlink: {id}}` and `{delete: {id}}` in a plain `orm.ref` list acted on whatever row the id named, so a caller could detach a tag from someone else's post, or delete another post's comment, through their own post. Both now act only on rows in the parent's relation that the repo can see, and raise `UpsertNotFoundError` for any other id, including one that names no row, as the upsert arm already did. Unlinking from a relation whose foreign key is not nullable is refused before anything is written, rather than failing part-way on Django and Tortoise.
- **`authorize=` replaced the repo instead of adding to it.** Passing an `authorize` callback to `apply_ref_list` skipped the model's repo entirely: its `scope_query`, its `can_*` checks, its `on_*` hooks and its `_create`, `_save` and `_delete`. A callback that only looked at the action allowed every write the repo refuses. The repo now always applies, and `authorize` is checked on top of it.
- **An `async` permission check allowed what it was written to deny.** A `can_*` hook or `authorize` callback written `async` returns a coroutine, which is truthy, so ref lists and upserts treated its `return False` as an allow, even on an async session. Async writes now await it, and a sync write raises `TypeError` instead. A result is read as a bool: `False` and `None` deny, and a truthy value other than `True` still allows but emits a `DeprecationWarning`.
- **An upsert miss wrote `where` fields that `create` does not allow.** A miss seeds the new row from `where`, so with `where: True` a caller could set any offered column on the new row, past the `_meta.create` allowlist (`is_published` on a post whose `create` lists only `title` and `body`). Sending a `where` field other than `id` that `create` does not offer, together with `create`, is now a validation error.
- **Node mutation inputs wrote columns their type excludes.** Inputs from `orm.mutations` ignored `@orm.type(..., exclude=[...])`, so a column hidden from reads stayed writable through `create_node_input`, `update_node_input` and `upsert_node_input`, unlike through `orm.input()`. Generated inputs now leave excluded columns out. Naming one in a `_meta` projection raises `ValueError`, and so does declaring the type after an input that already writes the column.
- **Re-pointing a link skipped `can_link` and `can_unlink`.** A single link pointed at an existing row, and an `update` item that took a row from another parent's list, asked neither whether the row could be linked nor whether it could leave where it was. A caller could move another user's comment onto their own post, or point a post at another author. Now `can_link` is asked for an existing target, `can_unlink` for the row a re-pointed link replaces, and `can_unlink` of the old parent when a row moves between lists.
- **A refused ref list stayed partly written.** A plain `orm.ref` list was written item by item, so a denial on the third item left the first two in place. Every list is now one unit of work, like an upsert: a savepoint on SQLAlchemy, `transaction.atomic()` on Django and `in_transaction()` on Tortoise.
- **A `MutationPolicy` passed with `repos=` never ran.** `policy=` was silently dropped whenever `repos=` was also given, so none of its checks applied to any model. Passing both now raises `ValueError`.
- **A ref-list delete bypassed the repo's delete on SQLAlchemy and Tortoise.** `{delete: {id}}` deleted the row directly, so a soft-delete `_delete` override was skipped and the row was gone for good. It goes through the repo now, as it did on Django.
- **A connection could be made to read a whole table.** `first` and `last` were capped at `max_results`, but `before` alone paged up to its cursor however far that was, `last: 0` returned every row after the cursor, and a forged cursor before the first row (`after: "arrayconnection:-5000"`) read every row with no `LIMIT` to slice them in Python. `before` alone now reads one page from the start, as no arguments do. `last: 0` returns an empty page at the end of the result. A cursor before the first row is refused with `Argument 'after' cannot point before the first item.`
- **A group's `items(last: 0)` returned the rows read for the group.** The `last: 0` fix above did not reach a connection's `groups { items }`, so each group answered with its first page. It is now an empty page at the end, as on the connection, read from at most one row per group. A `last` written in the query was also ignored when each group's rows were read, while one passed as a variable was not; both now read the same rows.
- **`groupBy` answered with any number of groups.** Grouping by a column with many values, such as `id`, built a group per value even for `first: 1`, and an `objects:` key held every related row of its group. A connection now refuses more than `max_groups` groups (default 100), and an `objects:` key holding more than `max_group_key_rows` related rows (default 100). Either one is an error, not a truncated answer.
- **An `objects:` group key was built whole before `max_group_key_rows` refused it.** The database put every related row of each row into its key, 3,000 tags' worth for a post with 3,000 tags, and only then was the key counted. Each key now reads at most one row more than `max_group_key_rows`, enough to tell that it is too big. Ordering by `objects:` still reads every related row, as the order of the whole set depends on them.
- **SQLAlchemy: a row keyed by more than one column was matched by the first.** `groupBy`, a filter through a scoped relation and a custom filter under `not:`, `any:`, `oneOf:` or `object:` match rows by key against the rows they keep, and compared only the first key column. A row the type's `scope_rows` hid was counted in a group, and matched a filter, when it shared that column with a row it showed, and grouping across a to-many relation counted each row under the related values of every row sharing it. The whole key is compared.
- **`order` and `groupBy` took any number of entries.** 5,000 entries made 5,000 `ORDER BY` terms. A list longer than `max_order_entries` or `max_group_by_entries` (default 10 each) is refused before any SQL is built.
- **On SQLAlchemy, a page ordered through a to-many relation read every joined row.** `order: [{object: {tags: …}}]` repeats a row once per related row, so the page was read whole and sliced in Python: `first: 1` read 12,000 rows. The page is now cut in SQL, each row counted once at its first appearance, as `.unique()` reads it. A join the resolver already cut with a `LIMIT` or `OFFSET` is still read whole, since that limit counts joined rows.
- **On SQLAlchemy, a database error reached the client with its SQL.** Only `OperationalError` and `ProgrammingError` on reads were replaced. A value the column type refused, such as a malformed UUID in `filter: {field: {id: {exact: "not-a-uuid"}}}`, returned the statement for edges, `totalCount`, aggregates and groups: the table name, columns the API leaves out and the `scope_rows` predicate, and on PostgreSQL the bound parameters. Every statement the library runs, including eager loads, counts and relation counts, sync, async and offloaded, now reports `Invalid filter expression`, or `Database connection failed` when the connection is lost. The original is kept as the exception's cause, for the server log.
- **The MCP `graphql` tool answered `__schema` and `__type`.** `root_fields` hid fields, types and mutations from `graphql_schema`, but an operation asking for `__schema` or `__type` was answered from the whole schema and listed them all. Such an operation is now refused, with or without `root_fields`; `graphql_schema` describes what the tool may use. `__typename` still works.
- **The SQLAlchemy filter helpers defaulted to allowing regex.** `_build_lookup_clauses`, `_build_sa_field_clause`, `_build_sa_filter` and their callers took `enable_regex=True` unless told otherwise. Every caller passed the configured value, so nothing reached them, but a new caller would have enabled regex without `enable_regex_filters`. They default to `False` now.
- **The release workflow ran actions by tag and put the tag name in its script.** Every action in the workflows is pinned to a commit SHA, and `github.ref_name` reaches the release script through `env:` rather than being written into it.
- **`not:` over a to-many relation tested hidden related rows.** On Django, `posts(filter: {not: {object: {comments: {field: {body: {startsWith: "the password is h"}}}}}})` dropped a post when a comment the caller cannot read matched, so hidden values could be read a character at a time. On Tortoise, a hidden related row was enough to drop a parent, and a parent with no related rows was dropped too. Through a to-many relation, `object:` is now one condition per parent, "a readable related row matches" (an `EXISTS` on Django, one subquery of the matching parents on Tortoise, as SQLAlchemy already did), and `not:` negates it whole.
- **A `@filter_field` through `object:` ran on the parent's query, over every row.** `posts(filter: {object: {author: {search: "bob@secret"}}})` ran `search` on the posts query, uncorrelated and unscoped, so all posts or none came back: a yes/no question about users the caller cannot read. Under `not:`, a custom filter was applied as if `not:` were absent, and in `any:` it narrowed the whole query instead of adding an alternative. Anywhere but the top of the filter (alone or in a top-level `all:`), a custom filter is now called on a fresh query over its own model, its rows are matched by primary key, and that condition is negated, combined or tested on the related row as its place says.
- **Root filters and `groupBy` used the edge `scope=` of the model's first-registered type.** With an admin type and a public type over one model, the public type's `scope=` on a relation was skipped when the admin type was registered first, so grouping or filtering through that relation read the rows it hides. The `scope=` now comes from the type the field returns. Where that cannot be told, as past an `object:` hop, every type over the model contributes its `scope=`, and only rows all of them allow are reached.
- **Filter limits held for each list, not for the filter.** A variable can be referenced any number of times, so a 10 KB request repeating one 500-value `inList` a couple of thousand times built a statement with a million bind parameters, every list within its limit. `max_filter_branches` and `max_in_list_size` now count every entry and every value across one filter argument, each reference to a variable included, and a filter over either is refused before any SQL is built.
- **A connection over a relation could sort by a scoped type's columns.** `comments(first: 10, order: [{object: {author: {field: {name: ASC}}}}])` was refused on a root connection but, on a relation connection, ranked comments by authors the caller cannot read. Connection ordering now runs the same check as every other order, and the check `orm.schema()` runs at build time covers the order of every type over a model, not only the last one registered.
- **The foreign-key shortcut compared any integer column with the foreign key.** `object: {author: {field: {age: {exact: 2}}}}` matched the posts whose `author_id` is 2, revealing foreign-key values and returning the wrong rows. The shortcut is taken only for the related model's primary key through a forward foreign key now, and any other column is compared on the related row.
- **Tortoise errors listed every column of the model.** A filter by key through a reverse relation (`object: {posts: {field: {id: {exact: 1}}}}`) was built as a `posts_id` column that does not exist, and Tortoise's error, which lists the model's columns, hidden ones included, reached the client. Reverse and many-to-many relations are filtered on the related rows now, and a filter Tortoise cannot resolve is refused with "Invalid filter expression".
- **`max_filter_depth` did not count levels under the foreign-key shortcut.** `object: {author: {not: {not: …}}}` was accepted as deep as graphql-core allows. Those levels count now.
- **Reference lookups read numeric-looking IDs as integers.** `{exact: "00123"}` on a text key was compared as `123`, so it missed its row and matched another or nothing, and raised on PostgreSQL. An ID is matched as the key column holds it now: as given on Django and Tortoise, and on SQLAlchemy converted only for an integer or UUID column, where an ID that is not one is refused.
- **Selecting a relation of rows handed to `orm.optimize()` skipped their scope.** `orm.payload.query` and `orm.payload.mutation` resolvers returning instances, and a manual `orm.optimize(rows, info)`, loaded the selected relations onto them and marked them as prepared, which the row scope check trusted. `{ post(id: 2) { data { title author { name } } } }` returned a draft its type hides, though without `author` in the selection `data` was `null`. Instances are now checked against `scope_rows` before anything is loaded onto them, the rows it rejects are dropped, and only rows a scope check kept are marked.
- **Rows a resolver returned in anything but a list skipped `scope_rows`.** graphql-core reads any iterable for a list field, but only a single row or a `list` was checked, so a tuple, a generator, an async iterable or a SQLAlchemy `ScalarResult` (`session.scalars(stmt)` without `.all()`) returned every row the resolver read, with the optimizer on or off. Such a result is now read into a list and checked as a list is. Strings and mappings are left to graphql-core, which does not read them as lists.
- **A connection over the rows a resolver read counted the hidden ones.** When a connection's resolver returned rows rather than a query, `totalCount`, the cursors and the page included rows `scope_rows` hides, whose `node` then failed as a non-null error. The rows are now checked before they are counted or sliced.
- **Rows of a subclass skipped the scope of their base model's type (SQLAlchemy, Django).** The type over a model accepts rows of an inherited SQLAlchemy mapping, a Django proxy or a multi-table child, but the scope was looked up by the row's or query's own class, which had none, so `{ document(id: 4) { title } }` returned a `Contract` the `Document` type hides. The scope is now found through the classes a model inherits: a subclass's rows and queries are scoped by its base's type, unless a type over the subclass defines its own `scope_rows`.
- **On Django, a proxy or multi-table child row's relations skipped their `scope=`.** A relation's `scope=` was looked up by the type over the row's own class, which had none, so `posts` on `FeaturedUser` rows returned as `UserType` came back without the `scope=` that `UserType.posts` puts on it. A subclass row is now read by its base's types, and their `scope=` applies; when the type cannot be told, the `scope=` of every type over the row's class and the classes it inherits applies.
- **SQLAlchemy: a relation loaded with a scope that joins kept only its WHERE clause.** A nested relation, read for the selection or through `using=`, was loaded with the WHERE clause of the related type's `scope_rows` and its `scope=`, and the rest of the statement was dropped. A scope that joins, such as `select.join(Membership).where(Membership.user_id == me)`, then let through every row of a visible parent: `{ orgs { projects { name } } }` listed projects the root `projects` field hides. To-one and many-to-many relations had the same gap. A scope that is more than a `.where()` on the related table's own columns is now run whole, as a statement of its own per relation, keyed by the parents' join column.
- **SQLAlchemy: a scope's `with_loader_criteria` was lost wherever the scope ran inside another statement.** SQLAlchemy applies those criteria from the outermost statement only, and counts, aggregates, groups and their items, orders and groups by a relation's rows (`objects:`), filters through relations, and the check on rows a resolver returned all run the scope as a subquery. A filter through `author` matched users the `User` type hides, `totalCount` and aggregates counted hidden rows, and an `objects:` key listed hidden tags. The statement a scope runs inside now carries its `with_loader_criteria`.
- **A relation loaded for one field's `scope=` was trusted by another.** The mark saying a relation's cache was loaded with its scope did not say which `scope=`. With two types over one model, one reading `posts` unrestricted (through `using=`, say) and one through `orm.field.eager(scope=published)`, the second read the cache the first loaded on the same row: `{ stats { draftCount } user(id: 1) { posts { body } } }` returned a draft's body. The mark now records the `scope=` the relation was loaded for, and a field with another one reads the relation again. On SQLAlchemy, a relation one statement loads in two places under different scopes is not marked at all. All backends.

### Added

- **`max_groups`** (default 100) and **`max_group_key_rows`** (default 100) on every backend: the groups one connection may answer with, and the related rows one `objects:` key may hold.
- **`max_order_entries`** and **`max_group_by_entries`** (default 10 each) on every backend: the longest `order` and `groupBy` lists accepted.
- **`get_default_queryset(model, limited=False)`** on a backend reads every row of the model, without `default_query_limit`. `orm.get_default_queryset(Model)` still applies it.

### Changed

- **Projections no longer imply destructive operations.** A to-many relation in a `project=` without `_meta` used to offer `unlink` and `delete`, and a single link offered `onReplace: DELETE`, which deletes the row the link replaces. A relation now offers `create` and `update` until its `_meta` sets `unlink: True` or `delete: True`, and a re-pointed link only disconnects the row it replaces unless `onReplace` lists `"DELETE"`. A link with a single `onReplace` option is a `@oneOf` input with no `onReplace` field. This changes the generated schema for projections that relied on the defaults.
- **Plain `orm.ref` items go through the same writer as upserts on every backend.** An item naming more than one of `create`, `update`, `unlink` and `delete` raises `UpsertValidationError` before anything is written, where the first one used to be applied. A `delete` from a many-to-many list removes the row from the relation before deleting it, so a row a soft-delete repo keeps also leaves the list.
- **`apply_ref_list` validates every list**, not only lists with generated items, so a hand-built list is checked in full before its first write.
- **Paging diverges from Strawberry's `ListConnection` in three requests** that it answers by slicing a Python list: `before` without `first` or `last` reads one page (`max_results`, or `default_query_limit` when that is smaller) from the start, not every row up to the cursor. `last: 0` returns no edges, with `hasPreviousPage` true when rows follow the cursor. A cursor before the first row is an error, where Strawberry counted it from the end of the list.
- **`default_query_limit` caps each read, not the rows a connection pages through.** It cut the query a generated connection was built on, so `totalCount` stopped at the limit and no page went past it. Now a connection's page is at most `default_query_limit` rows, `first` and `last` included, and its default when neither is given, and every page is reachable. A generated root list (`users: list[UserType] = orm.field.eager()`) is cut to the limit after its filter, order and `scope_rows`. On Django it was cut first, so a `filter` or `order` on it failed with "Cannot filter a query once a slice has been taken." A nested relation list is not limited; cap the root it hangs from, or make it a connection.
- **SQLAlchemy: a join that repeats rows is paged in the database.** See the security note above. `README.md#connection-fields` lists what is still read whole.
- **Filter limits are totals.** `max_filter_branches` and `max_in_list_size` count across the whole filter argument, and `max_in_list_size` counts the two ends of a `range` too. A filter that ran with several lists each under the limit may now be refused. A custom filter's value is not counted; a `@filter_field` that takes a list should cap it.
- **A custom filter under `not:`, `any:`, `oneOf:` or `object:` gets a fresh query over its filter's model**, not the field's query, and its rows are matched by primary key. A `@filter_field` that relied on what the field's query was already narrowed or joined by sees only its own model there.
- **Each `object:` through a to-many relation is its own condition.** On Django and Tortoise, `all: [{object: {tags: A}}, {object: {tags: B}}]` matches a post with one tag matching `A` and another matching `B`, as on SQLAlchemy, rather than needing one tag to match both.
- **`orm.schema()` checks the order input of every type over a model**, so a schema with an earlier-registered type whose order reaches a scoped type now fails to build, as one with that type registered last already did.
- **`build_reference_object_filter_clause` and `filter_tree_uses_only_reference_lookups` take `key=`**, the related field the foreign key holds, and the first takes `max_depth=` and `depth=`.
- **`orm.optimize()` returns what its scope check kept.** It returned what it was given, with relations loaded. A list comes back as a copy without the rows `scope_rows` rejects, and a rejected single instance as `None`, so use the return value: `rows = orm.optimize(rows, info)`.
- **SQLAlchemy: a relation whose scope is more than a `.where()` on the related table's own columns** is read in a statement of its own, keyed by the parents' join column, rather than by a `selectinload`. Through an association table, one statement more reads the links. Such a scope on a relation that does not join on one column (a composite key, or a `primaryjoin` that says more than the key comparison) now raises a `ValueError` naming the relation, where it loaded rows the scope hides. Write that scope as a `.where()` on the related table's columns, or read the relation in a resolver of your own.
- **SQLAlchemy: an ordered scope on a many-to-many relation keeps its order**, through an association table on one key column, for it is read the same way. This lifts the 0.29.0 limit for those relations.
- **SQLAlchemy: a scope's `with_loader_criteria` applies to the whole statement the scope runs inside**, as it does to the statement the scope returns: every place its entity is read there. A relation's `scope=` that uses it on the model the outer statement reads, as on a relation from a model to itself, narrows the outer rows too.
- **A relation connection without `first` is read in one query for every parent.** `{ users { posts { … } } }` sent one query per user; it is windowed at the page the connection returns, `max_results` or `default_query_limit` when that is smaller, as `first` was. `first` above `default_query_limit` no longer reads past it either.

### Fixed

- **A relation connection's `aggregates`, `groups` and `pageInfo { aggregates }` were lost when its page was windowed.** `posts(first: 10) { aggregates { count } }` answered `null` for every parent, and `pageInfo { aggregates }` raised. A connection that selects them is read one parent at a time, from the relation's own query.

## 0.30.0

### Security

Each of these let a request read rows its type's `scope_rows` hides, in 0.29.0.

- **A resolver named after a relation was trusted as the library's read of it.** A field named like a relation of its model, such as an app's own `@orm.field.lazy def posts(...)`, skipped the related type's `scope_rows`, so it returned every row its resolver found. Only the library's own relation resolvers and `on=` views are trusted now, whatever they are named. That also stops a view named in several words (`recent_posts`) being checked again a parent at a time.
- **A query `scope_rows` handed back unchanged was marked as scoped.** An admin's request, whose scope returns the query as it is, marked a query kept between requests (a module-level `select()` or queryset) as already scoped, and every later request read all its rows. Only a query the scope narrowed is marked now.
- **What one request learned about scoping was trusted by the next.** Rows, relation caches and queries stayed marked as scoped, so rows that outlive a request (cached by the app, or in a SQLAlchemy session shared by several requests) skipped `scope_rows` from then on. After an admin's request, the next user read the admin's rows and relations. A mark now holds only for the execution that made it, and each subscription event is an execution of its own.
- **Rows `scope_rows` rejected were removed from the resolver's own list.** The list a resolver returned was filtered in place, so a list the app keeps lost those rows for every later request, including those allowed to see them. They are dropped from a copy now.
- **Connection state was kept on `info.context`.** Groups and aggregates read the connection's query from there, by the field's path, so a server that shares one context object between requests could hand one request's query, and its scope, to another's. Wherever the optimizer extension runs, as under `orm.schema()`, the state is kept for the execution instead.

### Added

- **Ordering by the set of a to-many relation's rows.** `order: [{objects: {tags: {field: {name: ASC}}}}]` sorts each row by all of its visible related rows together, by the same value `objects:` grouping keys a group by. Rows with the same set sit together, so grouped by the set too, each group's edges are one run, its `items` follow the order, and groups are ranked by their least (or greatest) set. Naming the relation in several entries makes one set of all their columns. The entries must agree on the direction, and the nulls variants sort as plain `ASC`/`DESC`, since a row with no related rows has `[]`. The set is compared as JSON text: on SQLite that is byte order, which puts `[graphql, python]` before `[graphql]` and `[]` last. On PostgreSQL it follows the database's collation. PostgreSQL and SQLite only; other databases raise. The generated type is `<Model>OrderObjects`.

### Changed

Faster on every backend. `benchmarks/kitchen_sink` runs 12 workloads (nested connections, `on=` views, filters across several relations, groups and aggregates) over 500 users, 5,000 posts and 25,000 comments on SQLite. All of them are faster on all five backend variants, with identical results.

- **The library's schema extensions skip plain column fields.** Under `orm.schema()`, a scalar or enum field that reads a column straight off a row, Relay's default `id` when it reads a column, and the plain fields of the library's own connection, edge and page-info types are no longer wrapped by the optimizer, batching, connection, lazy-resolution and row-scope `resolve` hooks, none of which act on them. Any other field keeps them: a property can load a relation, which lazy resolution is there to report. An app's own `resolve` extensions still run on every field, as does a subclass of a library extension that overrides `resolve`. A schema built with `strawberry.Schema` directly is unchanged.
- **Django under async execution reads what is loaded on the event loop.** A loaded column, a relation the library prefetched and scoped, a loaded foreign key and Relay's default `id` are read in place instead of through `sync_to_async`, a thread hop per field. A deferred column, a relation given a `filter` or `order`, and a prefetched relation the library did not scope itself, when its type has `scope_rows`, still go to the worker thread. A resolver is no longer wrapped in `sync_to_async` twice. Async Django was about 6.5× faster across the workloads.
- **On Django, a plain column has no generated resolver**, so Strawberry reads it as an attribute rather than building an `Info` per value. A column whose descriptor changes the value it stores (a `FileField`, for one) keeps its resolver, and a row whose class (a proxy or subclass) puts a property or another descriptor over the column is read through it.
- **SQLAlchemy: a filter through a scoped relation uses the scope's WHERE clause** when the scope only filters the related table's own columns. It was embedded as `pk IN (SELECT ...)` inside a correlated `EXISTS`, which SQLite runs again for every outer row. A scope with joins, limits, grouping, nested selects, raw SQL, loader options or `with_loader_criteria`, one that selects another entity, one over a class that shares its parent's table, or one over a relation back to the same table, is still embedded as a subquery. Its `with_loader_criteria` did not apply there; see the Unreleased security notes.
- **SQLAlchemy: counts, aggregates and group counts drop the statement's `ORDER BY`**, unless it has a limit or offset, or a PostgreSQL `DISTINCT ON`, that the order decides.
- **Tortoise: a filterable relation reads the rows the optimizer prefetched**, rather than querying again per parent, when it is given no `filter` or `order`. A cache the library did not scope is still queried when the related type has `scope_rows`.
- **A group's `items` are not scope-checked again per group.** They are read from the connection's already scoped query.

### Fixed

- **Tortoise: `orm.order()` and `orm.group()` before `Tortoise.init`** no longer fail on a reverse or many-to-many relation known only by its annotation (`posts: fields.ManyToManyRelation["Post"]`). Before init, the annotation tells whether the relation is to-many.
- **`auto` honours `null=True` on Django and Tortoise.** A nullable column was typed non-null, so a row holding NULL failed with "Cannot return null for non-nullable field". It is typed nullable now, as on SQLAlchemy. This changes the generated schema for such columns.
- **Groups and aggregates work without a `context_value`.** A connection executed with no context returned `null` for both. Under `orm.schema()`, their state is now kept for the execution instead.
- **`orm.connection.lazy(resolver=fn)` on an `@orm.type`** was removed by the decorator, leaving a field with no `first`, `filter` or `order` arguments. The keyword form now builds the same field as the decorator form.
- **A generated connection applies its `filter` and `order` once**, not twice.

## 0.29.0

This release closes the `on=` view brief and, of the thirteen SQLAlchemy backend items, **1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12 and 13**. Items 5 and 9 have limits, given under each.

### Added

- **`to_query=` (SQLAlchemy item 8).** `StrawberryORM.for_*(to_query=lambda result, info: ...)` maps what an `orm.field`, `orm.connection`, `orm.payload.query` or `orm.payload.connection` resolver returns before the library decides between a query and rows. An app's own query objects are converted once, and a connection over one filters, orders and pages in the database. Backends also have `to_query(result, info)` to override.
- **`offload_sync_session=True` on SQLAlchemy (item 9).** Under async execution with a sync `Session`, the library's own reads (counts, a statement a resolver returned, eager loads, relation loads, aggregates and groups) run through `sync_to_async(thread_sensitive=True)`, one thread per operation, instead of on the event loop. It is opt-in: in-memory SQLite gives each thread a database of its own. **Limit:** writes (upserts, `orm.ref` lists, repo creates) still run where they are called.
- **`field_order=` on SQLAlchemy (item 10).** `field_order(model, names)` returns the order a model's fields are listed in, for the generated types and inputs. Names it leaves out follow the ones it returns.
- **Grouping by the set of a to-many relation's rows.** `groupBy: [{objects: {tags: {field: {name: true}}}}]` puts each row in exactly one group, keyed by all of its visible related rows together: a post tagged `graphql` and `python` is in the `[graphql, python]` group, not counted under each tag. The key reads `key { objects { tags { name } } }`, a list of the related keys, distinct and sorted, and `[]` for a row with none. It works across many-to-many relations and reverse foreign keys, takes several columns and date intervals, and combines with other group-by entries. Groups, their `items` and `edgeIndices` all work as for any other key. Built in SQL as JSON on PostgreSQL and SQLite; other databases raise. The generated types are `<Model>GroupByObjects` and `<Model>GroupKeyObjects`, and `AggregateMeta` has an `objects_key_type` field.

### Changed

- **A client's `order` replaces the order of the statement a resolver returned (SQLAlchemy item 4)**, as on Django and Tortoise, instead of only breaking its ties. With no `order`, the statement's own order stands; the primary-key tiebreak for paging still applies.
- **Dependencies.** Developed and tested on the latest releases (Django 6.1, SQLAlchemy 2.1, Tortoise ORM 1.1, Strawberry 0.341, pytest 9, uv_build 0.13). The minimum versions you can install with are unchanged.

### Fixed

- **An `on=` view's rows are trusted as a read of its relation** (the `on=` view brief). A view named differently from its relation (`recent = orm.field.eager(on="books", scope=...)`) had its prefetched, already scoped rows scope-checked and loaded again, a parent at a time. It is now decided by the relation the field reads. A field whose resolver returns rows it read itself is still checked. On Django, a nested selection under such a view is prefetched with it, rather than per row.
- **A batched field under a connection is batched (SQLAlchemy item 1).** A connection's edges now record their nodes as the parents of what is read under `node`, so an `on=` view or other batchable field under a connection is one statement for the page instead of one per node.
- **The batcher's rows are not scoped and loaded again per parent (item 2).** They are marked as read with the scope and the nested selection already applied. Under async execution, one batch is shared by every parent rather than awaited once per parent.
- **Rows a resolver hands back are loaded in their own session, and only what they lack (item 3).** SQLAlchemy's `load_relations` re-reads rows in the session they belong to, so the caller's instances get the relations, and skips relations every row already has loaded, descending into loaded ones. Rows that already hold the selection cost no statement. A detached row is filled from a copy read in the request's session. Values changed in memory survive, and so does a collection the app already loaded: it is not replaced, and the field scopes it as before. Rows from several sessions are read once per session.
- **A scope's `ORDER BY` is kept when its to-many relation is eager-loaded (item 5).** An ordered `scope=` or `scope_rows` on a to-many relation is read as one statement for all parents with the scope's order, as Django's `Prefetch` is, instead of a `selectinload` that dropped it. Parent keys are sent 500 to a statement. **Limit:** a relation through an association table (many-to-many), on a composite key, or whose `primaryjoin` says more than the key comparison still uses `selectinload`, and the scope's order is lost.
- **A scope check reads only keys (item 6).** `filter_scoped_pks` selects the primary key column, so loader options a `scope_rows` attaches are not run for a membership check.
- **`SmallInteger` and `BigInteger` columns are `Int` (item 7)**, not `String`, and get `sum`/`avg` aggregates. A column type is now looked up by its class and each class it derives from, so other subclasses of a known type change too: `Double`, `DOUBLE`, `DOUBLE_PRECISION` and `REAL` become `Float`, MySQL's `TINYINT` and `MEDIUMINT` become `Int`, and `MatchType` becomes `Boolean`. This changes the generated schema for models with such columns.
- **Date and datetime filters compare as dates (item 11).** Every value a lookup compares (`exact`, `neq`, `gt`, `gte`, `lt`, `lte`, `inList`, `notInList`, `range`) is parsed and bound in the column's own type, so PostgreSQL with psycopg 3 no longer refuses `timestamp >= varchar`. A value that is not ISO 8601 (`"2026-09"`) is now refused with an error naming it, rather than handed to the database. Tested on SQLite and by the bound types; the PostgreSQL behaviour follows from the binding.
- **`object: {books: {isNull: true}}` works on a to-many relation (item 12)**, through a foreign key or an association table: rows with no related row the reader can see. `false` gives rows with at least one.
- **A sync session no longer needs greenlet (item 13).** The SQLAlchemy backend looks for `AsyncSession` only once `sqlalchemy.ext.asyncio` has been imported.
- **On Django, a date grouped by an interval through a related type with `scope_rows`** read as a datetime (`"2026-01-01T00:00:00+00:00"`), and a datetime's period did not read in the current time zone. The period is now cut outside the scope's `CASE`, so it reads as it does on the model itself.

## 0.28.0

### Added

- **`median` aggregate.** `aggregates { median { amount } }` is `percentile_cont(0.5) WITHIN GROUP (ORDER BY …)`, exposed as `<Model>MedianAggregates` for the same numeric fields as `sum` and `avg`, over the result set and per group. It is computed on PostgreSQL only; other databases raise, as date intervals do. The page-level `median` in `pageInfo` is worked out in memory and works anywhere.

### Changed

- **`min` and `max` of a date, datetime or time field are typed as the field**, not `Float`: a date reads `"2026-03-01"` and a datetime an ISO 8601 instant. Numeric fields are still `Float`.
- **Groups are not ranked by a JSON, binary, hstore, full-text search vector or geometry column**, which PostgreSQL has no `MIN` of, on any database. Groups ranked only by such a column come in key order.
- `AggregateMeta` has a `median_type` field (last, after `relation_keys`), and `requested_aggregates(info, path, meta)` takes the meta to map names to model fields.

### Fixed

- **`__typename` in an aggregate selection broke the request** with "Cannot resolve keyword ''". Apollo adds it to every selection. Names that are not columns are now ignored, and fragments inside `aggregates` are read.
- **Aggregates of a two-word field failed** (`sum { amountTax }`, `min { createdAt }`) with "Cannot resolve keyword 'amountTax'", over the result set and per group. Each selected name is mapped to its model field, as filters and orders are.
- **Ranking groups failed on PostgreSQL** with `function min(uuid) does not exist` or `function min(boolean) does not exist` when the connection was ordered by a UUID or boolean column, such as a related row's primary key. A UUID now ranks by its text and a boolean as an integer, which sort as the column does. A SQLAlchemy `TypeDecorator` is judged by what it stores on the configured dialect.
- **An aggregate selected twice read only one selection's columns**, as with `sum { amount }` beside a fragment's `sum { tax }`, or two aliases of `max`. Both are read now. Fragments around `aggregates` (on the connection or a group) are read too.
- `sum { __typename }` with no column returns an empty `sum` object rather than `null`.

### Documentation

- A date field grouped by an interval is keyed by a date (`"2026-03-01"`), not a datetime.
- A row with no related row is keyed `{author: {id: null, name: null}}`, not `{author: null}`.

## 0.27.0

### Added

- **Group by related rows.** `groupBy: [{object: {author: {field: {name: true}}}}]` groups by any column of a related row, through a foreign key, a reverse foreign key or a many-to-many relation. The key nests one level per relation (`key { author { id name } }`). A relation is offered when its model has a group-by type and the type shows the relation as stored; `@orm.group_type` classes name relations with `auto`. The related row is read through its type's `scope_rows`, so a hidden one groups as `null`. Across a to-many relation a row is grouped once under each distinct key its visible related rows give, and once under `null` if it has none.
- **`@group_field` works.** Its value is grouped by in the database on all three backends, and is reported as a string under the method's name in the key. It may return an expression, a field name, an `(expression, label)` tuple or, on Tortoise, a pypika term.

### Changed

- **Groups follow the connection's order.** Each group is ranked by its first row under `order` (or the query's own ordering), then by its key, so a group's edges sit together. `items` within a group follow the same order.
- **`DateGroupByOption.interval` is honoured on every backend.** Django truncates in the current time zone. SQLAlchemy and Tortoise truncate on PostgreSQL and SQLite and raise on other databases. `batch_group_items` partitions by the same expression.
  - **Breaking:** an interval key is now the ISO 8601 start of the period (`"2026-03-01T00:00:00"`, with its offset on Django under `USE_TZ`). SQLAlchemy used to key by `strftime` text such as `"2026-03"` (SQLite only), and Django and Tortoise grouped by the raw value.
- **Breaking for custom backends:** `apply_grouping` and `batch_group_items` take a list of `strawberry_orm.grouping.GroupPath` instead of the group-by input or key field names, and backends implement `relation_hop` and `group_memberships`. `scope_query_to_group`, `AggregateMeta.group_key_fields` and `AggregateMeta.build_group_key` are removed.

### Fixed

- **Grouping without `order` gave one group per row** on Django, because the query's default ordering leaked into `GROUP BY`. It no longer does on any backend.
- **A `null` group's `edgeIndices` claimed every edge.** A `null` key now matches only rows whose value is null. Related, interval and `@group_field` keys are matched by asking the database for the page's rows.
- **A filter through a to-many relation repeated rows.** On Django and Tortoise, `filter: {object: {posts: ...}}` listed and counted a parent once per matching related row. The match is now a subquery, so each row appears once in edges, `totalCount`, aggregates and groups.
- **Async Django connections returned `null` aggregates and groups** when the resolver ran through `async_safe_resolver`. The query was stashed for aggregation before the worker thread had returned it.
- **A nested connection's aggregates and groups ignored its parent on Tortoise.** Every parent's connection shared one slot on the context, and concurrent async resolvers overwrote each other's query. The state is now kept per field path.

### Known issues

- On Tortoise, a `NULLS_FIRST`/`NULLS_LAST` order is not applied to a connection's edges, as before. Groups and their `items` do follow it, so `edgeIndices` may not be contiguous under such an order.

### Changed

- **Breaking: `contrib.mcp` serves your operations as tools, with no model on the server.** `mcp_server(schema, operations=DOCUMENT)` turns each named operation in a GraphQL document into one MCP tool. The operation's name and description become the tool's, its variables become typed arguments (a JSON Schema with your input types, enums, defaults and descriptions), and its selection set becomes the tool's output schema. Queries are marked read-only and mutations destructive. The document is validated when the server is built. `QueryAgent`, the `ask` tool, clarifications, confirmations, lookups and the hooks are removed, and the extra no longer installs any-llm.
- `sync=True` runs `schema.execute_sync` in a worker thread, `sync=False` always awaits `schema.execute`, and `execute=` is an async `(query, variables, context, allowed) -> ExecutionResult` of your own. By default a query that Django refuses on the event loop is rerun with `execute_sync`, with a warning; a mutation fails asking for `sync=True` rather than run twice.

### Added

- **`graphql_tool=`** adds a `graphql` tool that runs one operation the client writes, and a `graphql_schema` tool that returns the schema it is written against. `GraphQLTool(allow_mutations=, root_fields=, max_depth=, descriptions=)` configures them; only queries run by default.
- A weekly CI run against the newest strawberry-graphql release.

### Fixed

- Sync execution closes Django's stale connections before and after each operation, as Django's request handler does. Executor threads used to keep their connection past `CONN_MAX_AGE` and never noticed one the database had dropped.

## 0.25.1

### Changed

- Requires **strawberry-graphql ≥ 0.341.2**. Connection pages still follow Strawberry's `SliceMetadata`: `first` with `before` takes the first *n* rows that precede the cursor (from the start, or after `after`), not the *n* rows immediately before it.

### Fixed

- **Columns declared on an interface count as shown.** `_orm_reads_as_is` only looked at the type's own `__annotations__`, so a field inherited from a `strawberry.interface` never opened its column to the type's filters, orders, groups or aggregates. An order whose only field was inherited failed with `InputCutError`; every other input quietly dropped those columns. Inherited fields that read as stored are now counted the same way as own annotations. A type's `exclude=` still cuts them from the inputs.
- **`ORMListConnection` is a decorated Strawberry type.** strawberry-graphql 0.334+ refuses undecorated subclasses of Strawberry types; the README's `posts: ORMListConnection[PostType] = orm.connection.eager()` pattern then failed schema build. `ORMListConnection` is now `@strawberry.type`, like `ORMConnection`.
  - **Breaking:** this renames the published type. Strawberry used to publish the undecorated subclass under its parent's name, so `ORMListConnection[PostType]` appeared as `PostTypeORMConnection`; it is now `PostTypeORMListConnection`. Every operation still validates, but anything that names the type breaks without an error: `__typename` checks, fragments `on PostTypeORMConnection`, persisted queries, client cache `possibleTypes`/`typePolicies`, and test mocks. Search clients for `ORMConnection` and rename it to `ORMListConnection`.
- **`QueryAgent` rejects a non-object `variables` value.** `variables: []` was treated as `{}` because empty lists are falsy, so a malformed tool call could run. It is now refused like any other non-object.
- **`QueryAgent` runs sync schemas.** If `await schema.execute` raises Django's `SynchronousOnlyOperation`, or Strawberry returns it as a GraphQL error's `original_error`, the agent switches to `schema.execute_sync` in a worker thread, logs a warning, and keeps that path for later calls on the same agent.

## 0.25.0

### Added

- **`strawberry_orm.contrib`**, a home for optional plugins, each installed through its own extra.
- **`strawberry_orm.contrib.mcp`** (`pip install "strawberry-orm[contrib.mcp]"`): `mcp_server(QueryAgent(schema, model="anthropic:…"))` serves a schema as an MCP server with one `ask(question)` tool. A model writes a single GraphQL query for the question, and the query runs through the schema with the caller's context, so `scope_rows` and permissions apply. Mutations are rejected unless `allow_mutations=True`; queries are validated and depth-limited before they run. Models are called through any-llm, so the provider is a string. The rows of the answer never reach the model.
  - Clarifying questions: the model can ask for missing details through `ask_user`, shown as an elicitation form. 2026-07-28 clients get `input_required` with the agent's progress in sealed request state, older clients a form during the call, and clients without forms a `needs_input` result. `clarify=` and `max_clarifications=`.
  - Allowed mutations are confirmed by the person asking first (`confirm_mutations=True`). A paused confirmation runs its change once, within `confirm_within` seconds (default 600), and the change is checked again when it resumes.
  - Opt-in lookups (`lookups=True`): small read-only queries whose rows, capped by `max_lookup_rows`, the model sees. Turning them on with mutations allowed and `confirm_mutations=False` warns, since an instruction hidden in a row another user wrote could then steer the model into an unconfirmed change.
  - Hooks: `on_clarify(clarification, context)` answers or refuses a clarification; `before_query(query, variables, context)` rejects an operation with `QueryRejected` or stops with `QueryAgentError`.
  - `QueryAgent.ask(..., clarifier=)` and `QueryAgent.resume(state, answers)` for use without MCP.

### Changed

- **Filters, ordering, group-by and aggregates cover what the type shows.** They were generated from every column of the model, so a column the type hid could still be filtered on (read one `startsWith` probe at a time), sorted by, returned verbatim as a group key, or read through `min`/`max`. Each now covers only the columns and relations shown by the types it serves: the types it is attached to, and through `object:` the type the relation returns. `orm.schema()` works this out for the types each schema reaches, so an admin schema can keep a column a public schema hides; a schema built with `strawberry.Schema` directly gets the inputs uncut. A field with its own resolver, `on=`, `compute=` or permission classes does not count, since it does not show the column as stored; a `scope=` does, because traversal applies it too. Kept fields carry the type field's tags. **Breaking:** clients filtering, sorting or grouping on a column the type does not show get a validation error; show the column on the type to keep it. This replaces the error raised when a type's `exclude=` and its inputs disagreed.

### Fixed

- A type with `group=` but neither `filters=` nor `order=` could not be listed through `orm.connection()`: the generated resolver declared `filter` and `order` arguments it had no types for, which Strawberry refuses.
- Relation connections handed each parent's page to its resolver on the shared model instance and never removed it. On SQLAlchemy, whose session returns the same instance wherever a row is reached, an alias or a later request in the same session read that page, with the earlier field's filter and the earlier caller's `scope_rows`, instead of its own rows. Each field now takes its own page, and none outlives the field.
- The windowed read behind relation connections and `groups { items }` used `first` and the `after` cursor as a row limit before they were checked. `first: 100000` read every related row before being refused, and a forged `after` cursor read every row and returned empty pages without an error. The window is now computed as Strawberry computes the page, so an out-of-range `first` is refused before any read, and it starts at the page rather than the first row. `first` with `before` now pages as Strawberry defines it, and `items` without `first` returns up to `max_results` rather than eleven rows reported as the last page.

## 0.24.2

### Fixed

- **Rows that tie on a connection's order no longer repeat or go missing between pages.** Since 0.24.1 each page is its own query, and where the order was not unique a database could break ties differently on each page, so a row could appear on two pages and another on none. The cut now ends every order with the primary key, unless it already includes it: the client's `order`, the resolver's, the model's default, or none at all. This applies on all three backends, sync and async, to root connections and to the windowed pages of relation connections and `groups { items }`. Cursors, `pageInfo`, `totalCount` and the order of rows that do not tie are unchanged.
- Relation connections over several parents failed under async Django and with an SQLAlchemy `AsyncSession`: each parent's total was counted synchronously.

## 0.24.1

### Added

- `apply_page(query, offset, limit)` on the backend protocol. A backend returns the query cut to one page, or `None` to have the connection read it whole.

### Fixed

- **Connections read only the page they are asked for.** A connection over a query (`orm.connection.*`, `orm.payload.connection`, `ORMListConnection`) used to read every matching row, with its eager loads, and slice the page in Python. It now sends `LIMIT first + 1` and `OFFSET`, on all three backends, sync and async. `totalCount` is counted only when selected, and not when the page came back short. `last` counts to locate its window instead of reading the table. Pages, cursors and `pageInfo` are unchanged. See [Connection fields](README.md#connection-fields) for what is still read whole.
- `totalCount` selected with `aggregates` or `groups` raised `'coroutine' object has no attribute 'total_count'` on async backends (Tortoise, SQLAlchemy `AsyncSession`, async Django).
- Tortoise: `groups { items }` failed with an SQL syntax error, because the items were fetched before the group key fields were known.
- `count_query` works under async execution on Django and with an `AsyncSession`, counts `DISTINCT` queries correctly on Tortoise, and on SQLAlchemy counts an entity a join repeats once, as the rows are read.
- SQLAlchemy: a refused or dropped connection is reported as `ConnectionError("Database connection failed")` instead of `Invalid filter expression`. Both messages still hide the database error from clients, which names tables, columns or the host; it is kept as the exception's cause, for the server log.

## 0.24.0

### Added

- **Multi-hop `using=` paths.** `using=["author.posts"]` (or `"author__posts"`) loads every relation on the path with the parent query, on all three backends. Each hop is checked when the type is defined, and an error names the hop that does not exist.

### Fixed

- **`scope_rows` applies to rows a resolver returns already loaded.** Returning a list (`list(qs)`, `.scalars().all()`, `await qs`) instead of a query, or rows whose relations the resolver already prefetched, no longer skips the related type's `scope_rows`: rejected rows are dropped from lists and to-one relations read as `None`, on all three backends. Returning an unsaved instance of a scoped type raises, since it has no primary key to check.
- **Rows loaded through `using=` obey `scope_rows`.** Each hop applies the related type's `scope_rows` and the `scope=` of the field named for that relation, exactly as a selection of it would. Previously a hint loaded its relation unscoped. On Django and Tortoise, selecting the same relation alongside the hinted field returned the hidden row through the selected field.
- A relation reached both by the selection and by a `using=` path is loaded once, so the two can no longer disagree about which rows are cached.
- Tortoise: a scoped relation nested under another scoped relation loaded its rows unscoped. Scoped relations at any depth are now prefetched with their scope in one query per level, instead of falling back to one query per parent row.
- Tortoise: scoped forward relations are loaded in bulk. A hidden row reads as `None` and leaves the foreign key column untouched.
- Tortoise: forward one-to-one relations in the selection are prefetched.

## 0.23.0

### Added

- **Schema tags.** Build several GraphQL schemas from one set of types: tag what belongs to only some of them, then build one per tag with `orm.schema(..., tags="public")`.
  - `tags=` on `orm.field.eager`, `lazy` and `computed`, `orm.connection.*`, `orm.node` and `orm.payload.*`.
  - `tags("a", "b")`, for `metadata=` on anything declared with Strawberry itself: fields, mutations, arguments and input fields.
  - A schema built for some tags keeps what is untagged or carries one of them, and the types that still reach. Building it fails, naming the field, for a hidden argument or input field without a default, and for a type with every field hidden.
- `metadata=` on the same ORM helpers, passed to the Strawberry field.
- `orm.schema(schema_class=...)` builds a `strawberry.Schema` subclass of your own, with the library's extensions mounted.

### Changed

- On an `@orm.type`, a `strawberry.field(metadata=...)` that sets nothing else no longer replaces the model attribute's resolution. It used to stop Django and Tortoise from generating the relation's resolver.

## 0.22.0

### Added

- `orm.mutations.upsert_node_input(model=, project=, name=)` builds a standalone `{where, create, update}` upsert root for one model. `_meta.upsert.where` accepts a field list or `True` for caller-chosen optional fields.
- `_meta.name` names a projection's generated types and lets a projection recurse; `_meta.upsert.optional` makes a listed `where` optional.
- Insert-only (find-or-create) upserts:
  - `_meta.upsert.onMatch` chooses what a matched row does: `"UPDATE"` (the default), `"IGNORE"` or `"APPEND"`.
  - `_meta.immutable: True` removes `update`, `unlink`, `delete` and `onReplace`, and matches with `"APPEND"`.
  - `_meta.upsert.onMatchLinks: "IGNORE"` skips forward links sent for a matched row instead of rejecting them.
  - `_meta.maxVisits` caps how often a model may appear on one nesting path.
  - Contradictory keys raise `ValueError` naming the model and keys.
- `orm.apply_upsert(input, info, *, authorize=None)` executes an upsert input on all three backends as one unit of work: a savepoint on SQLAlchemy (sync and async), `transaction.atomic()` on Django (sync and async), and `in_transaction()` on Tortoise. It validates the whole payload first and raises `UpsertValidationError`, `UpsertNotFoundError` or `PermissionError`.
- `AbstractRepo.find_for_upsert`, `find_for_upsert_async`, `can_upsert` and `on_match`.
- Generated upsert inputs carry `__upsert_policy__`; `__relation_policy__` gains `on_match`, `immutable` and `max_visits`.
- `make_ref_type(..., descriptions=)` sets GraphQL descriptions on its variants.

### Changed

- **`apply_ref_list` now applies the `upsert` arm.** It used to drop it silently. The item is found or created under the parent row and linked, on every backend. On Django and Tortoise, a ref list containing an upsert runs in one transaction, so an error part way leaves no rows.
- **Single links without an `onReplace` field are `@oneOf`.** Previously a payload with several operations was resolved by precedence. This covers update-only links, links whose `onReplace` is narrowed to one option, and links to immutable targets. A link that offers `onReplace` stays a plain input, because `onReplace` is sent alongside `create` or `upsert`.
- A projection with no update operation generates no `*Patch` or `*Update` types. `update_node_input()` raises `ValueError` for a model whose projection has none, instead of building an update branch from every field.
- Projections that use none of the new keys generate the same SDL as before, apart from the `@oneOf` change above.

### Fixed

- `AbstractRepo._create_async` and `_save_async` await the flush on a SQLAlchemy `AsyncSession`, so the row has its primary key when the call returns.
