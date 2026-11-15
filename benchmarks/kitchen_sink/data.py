"""Deterministic rows, the same for every backend.

Each table is a list of dicts keyed by column name (foreign keys as ``<name>_id``),
with explicit primary keys so the backends hold identical data.
"""

from __future__ import annotations

import datetime
import random
from dataclasses import dataclass, field

COUNTRIES = ["US", "GB", "DE", "FR", "JP", "BR", "IN", "CA", "AU", "NL"]
STATUSES = ["draft", "published", "published", "published", "archived"]
WORDS = [
    "lorem",
    "ipsum",
    "dolor",
    "sit",
    "amet",
    "consectetur",
    "adipiscing",
    "elit",
    "sed",
    "do",
    "eiusmod",
    "tempor",
    "incididunt",
    "ut",
    "labore",
    "et",
    "dolore",
    "magna",
    "aliqua",
    "enim",
    "ad",
    "minim",
    "veniam",
    "quis",
    "nostrud",
    "exercitation",
    "ullamco",
    "laboris",
    "nisi",
    "aliquip",
    "ex",
    "ea",
    "commodo",
    "consequat",
    "graphql",
    "python",
    "rust",
    "django",
    "sqlalchemy",
    "tortoise",
    "strawberry",
    "relay",
    "cursor",
    "query",
]


@dataclass
class Dataset:
    organizations: list[dict] = field(default_factory=list)
    users: list[dict] = field(default_factory=list)
    categories: list[dict] = field(default_factory=list)
    tags: list[dict] = field(default_factory=list)
    posts: list[dict] = field(default_factory=list)
    post_tags: list[tuple[int, int]] = field(default_factory=list)
    comments: list[dict] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "organizations": len(self.organizations),
            "users": len(self.users),
            "categories": len(self.categories),
            "tags": len(self.tags),
            "posts": len(self.posts),
            "post_tags": len(self.post_tags),
            "comments": len(self.comments),
        }


def _sentence(rng: random.Random, n: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n)).capitalize()


def generate(scale: float = 1.0, seed: int = 42) -> Dataset:
    """About 5,000 posts, 25,000 comments and 15,000 tag links at scale 1."""
    rng = random.Random(seed)
    data = Dataset()
    n_orgs = max(2, int(20 * scale))
    n_users = max(10, int(500 * scale))
    n_categories = 30
    n_tags = 60
    n_posts = max(50, int(5000 * scale))
    epoch = datetime.datetime(2025, 1, 1)

    for i in range(1, n_orgs + 1):
        data.organizations.append(
            {
                "id": i,
                "name": f"Org {i:03d}",
                "country": COUNTRIES[i % len(COUNTRIES)],
                "founded": datetime.date(1990 + i % 30, 1 + i % 12, 1 + i % 28),
            }
        )
    for i in range(1, n_users + 1):
        data.users.append(
            {
                "id": i,
                "organization_id": 1 + (i * 7) % n_orgs,
                "name": f"User {i:04d}",
                "email": f"user{i}@example.com",
                "is_active": i % 10 != 0,
                "score": rng.randint(0, 1000),
                "joined_at": epoch
                + datetime.timedelta(minutes=rng.randint(0, 500_000)),
            }
        )
    for i in range(1, n_categories + 1):
        data.categories.append(
            {
                "id": i,
                "name": f"Category {i:02d}",
                # The first five are roots; the rest hang under one of them.
                "parent_id": None if i <= 5 else 1 + i % 5,
            }
        )
    for i in range(1, n_tags + 1):
        data.tags.append({"id": i, "name": f"tag-{i:02d}"})
    comment_id = 0
    for i in range(1, n_posts + 1):
        status = rng.choice(STATUSES)
        published = (
            epoch + datetime.timedelta(minutes=rng.randint(0, 600_000))
            if status != "draft"
            else None
        )
        data.posts.append(
            {
                "id": i,
                "author_id": 1 + int(rng.paretovariate(1.2) * 40) % n_users,
                "category_id": 1 + rng.randrange(n_categories),
                "title": _sentence(rng, 6),
                "body": _sentence(rng, 60),
                "status": status,
                "views": int(rng.paretovariate(1.5) * 50),
                "rating": round(rng.uniform(1, 5), 2),
                "published_at": published,
            }
        )
        for tag_id in rng.sample(range(1, n_tags + 1), rng.randint(0, 6)):
            data.post_tags.append((i, tag_id))
        for _ in range(rng.choice([0, 2, 4, 5, 6, 8, 10])):
            comment_id += 1
            data.comments.append(
                {
                    "id": comment_id,
                    "post_id": i,
                    "author_id": 1 + rng.randrange(n_users),
                    "body": _sentence(rng, 20),
                    "upvotes": int(rng.paretovariate(1.3) * 3),
                    "is_flagged": rng.random() < 0.05,
                    "created_at": epoch
                    + datetime.timedelta(minutes=rng.randint(0, 600_000)),
                }
            )
    return data
