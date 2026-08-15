"""Unit tests for the Django reflection primitives used by batching."""

from django.db.models import Q

from strawberry_orm.backends.django import DjangoBackend
from tests.backends.django.models import Post as DjPost


class TestDjangoSplitParentPredicate:
    def _backend(self):
        return DjangoBackend(warn_missing_scope=False)

    def test_splits_a_plain_parent_predicate(self):
        qs = DjPost.objects.filter(author_id=1)
        attr, handle, remainder = self._backend().split_parent_predicate(qs, 1)
        assert attr == "author_id"
        assert handle == "author_id"
        assert not remainder.query.where.children

    def test_keeps_other_criteria_in_the_remainder(self):
        qs = DjPost.objects.filter(author_id=1, is_published=True)
        attr, _, remainder = self._backend().split_parent_predicate(qs, 1)
        assert attr == "author_id"
        assert len(remainder.query.where.children) == 1

    def test_boolean_column_is_not_mistaken_for_the_parent_key(self):
        """``True == 1`` must not make is_published look like author_id."""
        qs = DjPost.objects.filter(is_published=True)
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_bails_on_extra_select(self):
        qs = DjPost.objects.filter(author_id=1).extra(select={"one": "1"})
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_bails_on_slicing(self):
        qs = DjPost.objects.filter(author_id=1)[:2]
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_bails_on_offset_slicing(self):
        qs = DjPost.objects.filter(author_id=1)[1:]
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_bails_when_the_top_level_node_is_negated(self):
        # Defensive guard: Django does not normally hand back a negated root,
        # so it is forced here rather than reached through the query API.
        qs = DjPost.objects.filter(author_id=1)
        qs.query.where.negated = True
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_bails_when_the_parent_key_is_inside_an_or(self):
        qs = DjPost.objects.filter(Q(author_id=1) | Q(title="nothing"))
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_bails_when_the_parent_key_is_nested_two_levels_deep(self):
        qs = DjPost.objects.filter(
            Q(Q(author_id=1) & Q(title="x")) | Q(title="nothing")
        )
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_allows_a_negation_that_does_not_mention_the_parent(self):
        qs = DjPost.objects.filter(author_id=1).exclude(title="Draft Post")
        attr, _, remainder = self._backend().split_parent_predicate(qs, 1)
        assert attr == "author_id"
        assert len(remainder.query.where.children) == 1

    def test_bails_when_no_predicate_matches_the_parent(self):
        qs = DjPost.objects.filter(author_id=99)
        assert self._backend().split_parent_predicate(qs, 1) is None

    def test_signature_distinguishes_remainders(self):
        backend = self._backend()
        published = DjPost.objects.filter(is_published=True)
        drafts = DjPost.objects.filter(is_published=False)
        assert backend.query_signature(published) != backend.query_signature(drafts)

    def test_apply_key_filter_adds_an_in_clause(self):
        backend = self._backend()
        filtered = backend.apply_key_filter(
            DjPost.objects.all(), "author_id", "author_id", [1, 2]
        )
        assert "IN" in str(filtered.query).upper()

    def test_instance_pk_reads_the_primary_key(self):
        assert self._backend().instance_pk(DjPost(id=7)) == 7

    def test_instance_pk_is_none_without_a_primary_key(self):
        assert self._backend().instance_pk(object()) is None
