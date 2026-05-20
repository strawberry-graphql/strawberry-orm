"""Optional runtime guardrails for lazy ORM relation resolution."""

from __future__ import annotations

import re
import warnings
from typing import TYPE_CHECKING, Any

from strawberry.extensions import SchemaExtension

if TYPE_CHECKING:
    from strawberry_orm.backends.protocol import Backend


class LazyResolutionExtension(SchemaExtension):
    """Warn or error when a Django FK is resolved without prefetch/cache."""

    _backend: Backend | None = None
    _mode: str = "warn"

    @classmethod
    def configure(
        cls,
        backend: Backend,
        *,
        mode: str = "warn",
    ) -> type[LazyResolutionExtension]:
        return type(
            f"{cls.__name__}_{backend.__class__.__name__}",
            (cls,),
            {"_backend": backend, "_mode": mode},
        )

    def resolve(self, _next: Any, root: Any, info: Any, *args: Any, **kwargs: Any) -> Any:
        self._check_django_fk_access(root, info)
        return _next(root, info, *args, **kwargs)

    async def resolve_async(
        self,
        _next: Any,
        root: Any,
        info: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        self._check_django_fk_access(root, info)
        return await _next(root, info, *args, **kwargs)

    def _check_django_fk_access(self, root: Any, info: Any) -> None:
        if self._mode == "off" or self._backend is None or root is None:
            return

        backend = self._backend
        if backend.__class__.__name__ != "DjangoBackend":
            return

        field_name = getattr(info, "python_name", None) or getattr(info, "field_name", None)
        if not field_name:
            return
        field_name = re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", field_name).lower()

        try:
            field = root._meta.get_field(field_name)  # type: ignore[attr-defined]
        except Exception:
            return

        if type(field).__name__ not in ("ForeignKey", "OneToOneField"):
            return

        if getattr(field, "is_cached", lambda instance: True)(root):
            return

        message = (
            f"Lazy resolution of '{field_name}' on {type(root).__name__} may cause "
            f"N+1 queries or SynchronousOnlyOperation under async GraphQL. Use "
            f"optimizer_extension() or explicit prefetch/select_related."
        )
        if self._mode == "error":
            raise RuntimeError(message)
        warnings.warn(message, stacklevel=2)
