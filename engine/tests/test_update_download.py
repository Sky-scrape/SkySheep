"""更新链路真实下载/校验路径（_download_setup / _fetch_setup_sha256 / install_update）。

回归 2026-10 审查项 10：此前更新用例只盖源码版拒绝 / helper 批处理 / UAC 判定
等边缘，下载主体的三道防线（体积上限、MZ 头、sha256 比对）零执行——而
docs/发布清单.md 自述这条链路「1.7 引入、1.9 才修好」。这里用 httpx
MockTransport 打真实函数（web_fetch 硬化测试同款手法），不打真实网络。

SEC-H01（2026-10 全面测试与灰度评估报告）：本文件是全套件里唯一一处
「无 SKYSHEEP_HOME 隔离就构造 ServerBackend 并 setup()」的用例组——会读写
开发者真实 ~/.skysheep（库、哨兵、建目录）并按真实 mcp.json 拉起 MCP 子进程。
修复：_backend 构造前显式设隔离；文末另有 SEC-M20 隔离回归（根级 autouse
夹具的默认隔离，特意不挂 home 夹具验证）。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import httpx
import pytest
from conftest import FakeProvider

from skysheep.messages import TextBlock
from skysheep.server.backend_parts import remote as remote_mod

SETUP_URL = (
    "https://github.com/Sky-scrape/SkySheep/releases/download/v9.9.9/"
    "SkySheep-9.9.9-setup.exe"
)


def _payload(size: int = 64) -> bytes:
    """像样的 PE 形状载荷（MZ 头 + 填充）。"""
    return b"MZ" + b"\x00" * (size - 2)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _mock_httpx(monkeypatch, handler):
    """给 remote 函数内部自建的 httpx.Client 注入 MockTransport（其余参数原样）。

    transport 与 proxy 都剥掉：MockTransport 接管全部出网，真实回退候选在此
    无意义（回退链行为由显式替换 _egress_fallbacks 的用例覆盖）。
    """
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        kwargs.pop("proxy", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _backend(tmp_path, monkeypatch):
    """构造 ServerBackend：构造前显式设 SKYSHEEP_HOME（SEC-H01 的修复锚）。

    ServerBackend.__init__ 当场解析 skysheep_home()（subagents.json /
    teams.json / team_active.json），setup() 再连会话库、读 mcp.json——
    不隔离就会读写开发者真实 ~/.skysheep。根级 autouse 夹具（engine/
    conftest.py）虽已兜底，这里仍显式设一遍：本文件的用例自证隔离，
    不依赖夹具的加载顺序。
    """
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    from skysheep.server.backend import ServerBackend

    return ServerBackend(
        working_dir=tmp_path / "proj",
        provider_factory=lambda: FakeProvider([[TextBlock(text="好")]]),
    )


# ---- _download_setup：三道防线 + 404 ----


def test_download_setup_success_writes_and_verifies(tmp_path, monkeypatch):
    payload = _payload()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=payload)

    _mock_httpx(monkeypatch, handler)
    dest = tmp_path / "setup.exe"
    remote_mod._download_setup(SETUP_URL, dest, _sha(payload))
    assert dest.read_bytes() == payload
    assert not dest.with_suffix(".exe.part").exists(), "暂存文件用完必须消失"
    assert seen == [SETUP_URL]


def test_download_setup_rejects_sha256_mismatch(tmp_path, monkeypatch):
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    _mock_httpx(monkeypatch, handler)
    dest = tmp_path / "setup.exe"
    with pytest.raises(RuntimeError, match="校验和不匹配"):
        remote_mod._download_setup(SETUP_URL, dest, "0" * 64)
    assert not dest.exists(), "被篡改的包不得落正式名"
    assert not (tmp_path / "setup.exe.part").exists(), "半截文件不残留"


def test_download_setup_enforces_size_cap(tmp_path, monkeypatch):
    payload = _payload(1 << 20)  # 1MB，超过收紧后的上限

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    _mock_httpx(monkeypatch, handler)
    monkeypatch.setattr(remote_mod, "MAX_SETUP_BYTES", 1024)
    dest = tmp_path / "setup.exe"
    # allow_unverified：本用例只验体积防线，绕过「缺 checksum 即拒装」的前置门
    with pytest.raises(RuntimeError, match="上限"):
        remote_mod._download_setup(SETUP_URL, dest, "", allow_unverified=True)
    assert not dest.exists()
    assert not (tmp_path / "setup.exe.part").exists()


def test_download_setup_rejects_non_pe_header(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not an exe</html>")

    _mock_httpx(monkeypatch, handler)
    dest = tmp_path / "setup.exe"
    with pytest.raises(RuntimeError, match="头部校验失败"):
        # 显式放行未校验（只验 MZ 头兜底这一层）
        remote_mod._download_setup(SETUP_URL, dest, "", allow_unverified=True)
    assert not dest.exists()


def test_download_setup_propagates_404_as_status_error(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        remote_mod._download_setup(SETUP_URL, tmp_path / "setup.exe", "", allow_unverified=True)
    # 404 的「人话」包装在 install_update 层（下面有端到端用例）


# ---- _fetch_setup_sha256 ----


def test_fetch_sha256_parses_hex_and_filename_forms(monkeypatch):
    hexdigest = "a" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SETUP_URL + ".sha256"
        return httpx.Response(200, text=f"{hexdigest}  SkySheep-9.9.9-setup.exe")

    _mock_httpx(monkeypatch, handler)
    assert remote_mod._fetch_setup_sha256(SETUP_URL) == hexdigest


def test_fetch_sha256_empty_on_404_garbage_and_network_error(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _mock_httpx(monkeypatch, handler)
    assert remote_mod._fetch_setup_sha256(SETUP_URL) == ""

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-a-hash")

    _mock_httpx(monkeypatch, garbage)
    assert remote_mod._fetch_setup_sha256(SETUP_URL) == ""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no net")

    _mock_httpx(monkeypatch, boom)
    assert remote_mod._fetch_setup_sha256(SETUP_URL) == "", "网络错误不阻断更新（有则必校）"


# ---- install_update 端到端（真实下载函数 + MockTransport） ----


async def _setup_frozen_backend(tmp_path, monkeypatch, handler, version="9.9.9"):
    _mock_httpx(monkeypatch, handler)

    async def fake_release(channel: str = "stable"):  # 新签名：渠道由 remote 层传入
        return {"version": version, "setup_url": SETUP_URL, "notes": ""}

    monkeypatch.setattr(remote_mod, "check_latest_release", fake_release)
    monkeypatch.setattr(remote_mod, "_update_dir", lambda: tmp_path)  # 不碰真实 %TEMP%
    be = _backend(tmp_path, monkeypatch)
    await be.setup()
    be._is_frozen = True  # 测试进程永远非 frozen：显式按安装版驱动
    return be


async def test_install_update_downloads_verifies_and_pends(tmp_path, monkeypatch):
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == SETUP_URL + ".sha256":
            return httpx.Response(200, text=_sha(payload))
        return httpx.Response(200, content=payload)

    be = await _setup_frozen_backend(tmp_path, monkeypatch, handler)
    r = await be.install_update()
    assert r["update_available"] is True and r["version"] == "9.9.9"
    assert r["verified"] is True, "核对过 .sha256 必须如实上报"
    assert Path(r["path"]).read_bytes() == payload
    assert be._pending_update == r["path"]
    await be.shutdown()


async def test_install_update_without_checksum_refuses_to_install(tmp_path, monkeypatch):
    """发布方未提供 .sha256：默认拒绝自动安装（fail closed），引导手动下载核对。

    旧行为是「有则必校、无则只靠 MZ 头 + 体积上限放行」——那两道只能防半截
    文件，不能证明内容未被替换。现在缺校验和直接拒绝，不再降级安装。
    """
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".sha256"):
            return httpx.Response(404)  # 发布方没传校验文件
        return httpx.Response(200, content=payload)

    be = await _setup_frozen_backend(tmp_path, monkeypatch, handler)
    with pytest.raises(RuntimeError, match="校验和"):
        await be.install_update()
    assert not be._pending_update, "缺校验和时不得留下待安装包"
    await be.shutdown()


async def test_install_update_missing_attachment_asks_manual_download(
    tmp_path, monkeypatch
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)  # 安装包附件真缺

    be = await _setup_frozen_backend(tmp_path, monkeypatch, handler)
    with pytest.raises(RuntimeError, match="手动下载"):
        await be.install_update()
    assert not be._pending_update
    await be.shutdown()


async def test_install_update_no_newer_version_skips_download(tmp_path, monkeypatch):
    from skysheep import __version__

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_payload())

    be = await _setup_frozen_backend(tmp_path, monkeypatch, handler, version=__version__)
    r = await be.install_update()
    assert r == {
        "update_available": False, "current": __version__, "latest": __version__,
    }
    assert calls == [], "已是最新版就不该发起任何下载"
    await be.shutdown()


# ---- 出网回退：直连失败 → 环境变量代理 / Windows 系统代理（见 core/uptodate.py）----


def test_download_setup_falls_back_after_connect_failure(tmp_path, monkeypatch):
    """只有系统代理一条路的机器：直连 ConnectTimeout 后经候选路径重试成功。"""
    calls = {"n": 0}
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("直连超时", request=request)
        return httpx.Response(200, content=payload)

    _mock_httpx(monkeypatch, handler)
    monkeypatch.setattr(remote_mod, "_egress_fallbacks",
                        lambda: [httpx.Proxy("http://127.0.0.1:9")])
    dest = tmp_path / "setup.exe"
    remote_mod._download_setup_with_fallback(SETUP_URL, dest, _sha(payload))
    assert dest.read_bytes() == payload
    assert calls["n"] == 2
    assert not (tmp_path / "setup.exe.part").exists(), "暂存文件不残留"


def test_download_setup_fallback_exhausted_reraises_last_connect_error(tmp_path, monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("全部超时", request=request)

    _mock_httpx(monkeypatch, handler)
    monkeypatch.setattr(remote_mod, "_egress_fallbacks",
                        lambda: [httpx.Proxy("http://127.0.0.1:9")])
    with pytest.raises(httpx.ConnectTimeout):
        remote_mod._download_setup_with_fallback(SETUP_URL, tmp_path / "setup.exe", _sha(_payload()))
    assert calls["n"] == 2, "直连 + 候选各试一次"
    assert not (tmp_path / "setup.exe.part").exists()


def test_fetch_setup_sha256_falls_back_after_connect_failure(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectTimeout("直连超时", request=request)
        return httpx.Response(200, text=f"{'a' * 64}  SkySheep-2.7.0-setup.exe")

    _mock_httpx(monkeypatch, handler)
    monkeypatch.setattr(remote_mod, "_egress_fallbacks",
                        lambda: [httpx.Proxy("http://127.0.0.1:9")])
    sha = remote_mod._fetch_setup_sha256("https://example.com/SkySheep-2.7.0-setup.exe")
    assert sha == "a" * 64
    assert calls["n"] == 2


# ---- SEC-M20 隔离回归：根级 autouse 夹具（以下用例特意不挂 home 夹具） ----


def test_skysheep_home_isolated_without_home_fixture(tmp_path):
    """不挂 home 夹具：根级 autouse 夹具也必须把 SKYSHEEP_HOME 指到 tmp_path/home。

    home 夹具是 opt-in 的，SEC-M20 的修复是把默认隔离上提到 engine/conftest.py
    （tests/ 与 evals/ 两棵测试树共同的根）——漏挂夹具的测试不再绑到开发者
    真实 ~/.skysheep。config.skysheep_home 与 instance.data_home 同源，一并断言。
    """
    from skysheep.config import skysheep_home
    from skysheep.instance import data_home

    assert Path(os.environ["SKYSHEEP_HOME"]) == tmp_path / "home"
    assert skysheep_home() == data_home() == tmp_path / "home"
    assert skysheep_home() != Path.home() / ".skysheep"


async def test_backend_construction_and_setup_stay_in_isolated_home(tmp_path, monkeypatch):
    """Backend 构造与 setup 的全部主目录落点都在临时 home（H01 复现面的反面）。

    H01 的复现产物是：真实 ~/.skysheep 出现 skysheep.db、fast-talk/、
    remote-control/、snippets 哨兵，并按真实 mcp.json 拉起 MCP 子进程。
    本用例不挂 home 夹具、走完整 setup()，同样的产物必须全部落在临时
    home——临时 home 里没有 mcp.json，也就不会有任何 MCP 子进程。
    """
    from skysheep.config import db_path

    async def fake_release(channel: str = "stable"):  # 拦住后台更新检查，不发真实网络
        return {"version": "9.9.9", "setup_url": SETUP_URL, "notes": ""}

    monkeypatch.setattr(remote_mod, "check_latest_release", fake_release)
    home = Path(os.environ["SKYSHEEP_HOME"])
    assert home == tmp_path / "home", "根级 autouse 夹具必须已就位"
    (tmp_path / "proj").mkdir()
    be = _backend(tmp_path, monkeypatch)
    try:
        # __init__ 期就地解析的三处落点（H01 的根因面）
        assert be.subagent_store.path == home / "subagents.json"
        assert be.team_templates.path == home / "teams.json"
        assert be._team_state_path == home / "team_active.json"
        assert db_path() == home / "skysheep.db"
        await be.setup()
        # setup 的写路径：会话库与两个固定工作目录全部落在临时 home 内
        assert (home / "skysheep.db").exists()
        assert (home / "fast-talk").is_dir()
        assert (home / "remote-control").is_dir()
    finally:
        await be.shutdown()
