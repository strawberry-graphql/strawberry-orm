"""Backend creation and optimizer extension tests."""

import pytest


class TestBackendCreation:
    def test_creates_tortoise_backend(self, orm):
        from strawberry_orm.backends.tortoise import TortoiseBackend

        assert isinstance(orm.backend, TortoiseBackend)


class TestOptimizerExtension:
    def test_extension_is_a_class(self, orm):
        ext = orm.optimizer_extension()
        assert isinstance(ext, type)
