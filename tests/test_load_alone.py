"""Which loads onto one row keep each other from marking it.

A load checks what a relation holds and marks it without awaiting, so a load
on the same event loop cannot come between the two; one in another thread
can, when it loads one of the same relations.
"""

import threading

from strawberry_orm.lazy_resolution import load_alone


class _Row:
    pass


def _in_thread(work):
    found = []
    thread = threading.Thread(target=lambda: found.append(work()))
    thread.start()
    thread.join()
    return found[0]


def _alone_after(row, names):
    with load_alone([row], names) as alone:
        return alone()


class TestLoadAlone:
    def test_a_load_in_another_thread_of_the_same_relation_crosses(self):
        row = _Row()
        with load_alone([row], ["posts"]) as alone:
            assert _in_thread(lambda: _alone_after(row, ["posts", "tags"])) is False
            assert alone() is False

    def test_a_load_on_the_same_thread_does_not_cross(self):
        row = _Row()
        with load_alone([row], ["posts"]) as alone:
            assert _alone_after(row, ["posts"]) is True
            assert alone() is True

    def test_a_load_of_another_relation_does_not_cross(self):
        row = _Row()
        with load_alone([row], ["posts"]) as alone:
            assert _in_thread(lambda: _alone_after(row, ["tags"])) is True
            assert alone() is True

    def test_a_finished_load_crosses_nothing(self):
        row = _Row()
        assert _in_thread(lambda: _alone_after(row, ["posts"])) is True
        assert _alone_after(row, ["posts"]) is True
