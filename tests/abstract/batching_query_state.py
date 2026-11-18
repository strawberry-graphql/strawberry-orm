"""Relation batching merges sibling reads only when nothing else tells them apart.

``BatchingExtension`` lifts each parent's own predicate out of its query and
runs one query, the first sibling's, for every parent whose remainder signs
the same. Whatever shapes the rows a query hands back without showing in its
SQL - its prefetches, the classes it builds rows with, state an app keeps on
it - has to be in that signature, and a query carrying state the backend
cannot vouch for is read on its own.

Each backend binds :class:`AbstractTestBatchingQueryState` and supplies:

- ``builds``: a mapping of state name to ``build(author_pk, mine, viewer)``,
  a query of the author's posts carrying that state; ``mine`` says whether
  the author is the viewer;
- the fixtures ``backend``; ``read(state, viewer, batch=...)``, what a schema
  reading each user's posts through that build answers ``viewer``; and the
  parametrized ``state`` (siblings differ in it), ``shared`` (siblings carry
  the same) and ``observed`` (a schema can tell), each naming a build.
"""

from typing import Any


def batch_key(backend: Any, query: Any, parent_pk: Any) -> str | None:
    """The group ``BatchingExtension`` reads *query* in, or None for none."""
    split = backend.split_parent_predicate(query, parent_pk)
    if split is None:
        return None
    attr_name, _key_handle, remainder = split
    signature = backend.query_signature(remainder)
    return None if signature is None else f"{attr_name}|{signature}"


class AbstractTestBatchingQueryState:
    builds: dict[str, Any]

    def test_siblings_differing_in_it_are_not_batched_together(
        self, backend, state, seed
    ):
        build = self.builds[state]
        # Alice, the viewer, reads her own posts and then Bob's; both queries
        # are kept, as the batch keeps every sibling's while it compares them.
        queries = build(1, True, 1), build(2, False, 1)
        own, other = (
            batch_key(backend, query, pk)
            for query, pk in zip(queries, (1, 2), strict=True)
        )
        assert own is None or other is None or own != other, own

    def test_siblings_sharing_it_are_still_batched(self, backend, shared, seed):
        build = self.builds[shared]
        keys = [batch_key(backend, build(pk, False, 3), pk) for pk in (1, 2)]
        assert keys[0] is not None, "not batched"
        assert keys[0] == keys[1]

    def test_a_batched_read_answers_as_each_parent_read_alone_does(
        self, read, observed
    ):
        for viewer in (1, 2, 3):
            batched = read(observed, viewer, batch=True)
            assert batched == read(observed, viewer, batch=False), (viewer, batched)
