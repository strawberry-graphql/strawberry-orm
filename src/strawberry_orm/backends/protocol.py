"""Backend protocol that all ORM adapters must implement."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from strawberry.extensions import SchemaExtension

from strawberry_orm._async import AwaitableOrValue


@runtime_checkable
class Backend(Protocol):
    """Abstract interface implemented by each ORM backend.

    Every method receives ``**kwargs`` so individual backends can accept
    backend-specific options without breaking the common interface.
    """

    # -- Type generation -----------------------------------------------------

    def type(self, model: type, **kwargs: Any) -> Any:
        """Generate a Strawberry object type from an ORM model."""
        ...

    def input(self, model: type, **kwargs: Any) -> Any:
        """Generate a Strawberry input type from an ORM model."""
        ...

    def partial(self, model: type, **kwargs: Any) -> Any:
        """Generate a partial (all-optional) Strawberry input from a model."""
        ...

    def filter(self, model_or_type: type, **kwargs: Any) -> Any:
        """Generate a @oneOf filter input for a model or Strawberry type."""
        ...

    def order(self, model_or_type: type, **kwargs: Any) -> Any:
        """Generate an ordering input for a model or Strawberry type."""
        ...

    def filter_type(self, model: type, **kwargs: Any) -> Any:
        """Return a decorator that builds a filter input with custom fields."""
        ...

    def order_type(self, model: type, **kwargs: Any) -> Any:
        """Return a decorator that builds an order input with custom fields."""
        ...

    def group(self, model_or_type: type, **kwargs: Any) -> Any:
        """Generate a @oneOf group-by input for a model."""
        ...

    def group_type(self, model: type, **kwargs: Any) -> Any:
        """Return a decorator that builds a group-by input with custom fields."""
        ...

    def aggregate(self, model_or_type: type, **kwargs: Any) -> Any:
        """Return a marker for auto-generated aggregation."""
        ...

    def aggregate_type(self, model: type, **kwargs: Any) -> Any:
        """Return a decorator that registers a custom aggregate class."""
        ...

    # -- Fields --------------------------------------------------------------

    def field(self, **kwargs: Any) -> Any:
        """Create a Strawberry field descriptor with optimizer hints."""
        ...

    def node(self, **kwargs: Any) -> Any:
        """Create a Relay node field descriptor."""
        ...

    def connection(self, **kwargs: Any) -> Any:
        """Create a Relay connection field descriptor."""
        ...

    # -- Mutations -----------------------------------------------------------

    def create(self, input_type: type, **kwargs: Any) -> Any:
        """Create a mutation field that inserts a new object."""
        ...

    def update(self, input_type: type, **kwargs: Any) -> Any:
        """Create a mutation field that updates an existing object."""
        ...

    def delete(self, **kwargs: Any) -> Any:
        """Create a mutation field that deletes objects."""
        ...

    # -- Related list refs ---------------------------------------------------

    def ref(
        self,
        model: type,
        *,
        create: type | None = None,
        update: type | None = None,
        delete: bool = False,
    ) -> type:
        """Generate a ``@oneOf`` ref input for managing a related list."""
        ...

    def apply_ref_list(
        self,
        instance: Any,
        field: str,
        refs: list[Any],
        info: Any,
        *,
        authorize: Any | None = None,
    ) -> AwaitableOrValue[None]:
        """Apply a list of ref operations to *instance*'s *field* relation.

        Each ref is a ``@oneOf`` input with exactly one of ``create``,
        ``update`` (link + optional field updates), ``unlink`` (remove from
        relation), or ``delete`` (hard-delete the row).
        """
        ...

    # -- Query application ----------------------------------------------------

    def apply_filters(
        self, query: Any, filter_input: Any, model: type, info: Any = None
    ) -> Any:
        """Translate a filter input object into ORM-specific query conditions."""
        ...

    def apply_ordering(
        self, query: Any, order_input: Any, model: type, info: Any = None
    ) -> Any:
        """Translate a list of ``@oneOf`` order entries into ORM-specific ordering.

        Each entry represents one column; list position determines tie-break
        priority.
        """
        ...

    # -- Grouping / aggregation -----------------------------------------------

    def apply_aggregation(
        self, query: Any, info: Any, aggregate_meta: Any
    ) -> AwaitableOrValue[Any]:
        """Run aggregate functions on the full filtered query.

        Only the aggregates requested in the GraphQL selection set are
        computed (selection-set-driven optimization).
        """
        ...

    def apply_grouping(
        self,
        query: Any,
        group_by_input: Any,
        info: Any,
        aggregate_meta: Any,
        *,
        order_input: Any | None = None,
    ) -> AwaitableOrValue[list[Any]]:
        """Run GROUP BY with aggregates and return Group instances.

        If *order_input* contains fields that overlap with *group_by_input*,
        the groups are sorted by those fields.
        """
        ...

    def scope_query_to_group(self, query: Any, group_key: Any) -> Any:
        """Add WHERE clauses to *query* matching the group key values.

        Fallback for unbatched per-group ``items`` resolution.
        """
        ...

    def batch_group_items(
        self,
        query: Any,
        group_key_fields: list[str],
        info: Any,
        model: type,
        *,
        per_group_limit: int,
        order_input: Any | None = None,
    ) -> AwaitableOrValue[dict[tuple, list[Any]]]:
        """Fetch the first *per_group_limit* items for every group in one query.

        Uses ``ROW_NUMBER() OVER (PARTITION BY ...)`` to avoid N+1.
        Returns a dict mapping group-key tuples to lists of model instances.
        """
        ...

    def group_counts(
        self, query: Any, key_field: str, info: Any
    ) -> AwaitableOrValue[dict[Any, int]]:
        """Count *query*'s rows per distinct value of *key_field*, in one query.

        A windowed page cannot report ``totalCount``, having kept only the
        page, so the totals come from here instead.
        """
        ...

    # -- Queryset overrides --------------------------------------------------

    def get_default_queryset(self, model: type) -> Any:
        """Return the default queryset for *model*."""
        ...

    def is_query_object(self, value: Any) -> bool:
        """Return ``True`` if *value* is a query object the optimizer can handle."""
        ...

    def is_model_instance(self, value: Any) -> bool:
        """Return ``True`` if *value* is a persisted model instance."""
        ...

    def load_relations(
        self, store: Any, instances: list[Any], info: Any
    ) -> AwaitableOrValue[list[Any]]:
        """Eager-load the selected relations onto *instances*, in place.

        Returns the rows it loaded onto - empty when the selection named no
        relations, so callers can tell a real load from a no-op.
        """
        ...

    def relation_names(self, model: type) -> set[str]:
        """Return the names of *model*'s relations, for hint validation."""
        ...

    def query_probe(self, info: Any) -> Any:
        """Context manager counting SQL statements issued while it is open."""
        ...

    def instance_pk(self, instance: Any) -> Any:
        """Primary key of *instance*, used to key batched rows back to parents."""
        ...

    def split_parent_predicate(
        self, query: Any, parent_pk: Any
    ) -> tuple[str, Any, Any] | None:
        """Split *query* into its parent predicate and the rest, if safe."""
        ...

    def query_signature(self, query: Any) -> str | None:
        """Stable structural signature of *query*, for grouping batches."""
        ...

    def apply_key_filter(
        self, query: Any, attr_name: str, key_handle: Any, keys: list[Any]
    ) -> Any:
        """Restrict *query* to rows whose parent key is one of *keys*."""
        ...

    def materialize_query(self, query: Any, info: Any) -> AwaitableOrValue[list[Any]]:
        """Evaluate *query* into model instances for resolver-backed fields."""
        ...

    # -- Optimizer -----------------------------------------------------------

    def optimizer_extension(self, **kwargs: Any) -> type[SchemaExtension]:
        """Return the schema extension class that handles query optimization."""
        ...

    def apply_optimizer_hints(
        self, store: Any, query: Any, info: Any
    ) -> AwaitableOrValue[Any]:
        """Apply field-level hints from *store* onto *query*."""
        ...
