"""Shared helpers for scoping hook call-order tests."""

from __future__ import annotations

from typing import Any

SCOPE_PREFIX = "SCOPE:"


def _published_posts_queryset(Post: type, backend_name: str, qs: Any) -> Any:
    if backend_name == "sqlalchemy":
        return qs.where(Post.is_published == True)  # noqa: E712
    return qs.filter(is_published=True)


def _posts_load_exclude_guide(Post: type, backend_name: str, qs: Any) -> Any:
    if backend_name == "sqlalchemy":
        return qs.where(Post.title != "GraphQL Guide")
    return qs.exclude(title="GraphQL Guide")


def scope_messages(print_calls: list[str]) -> list[str]:
    return [msg for msg in print_calls if msg.startswith(SCOPE_PREFIX)]


def assert_get_queryset_before_load(messages: list[str]) -> None:
    get_qs = f"{SCOPE_PREFIX}PostType.get_queryset"
    load = f"{SCOPE_PREFIX}UserType.posts.load"
    assert get_qs in messages
    assert load in messages
    get_indices = [i for i, msg in enumerate(messages) if msg == get_qs]
    load_indices = [i for i, msg in enumerate(messages) if msg == load]
    for get_idx in get_indices:
        later_loads = [load_idx for load_idx in load_indices if load_idx > get_idx]
        assert later_loads, (
            f"expected a {load} call after {get_qs} at index {get_idx}, "
            f"got order: {messages}"
        )
