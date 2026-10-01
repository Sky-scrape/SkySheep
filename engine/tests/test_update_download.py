"""更新链路真实下载/校验路径（_download_setup / _fetch_setup_sha256 / install_update）。

回归 2026-10 审查项 10：此前更新用例只盖源码版拒绝 / helper 批处理 / UAC 判定
等边缘，下载主体的三道防线（体积上限、MZ 头、sha256 比对）零执行——而
docs/发布清单.md 自述这条链路「1.7 引入、1.9 才修好」。这里用 httpx
MockTransport 打真实函数（web_fetch 硬化测试同款手法），不打真实网络。
"""

from __future__ import annotations

import hashlib
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
    """给 remote 函数内部自建的 httpx.Client 注入 MockTransport（其余参数原样）。"""
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", factory)


def _backend(tmp_path):
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
    with pytest.raises(RuntimeError, match="上限"):
        remote_mod._download_setup(SETUP_URL, dest, "")
    assert not dest.exists()
    assert not (tmp_path / "setup.exe.part").exists()


def test_download_setup_rejects_non_pe_header(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not an exe</html>")

    _mock_httpx(monkeypatch, handler)
    dest = tmp_path / "setup.exe"
    with pytest.raises(RuntimeError, match="头部校验失败"):
        remote_mod._download_setup(SETUP_URL, dest, "")  # 无 .sha256 时 MZ 头兜底
    assert not dest.exists()


def test_download_setup_propagates_404_as_status_error(tmp_path, monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    _mock_httpx(monkeypatch, handler)
    with pytest.raises(httpx.HTTPStatusError):
        remote_mod._download_setup(SETUP_URL, tmp_path / "setup.exe", "")
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

    async def fake_release():
        return {"version": version, "setup_url": SETUP_URL, "notes": ""}

    monkeypatch.setattr(remote_mod, "check_latest_release", fake_release)
    monkeypatch.setattr(remote_mod, "_update_dir", lambda: tmp_path)  # 不碰真实 %TEMP%
    be = _backend(tmp_path)
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


async def test_install_update_without_checksum_reports_unverified(tmp_path, monkeypatch):
    payload = _payload()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith(".sha256"):
            return httpx.Response(404)  # 发布方没传校验文件
        return httpx.Response(200, content=payload)

    be = await _setup_frozen_backend(tmp_path, monkeypatch, handler)
    r = await be.install_update()
    assert r["update_available"] is True and r["verified"] is False
    assert Path(r["path"]).read_bytes() == payload  # MZ 头 + 体积上限两道兜底后放行
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
