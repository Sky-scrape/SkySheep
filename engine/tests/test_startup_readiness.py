"""启动就绪判定：端口可连 ≠ 引擎可用。

背景（2026-11-04）：安装版撞上「库版本 11 > 当前程序支持 8」，uvicorn 先绑
端口再跑 lifespan，旧实现只探端口，于是启动失败被当成启动成功——桌面壳带着
一个已经退出的后端去加载页面，窗口永远停在「正在启动 · 引擎准备中」，连报错
都不给。这里守住四条：

  1. ``server.started``（lifespan 跑完才置位）才是就绪信号，端口开着不算；
  2. lifespan 失败（服务线程退出）立刻判失败，不干等满 60 秒；
  3. 失败原因要从 uvicorn 的 ERROR 日志里捞回来，交给启动失败页显示；
  4. ``_start_backend`` 抛出的 SystemExit 必须带上那句真原因。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from skysheep.cli.app import _start_backend, _StartupLogCatcher, _wait_ready


class _FakeServer:
    """够用的 uvicorn.Server 替身：只看 started 与 uvicorn_thread。"""

    def __init__(self, started: bool, thread: threading.Thread | None = None) -> None:
        self.started = started
        self.uvicorn_thread = thread


def _dead_thread() -> threading.Thread:
    t = threading.Thread(target=lambda: None)
    t.start()
    t.join()
    return t


def test_wait_ready_true_only_after_started() -> None:
    """started 置位即就绪，不依赖端口探测。"""
    assert _wait_ready(1, _FakeServer(started=True), timeout_s=5) is True


def test_wait_ready_fails_fast_when_thread_died() -> None:
    """lifespan 抛异常 → uvicorn 在服务线程里 sys.exit；必须立刻判失败。"""
    server = _FakeServer(started=False, thread=_dead_thread())
    started_at = time.monotonic()
    assert _wait_ready(1, server, timeout_s=30) is False
    assert time.monotonic() - started_at < 2, "线程已经退出还等满了超时预算"


def test_wait_ready_times_out_without_started() -> None:
    """线程还活着但 lifespan 一直没跑完：给足预算后判超时（慢≠死）。"""
    t = threading.Thread(target=lambda: time.sleep(5))
    t.start()
    try:
        assert _wait_ready(1, _FakeServer(started=False, thread=t), timeout_s=0.5) is False
    finally:
        t.join(timeout=5)


def test_startup_log_catcher_keeps_real_reason() -> None:
    """带 exc_info 的记录：留异常消息，不被 uvicorn 收尾那句空话覆盖。"""
    catcher = _StartupLogCatcher()
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(catcher)
    try:
        try:
            raise RuntimeError("会话数据库由更新版本的 SkySheep 创建（库版本 11 > 当前程序支持 8）")
        except RuntimeError:
            logger.error("Exception in 'lifespan' protocol.", exc_info=True)
        logger.error("Application startup failed. Exiting.")
    finally:
        logger.removeHandler(catcher)
    assert "库版本 11" in catcher.detail
    assert "Application startup failed" not in catcher.detail


def test_startup_log_catcher_compacts_traceback_text() -> None:
    """纯文本 traceback（log_level="warning" 实测形态）：只留最后一行真原因。"""
    catcher = _StartupLogCatcher()
    logger = logging.getLogger("uvicorn.error")
    logger.addHandler(catcher)
    try:
        logger.error(
            "Traceback (most recent call last):\n"
            '  File "starlette\\routing.py", line 648, in lifespan\n'
            "RuntimeError: 会话数据库由更新版本的 SkySheep 创建（库版本 11 > 当前程序支持 8）"
        )
        logger.error("Application startup failed. Exiting.")
    finally:
        logger.removeHandler(catcher)
    assert catcher.detail.startswith("RuntimeError: 会话数据库")
    assert "Traceback" not in catcher.detail


@pytest.mark.e2e
def test_wait_ready_ignores_open_port_before_lifespan() -> None:
    """回归：端口已经能连、但 lifespan 没跑完，不算就绪。

    旧实现只看端口。端口通不代表 ``backend.setup()`` 成功，失败就和「启动很慢」
    混在一起，只能等满 60 秒超时——用户看到的就是一直转圈的启动页。
    """
    import socket

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(1)
    t = threading.Thread(target=lambda: time.sleep(5))
    t.start()
    try:
        started_at = time.monotonic()
        assert _wait_ready(port, _FakeServer(started=False, thread=t), timeout_s=0.8) is False
        assert time.monotonic() - started_at < 3
    finally:
        srv.close()
        t.join(timeout=5)


@pytest.mark.e2e
@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_start_backend_reports_lifespan_failure(home, monkeypatch) -> None:
    """真实 uvicorn：lifespan 失败要立刻抛 SystemExit，消息里带原始原因。

    uvicorn 的失败路径是服务线程里 ``sys.exit(3)``，线程以 SystemExit 结束——
    threading 本身会静默吞掉它（生产上无噪音），pytest 会把「线程里冒出的
    SystemExit」记成 PytestUnhandledThreadExceptionWarning，这里就地忽略。
    """
    from fastapi import FastAPI

    import skysheep.server as server_pkg

    @asynccontextmanager
    async def _boom(_app):
        raise RuntimeError("测试用启动失败：库版本 99 > 当前程序支持 8")
        yield  # pragma: no cover - 到不了，只为让它是生成器

    def _fake_create_app(**_kwargs):
        app = FastAPI(lifespan=_boom)
        app.state.backend = SimpleNamespace()  # _start_backend 会挂 request_shutdown
        return app

    monkeypatch.setattr(server_pkg, "create_app", _fake_create_app)
    args = SimpleNamespace(directory=str(home), provider=None, port=0)
    started_at = time.monotonic()
    with pytest.raises(SystemExit) as ei:
        _start_backend(args)
    elapsed = time.monotonic() - started_at
    message = str(ei.value)
    assert "服务启动失败" in message
    assert "库版本 99" in message, f"失败页看不到真原因：{message}"
    assert elapsed < 20, f"等了 {elapsed:.1f}s，失败应立刻上报"
