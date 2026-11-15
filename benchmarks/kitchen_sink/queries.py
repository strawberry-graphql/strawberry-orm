"""The workload: the same GraphQL documents on every backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from strawberry.relay.utils import to_base64


@dataclass(frozen=True)
class Workload:
    name: str
    description: str
    query: str
    variables: dict[str, Any] = field(default_factory=dict)


FLAT_PAGE = Workload(
    "flat_page",
    "500 posts, scalar columns only, with totalCount",
    """
    { posts(first: 500, order: [{field: {id: ASC}}]) {
        totalCount
        edges { node { id title status views rating publishedAt } }
    } }
    """,
)

DEEP_NESTED = Workload(
    "deep_nested",
    "200 posts -> author -> organization, category -> parent, tags (m2m), "
    "comments -> author; scoped users and comments",
    """
    { posts(first: 200, order: [{field: {id: ASC}}]) { edges { node {
        id title
        author { name organization { name country } }
        category { name parent { name } }
        tags { name }
        comments { body upvotes author { name } }
    } } } }
    """,
)

CRAZY_FILTER = Workload(
    "crazy_filter",
    "all/any/not across author->organization, m2m tags, reverse-FK comments, "
    "category; ordered by a related column; totalCount",
    """
    { posts(
        first: 50
        filter: {all: [
          {field: {views: {gte: 20}}}
          {not: {field: {status: {exact: "draft"}}}}
          {any: [
            {object: {author: {object: {organization: {field: {country: {inList: ["US", "DE", "JP"]}}}}}}}
            {object: {tags: {field: {name: {inList: ["tag-01", "tag-02", "tag-03", "tag-04"]}}}}}
            {object: {category: {field: {name: {inList: ["Category 02", "Category 07"]}}}}}
            {field: {title: {iContains: "graphql"}}}
          ]}
          {object: {comments: {field: {upvotes: {gte: 5}}}}}
        ]}
        order: [{object: {author: {field: {name: ASC}}}}, {field: {publishedAt: DESC}}, {field: {id: ASC}}]
      ) {
        totalCount
        edges { node {
          title views status
          author { name }
          category { name }
          tags { name }
        } }
    } }
    """,
)

USERS_TREE = Workload(
    "users_tree",
    "100 users -> organization, an on= view of their posts (ordered) -> tags, comments",
    """
    { users(first: 100, order: [{field: {id: ASC}}]) { edges { node {
        name score
        organization { name }
        recentPosts { title views tags { name } comments { body } }
    } } } }
    """,
)

NESTED_CONNECTIONS = Workload(
    "nested_connections",
    "50 users, each with an eager (windowed) connection of their posts (first: 5), "
    "totalCount",
    """
    { users(first: 50, order: [{field: {id: ASC}}]) { edges { node {
        name
        posts(first: 5, order: [{field: {views: DESC}}, {field: {id: ASC}}]) {
          totalCount
          edges { node { title views } }
        }
    } } } }
    """,
)

NESTED_CONNECTIONS_LAZY = Workload(
    "nested_connections_lazy",
    "The same, through a lazy connection: one query per user",
    """
    { users(first: 50, order: [{field: {id: ASC}}]) { edges { node {
        name
        postsLazy(first: 5, order: [{field: {views: DESC}}, {field: {id: ASC}}]) {
          totalCount
          edges { node { title views } }
        }
    } } } }
    """,
)

LAZY_BATCHED = Workload(
    "lazy_batched",
    "300 posts, each with a hand-written lazy resolver (top comments) -> author",
    """
    { posts(first: 300, order: [{field: {id: ASC}}]) { edges { node {
        title
        topComments { body upvotes author { name } }
    } } } }
    """,
)

AGGREGATES_GROUPS = Workload(
    "aggregates_groups",
    "Group by category name x status, whole-set and per-group aggregates",
    """
    { posts(first: 100, groupBy: [{object: {category: {field: {name: true}}}}, {field: {status: true}}],
            order: [{field: {views: DESC}}, {field: {id: ASC}}]) {
        totalCount
        aggregates { count sum { views } avg { rating } min { publishedAt } max { publishedAt } }
        groups {
          key { category { name } status }
          aggregates { count avg { views } max { rating } }
          edgeIndices
        }
        edges { node { id title } }
    } }
    """,
)

DATE_GROUPS = Workload(
    "date_groups",
    "Published posts grouped by month, with three items per group",
    """
    { posts(first: 200, filter: {field: {status: {exact: "published"}}},
            groupBy: [{field: {publishedAt: {interval: MONTH}}}]) {
        groups {
          key { publishedAt }
          aggregates { count sum { views } }
          items(first: 3) { edges { node { title } } }
        }
    } }
    """,
)

OBJECTS_GROUPS = Workload(
    "objects_groups",
    "Group by the set of each post's tags (objects:)",
    """
    { posts(first: 100, groupBy: [{objects: {tags: {field: {name: true}}}}]) {
        totalCount
        groups { key { objects { tags { name } } } aggregates { count } }
        edges { node { id } }
    } }
    """,
)

DEEP_PAGE = Workload(
    "deep_page",
    "Page 81 (offset 4000) of posts by views, with author",
    """
    query($after: String) { posts(first: 50, after: $after,
                                  order: [{field: {views: DESC}}, {field: {id: ASC}}]) {
        totalCount
        pageInfo { hasNextPage endCursor }
        edges { node { title views author { name } } }
    } }
    """,
    {"after": to_base64("arrayconnection", 3999)},
)

COMMENTS_SEARCH = Workload(
    "comments_search",
    "Comments containing a word by authors in two countries -> post -> author",
    """
    { comments(first: 100,
        filter: {all: [
          {field: {body: {iContains: "graphql"}}}
          {object: {author: {object: {organization: {field: {country: {inList: ["US", "GB"]}}}}}}}
        ]}
        order: [{field: {upvotes: DESC}}, {field: {id: ASC}}]) {
        totalCount
        edges { node { body upvotes author { name } post { title author { name } } } }
    } }
    """,
)

WORKLOADS = [
    FLAT_PAGE,
    DEEP_NESTED,
    CRAZY_FILTER,
    USERS_TREE,
    NESTED_CONNECTIONS,
    NESTED_CONNECTIONS_LAZY,
    LAZY_BATCHED,
    AGGREGATES_GROUPS,
    DATE_GROUPS,
    OBJECTS_GROUPS,
    DEEP_PAGE,
    COMMENTS_SEARCH,
]
