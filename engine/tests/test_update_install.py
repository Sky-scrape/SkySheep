"""应用内一键更新：源码版拒绝下载/安装；未下载时不能直接应用。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from test_server import make_client, recv_until  # home fixture 在 conftest.py

import skysheep.server.app as server_app
from skysheep.server.app import LOCAL_ONLY_METHODS


def test_install_update_refused_on_source_build(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u1", "method": "app.install_update", "params": {}})
        r = recv_until(ws, "u1")
        assert not r["ok"] and "源码版" in r["error"]


def test_apply_update_requires_downloaded_package(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u2", "method": "app.apply_update", "params": {}})
        r = recv_until(ws, "u2")
        assert not r["ok"] and "下载" in r["error"]


def test_check_update_reports_frozen_flag(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "u3", "method": "app.check_update", "params": {}})
        r = recv_until(ws, "u3")
        # 网络可达与否都行：字段必须在（TestClient 进程永远非 frozen）
        assert r["result"].get("frozen") is False


def test_update_methods_are_local_only(home, monkeypatch):
    """M1：持令牌的局域网设备不得远程触发「下载安装包 + 静默安装 + 强退」。

    只读的 app.check_update 不在门禁表里：它不拉起任何本机进程。
    """
    assert {"app.install_update", "app.apply_update"} <= LOCAL_ONLY_METHODS
    assert "app.check_update" not in LOCAL_ONLY_METHODS

    monkeypatch.setattr(server_app, "_client_is_local", lambda client: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i, method in enumerate(("app.install_update", "app.apply_update")):
            ws.send_json({"id": f"r{i}", "method": method, "params": {}})
            r = recv_until(ws, f"r{i}")
            assert r["ok"] is False, method
            assert "本机" in r["error"], method


def test_release_tag_is_sanitized_before_reaching_paths():
    """M8：tag 会拼进本地文件路径与 `cmd /c` 命令行，必须先过白名单。

    更新源被控（或 DNS 劫持）时，带 `"` `&` 的 tag 能借 helper 命令注入，
    带 `/` `\\` `..` 的 tag 能把安装包写到 %TEMP% 之外。
    """
    from skysheep.core.uptodate import RELEASES_LATEST, is_safe_tag, release_from_redirect

    for bad in (
        'v1.0"&calc.exe&"',       # 引号闭合 + 命令拼接
        "v1.0/../../evil",        # 路径穿越
        "v1.0\\..\\evil",          # Windows 分隔符
        "v1.0%22",                # URL 编码的引号（unquote 后才露馅）
        "v1.0 `whoami`",          # 空格 + 反引号
        "",                       # 空
        "v" + "0" * 80,           # 过长
    ):
        assert not is_safe_tag(bad), bad
        with pytest.raises(RuntimeError):
            release_from_redirect(
                RELEASES_LATEST, f"/Sky-scrape/SkySheep/releases/tag/{bad}")

    # 正常发布 tag 不受影响（含预发布后缀与 build 元数据）
    for good in ("v1.7", "v0.6.0-beta1", "v1.7.0+build.2"):
        assert is_safe_tag(good), good
    info = release_from_redirect(RELEASES_LATEST, "/Sky-scrape/SkySheep/releases/tag/v1.7")
    assert info["setup_url"].endswith("/v1.7/SkySheep-1.7-setup.exe")


def test_write_update_helper_batch_content(tmp_path):
    """更新助手批处理：ping 延迟（DETACHED 下 timeout 会立即失败，互斥体来不及
    释放）、/RESTARTAPP 让安装器装完自动拉起新版、/LOG 与退出码回写落盘。

    回归背景：此前是拼命令串交给 `cmd /c`（Popen 传列表），Python 会把引号转义成
    `\\"` 而 cmd.exe 不认——安装包路径与重定向整段解析失败、安装器根本起不来
    （日志只剩一行头部，表现为「更新点了没下文」）。现在落成 .cmd 文件执行，
    这里锁住「引号按 cmd 规则原样出现」与各参数形状。
    """
    from skysheep.server.backend import _write_update_helper

    pending = str(tmp_path / "SkySheep-1.9-setup.exe")
    (tmp_path / "SkySheep-1.9-setup.exe").write_bytes(b"stub")
    log = tmp_path / "update-setup.log"
    log.write_text("[head]\n", encoding="utf-8")

    script = _write_update_helper(pending, log, "/CURRENTUSER")
    assert script is not None and script.name == "apply-update.cmd"
    text = script.read_text(encoding="gbk")
    assert "\\\"" not in text, "批处理里不能出现被转义的引号（cmd 不认）"
    assert text.index("ping -n 3 127.0.0.1 >nul") < text.index(f'"{pending}"')
    assert f'"{pending}" /SILENT /CLOSEAPPLICATIONS /RESTARTAPP /CURRENTUSER' in text
    assert f'/LOG="{log}"' in text          # 传给安装器：Inno 自己记安装过程
    assert f'>>"{log}" echo [setup 退出码: %errorlevel%]' in text  # 装完回写退出码
    assert "%errorlevel%" in text
    assert "if not exist" in text  # 安装包缺失时的显式失败行

    # 无日志（建不出来）的退化形态：命令仍完整，只是不落日志
    bare = _write_update_helper(pending, None, "")
    bare_text = bare.read_text(encoding="gbk")
    assert f'"{pending}" /SILENT /CLOSEAPPLICATIONS /RESTARTAPP' in bare_text
    assert "/LOG" not in bare_text and "/CURRENTUSER" not in bare_text
    assert "errorlevel" not in bare_text


@pytest.mark.skipif(sys.platform != "win32", reason="批处理执行只在 Windows 成立")
def test_update_helper_batch_actually_runs_and_logs_exit_code(tmp_path):
    """真跑一遍批处理（用 exit /b 7 的替身代替安装包）：退出码必须落进日志。

    这一条是上面那个 bug 的正面回归——旧实现里批处理/命令串"看起来对"，
    但执行链断了；只有真的执行一次才能证明它通。

    断言只锁 ASCII 骨架（方括号、退出码数字、安装包文件名），不拿「退出码」
    这类汉字当锚点：.cmd 里的中文按 GBK 落盘，而 cmd 按**本机**代码页解读
    （中文 Windows 是 cp936，GitHub 英文 CI 是 cp437/1252），同一份脚本在
    非 GBK 机器上 echo 进日志的中文必然是另一串字符——拿汉字断言就等于
    「测试只在中文系统上能过」（CI #61 即栽在这里）。
    """
    import subprocess

    from skysheep.server.backend import _write_update_helper

    stub = tmp_path / "fake-setup.cmd"
    stub.write_text("@echo off\r\nexit /b 7\r\n", encoding="gbk")
    log = tmp_path / "update-setup.log"

    script = _write_update_helper(str(stub), log, "")
    assert script is not None
    proc = subprocess.run(["cmd", "/c", script.name], cwd=str(tmp_path),
                          capture_output=True, timeout=60)
    assert proc.returncode == 0, proc
    content = log.read_text(encoding="gbk", errors="replace")
    lines = [ln for ln in content.splitlines() if ln.strip()]
    assert len(lines) == 2, content          # 头部时间行 + 退出码行，一行不多
    assert lines[0].startswith("["), content  # [%DATE% %TIME%] …
    # 安装器替身的退出码 7 必须落进日志：这正是旧实现断掉、也最需要证据的一环
    assert lines[-1].startswith("[setup ") and lines[-1].rstrip().endswith("7]"), content

    # 守卫分支也要真的拦：安装包不存在 → 整个批处理退出码 1，路径（ASCII）落日志
    missing = _write_update_helper(str(tmp_path / "nope-setup.exe"), log, "")
    proc2 = subprocess.run(["cmd", "/c", missing.name], cwd=str(tmp_path),
                           capture_output=True, timeout=60)
    assert proc2.returncode == 1, proc2
    assert "nope-setup.exe" in log.read_text(encoding="gbk", errors="replace")


def test_install_update_result_reports_uac_need():
    """install_update 的结果要带 uac 标记：本机是「所有用户」安装时，
    前端必须在应用退出前就把「留意授权窗口」讲清楚（退出后提示就看不见了）。"""
    from skysheep.server.app import STATIC_DIR

    backend_src = (Path(__file__).resolve().parents[1] / "src" / "skysheep"
                   / "server" / "backend.py").read_text(encoding="utf-8")
    assert '"uac": _setup_privilege_override() != "/CURRENTUSER"' in backend_src
    js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "r.uac" in js and "用户账户控制" in js
    assert 'request("app.notify", {' in js  # 退出前的系统通知提醒


class _FakeWinreg:
    """够 _setup_privilege_override 用的 winreg 桩：按侧别决定键是否存在。"""

    HKEY_CURRENT_USER = "hkcu"
    HKEY_LOCAL_MACHINE = "hklm"

    def __init__(self, hkcu_has: bool):
        self.hkcu_has = hkcu_has

    def OpenKey(self, root, key):
        if root == self.HKEY_CURRENT_USER and self.hkcu_has:
            return _OpenKeyOk()
        raise OSError("not found")


class _OpenKeyOk:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_setup_privilege_override_follows_install_side(monkeypatch):
    """安装登记在 HKCU（per-user，装在用户目录）→ 传 /CURRENTUSER 免 UAC；
    HKLM（为所有用户安装，必须提权）或无登记 → 不传参数，维持默认行为。"""
    import sys

    from skysheep.server.backend import _setup_privilege_override

    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(hkcu_has=True))
    assert _setup_privilege_override() == "/CURRENTUSER"
    monkeypatch.setitem(sys.modules, "winreg", _FakeWinreg(hkcu_has=False))
    assert _setup_privilege_override() == ""
