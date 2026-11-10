"""保持电脑运行：PowerKeeper 的开关是幂等的，停止后线程退出。"""

from __future__ import annotations

import sys

import pytest

from skysheep.core.power import PowerKeeper

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="仅 Windows 有 SetThreadExecutionState")


def test_power_keeper_idempotent_toggle():
    keeper = PowerKeeper()
    try:
        keeper.set_enabled(True)
        keeper.set_enabled(True)  # 重复开启不叠加线程
        assert keeper.enabled()
    finally:
        keeper.stop()
    assert not keeper.enabled()
    # 关掉后再关一次不报错（设置页重复保存路径）
    keeper.stop()
