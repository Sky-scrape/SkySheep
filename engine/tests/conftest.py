"""测试共享夹具。"""

from __future__ import annotations

import pytest

from skysheep.models.fake import FakeProvider  # noqa: F401  (re-exported for tests)
from skysheep.session.store import SessionStore

__all__ = ["FakeProvider", "store", "home"]


@pytest.fixture
def home(tmp_path, monkeypatch):
    """隔离的 SkySheep home + 临时项目目录（proj/）。"""
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    (tmp_path / "proj").mkdir()
    return tmp_path


@pytest.fixture
async def store(tmp_path):
    s = await SessionStore(tmp_path / "test.db").connect()
    yield s
    await s.close()
