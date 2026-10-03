"""``skysheep app`` 入口的桌面文件日志接线。

修复前：src/skysheep 全树没有任何 addHandler——桌面日志只在打包入口
desktop.py 接线，源码 ``uv run skysheep app`` 直启时 logs/desktop.log 永不
生成，轮次诊断 diagnostics.turn_breakdown（读该文件里 obs.py 轮末的
``ev=turn`` 行）恒空。这里锁住：obs.setup_file_logging 的落点/格式/轮转
与 desktop.py 同款、幂等不叠加、obs 行落得进文件且 turn_breakdown 读得回、
``_app_cmd`` 入口真的调了它（且先于服务启动）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import time as time_mod
import types
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from skysheep.config import skysheep_home
from skysheep.obs import info as obs_info
from skysheep.obs import setup_file_logging
from skysheep.server.backend import _load_turn_rows

_ENGINE = Path(__file__).resolve().parents[1]


@pytest.fixture
def root_logger_snapshot():
    """接线会往根 logger 挂 handler、改级别：测完摘掉还原，不污染其他用例。"""
    root = logging.getLogger()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    yield root
    root.setLevel(saved_level)
    for h in root.handlers[:]:
        if h not in saved_handlers:
            root.removeHandler(h)
            h.close()


# ---------- 接线：落点与 desktop.log 一致、handler 就位、文件随即存在 ----------

def test_setup_file_logging_wires_desktop_log_path(home, root_logger_snapshot):
    p = setup_file_logging()
    assert p == skysheep_home() / "logs" / "desktop.log"
    assert p.exists(), "RotatingFileHandler 构造即建文件：app 一启动日志就该在"
    wired = [
        h for h in root_logger_snapshot.handlers
        if isinstance(h, RotatingFileHandler)
        and Path(getattr(h, "baseFilename", "")) == p
    ]
    assert len(wired) == 1
    # 行格式与 desktop.py _setup_logging 同款（turn_breakdown 的 asctime 解析靠它）
    assert wired[0].formatter._fmt == "%(asctime)s %(levelname)s %(name)s: %(message)s"
    assert root_logger_snapshot.level == logging.INFO, "obs_info 是 INFO 级，根级别要放行"


def test_setup_file_logging_is_idempotent(home, root_logger_snapshot):
    """重复调用（或 desktop 入口已接过同一路径）不叠加 handler，否则一行落两遍。"""
    p1 = setup_file_logging()
    p2 = setup_file_logging()
    assert p1 == p2
    same = [
        h for h in root_logger_snapshot.handlers
        if getattr(h, "baseFilename", None) and Path(h.baseFilename) == p1
    ]
    assert len(same) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="desktop.py 启动器仅 Windows")
def test_file_log_parity_with_desktop_launcher(home, root_logger_snapshot):
    """与 desktop.py 的「同款」是硬约定：路径、轮转参数逐项对齐。"""
    spec = importlib.util.spec_from_file_location(
        "skysheep_desktop_launcher", _ENGINE / "desktop.py"
    )
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)

    from skysheep import obs

    p = setup_file_logging()
    assert p == desktop.log_path(), "两边必须是同一个 desktop.log"
    assert obs.LOG_MAX_BYTES == desktop.LOG_MAX_BYTES
    handler = next(
        h for h in root_logger_snapshot.handlers
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == p
    )
    assert handler.maxBytes == desktop.LOG_MAX_BYTES
    assert handler.backupCount == 1, "滚出一个 .1，与 desktop.py 的手工轮转保留数一致"


# ---------- 修复的核心闭环：obs 行落文件，turn_breakdown 的读取路径读得回 ----------

def test_turn_log_lands_in_desktop_log_and_is_readable(home, root_logger_snapshot):
    setup_file_logging()
    obs_info("turn", "turn finished sid=abc", session_id="abc", duration_ms=1234,
             slowest_tool="list_dir", tool_calls=1)
    rows = _load_turn_rows(skysheep_home() / "logs" / "desktop.log", "abc")
    assert len(rows) == 1
    assert rows[0]["duration_ms"] == 1234
    assert rows[0]["slowest_tool"] == "list_dir"
    assert rows[0]["ts"] is not None, "行首 asctime 可解析（格式没破）"


def test_setup_file_logging_rotates_like_desktop(home, root_logger_snapshot):
    """超过阈值滚动出 desktop.log.1：命名与 desktop.py 的手工轮转一致，诊断包照收。"""
    p = setup_file_logging(max_bytes=500)
    log = logging.getLogger()
    for i in range(50):
        log.info("填充日志行 %04d %s", i, "x" * 40)
    assert p.exists()
    assert (p.parent / (p.name + ".1")).exists(), "超限后应滚出 .1 备份"


# ---------- 入口接线：_app_cmd 真的调了，且先于服务启动 ----------

def test_app_cmd_wires_file_logging_before_backend(home, monkeypatch):
    import skysheep.cli.app as cli_app

    order: list[str] = []

    def fake_setup():
        order.append("log")
        return skysheep_home() / "logs" / "desktop.log"

    def fake_backend(_args):
        order.append("backend")
        return None, "http://127.0.0.1:1/"

    monkeypatch.setattr("skysheep.obs.setup_file_logging", fake_setup)
    monkeypatch.setattr(cli_app, "_start_backend", fake_backend)
    monkeypatch.setattr(webbrowser, "open", lambda *a, **k: None)

    def fake_sleep(_s):
        raise KeyboardInterrupt  # 浏览器分支的常驻循环立即退出

    monkeypatch.setattr(time_mod, "sleep", fake_sleep)

    args = types.SimpleNamespace(
        directory=str(home / "proj"), provider="demo", port=0, browser=True,
    )
    cli_app._app_cmd(args)  # 不抛即通过（_stop_backend(None) 直接返回）
    assert order == ["log", "backend"], "先接日志再起服务，启动期日志也要留痕"
