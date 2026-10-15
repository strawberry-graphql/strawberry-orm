from tests.abstract.insert_only_upserts import (
    AbstractTestApplyRefList,
    AbstractTestBooks,
    AbstractTestFindOrCreate,
    AbstractTestForwardLinks,
    AbstractTestHandBuiltPayloads,
    AbstractTestHooks,
    AbstractTestOnMatch,
    AbstractTestUpdateOnMatch,
    AbstractTestValidation,
)


class TestFindOrCreate(AbstractTestFindOrCreate):
    pass


class TestOnMatch(AbstractTestOnMatch):
    pass


class TestForwardLinks(AbstractTestForwardLinks):
    pass


class TestValidation(AbstractTestValidation):
    pass


class TestHandBuiltPayloads(AbstractTestHandBuiltPayloads):
    pass


class TestHooks(AbstractTestHooks):
    pass


class TestApplyRefList(AbstractTestApplyRefList):
    pass


class TestUpdateOnMatch(AbstractTestUpdateOnMatch):
    pass


class TestBooks(AbstractTestBooks):
    pass
