from tests.abstract.write_path_hooks import (
    AbstractTestAfterHooksWaitForTheChecks,
    AbstractTestAsyncLifecycleHooks,
    AbstractTestRefListNeedsAList,
)
from tests.backends.django.test_write_path_authorization import (  # noqa: F401
    writes,
)


class TestAsyncLifecycleHooks(AbstractTestAsyncLifecycleHooks):
    pass


class TestRefListNeedsAList(AbstractTestRefListNeedsAList):
    pass


class TestAfterHooksWaitForTheChecks(AbstractTestAfterHooksWaitForTheChecks):
    pass
