"""更新可信度链路：渠道（stable/beta）、semver 预发布比较、更新前备份与失败对账。

回归 2026-10「代码签名与更新可信度」落地：beta 渠道收预发布、beta 用户能收到
转正推送、更新前有数据备份、更新失败下次启动亮现场。更新源相关网络路径全部
用 httpx MockTransport 打真实函数（与 test_update_download 同款手法）。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from skysheep import __version__
from skysheep.core.uptodate import (
    RELEASES_FEED,
    is_newer_version,
    release_from_feed,
)
from skysheep.server.backend_parts import remote as remote_mod

_FEED_URL = RELEASES_FEED


# ---- semver 预发布比较：正式版 > 预发布，预发布之间逐标识符比 ----


def test_is_newer_version_final_beats_prerelease():
    """beta 渠道核心回归：装了 beta1 的用户要能收到 2.7.0 正式版推送。"""
    assert is_newer_version("2.7.0", "v2.7.0-beta1")
    assert is_newer_version("2.7.0", "2.7.0-beta.2")
    assert not is_newer_version("v2.7.0-beta1", "2.7.0")


def test_is_newer_version_prerelease_ordering():
    assert is_newer_version("2.7.0-beta.2", "2.7.0-beta.1")
    assert is_newer_version("2.7.0-beta.10", "2.7.0-beta.9"), "后缀数字按数值比"
    assert is_newer_version("2.7.0-rc.1", "2.7.0-beta.3"), "字母标识符按字典序"
    assert is_newer_version("2.7.0-beta.1.1", "2.7.0-beta.1"), "标识符多者新"
    assert not is_newer_version("2.7.0-beta.1", "2.7.0-beta.1")


def test_is_newer_version_plain_versions_unchanged():
    """无后缀的常规比较不能被改坏（test_new_features 的原有断言在这里再盖一遍）。"""
    assert is_newer_version("v0.7.0", "0.6.0")
    assert is_newer_version("0.10.0", "0.9.9")
    assert not is_newer_version("0.6.0", "0.6.0")
    assert not is_newer_version("", "0.6.0")
    assert not is_newer_version("0.5.9", "0.6.0")


# ---- beta 渠道：releases.atom 第一条（含预发布）----


_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xml:base="https://github.com/Sky-scrape/SkySheep/releases">
  <link href="/Sky-scrape/SkySheep/releases" rel="alternate" type="text/html"/>
  <entry>
    <id>tag:github.com,2008:Repository/1/v2.7.0-beta.1</id>
    <updated>2026-10-10T10:00:00Z</updated>
    <link rel="alternate" type="text/html" href="/Sky-scrape/SkySheep/releases/tag/v2.7.0-beta.1"/>
    <title>SkySheep v2.7.0-beta.1</title>
  </entry>
  <entry>
    <id>tag:github.com,2008:Repository/1/v2.6.0</id>
    <link rel="alternate" type="text/html" href="/Sky-scrape/SkySheep/releases/tag/v2.6.0"/>
  </entry>
</feed>
"""


def test_release_from_feed_takes_first_entry():
    info = release_from_feed(_ATOM, _FEED_URL)
    assert info["version"] == "2.7.0-beta.1"
    assert info["tag"] == "v2.7.0-beta.1"
    assert info["setup_url"].endswith("/v2.7.0-beta.1/SkySheep-2.7.0-beta.1-setup.exe")
    assert info["notes"] == ""


def test_release_from_feed_empty_and_unsafe_tag_rejected():
    with pytest.raises(RuntimeError, match="没有版本号"):
        release_from_feed("<feed><entry><title>nothing</title></entry></feed>", _FEED_URL)
    # 注入形态：引号前的合法段会被提取（防注入的正确姿态），但带 / 的整段
    # 穿过 feed 正则后被 is_safe_tag 拒收——宁可当作「更新源异常」也不进路径
    bad = '<link href="/Sky-scrape/SkySheep/releases/tag/v2.7.0/../../evil"/>'
    with pytest.raises(RuntimeError, match="非法字符"):
        release_from_feed(bad, _FEED_URL)
    # 引号截断：带属性的注入只提取到合法 tag，不报错也不带脏字符
    ok = '<link href="/Sky-scrape/SkySheep/releases/tag/v2.7.0" onerror="x"/>'
    assert release_from_feed(ok, _FEED_URL)["tag"] == "v2.7.0"


def test_fetch_latest_release_beta_uses_atom(monkeypatch):
    from skysheep.core.uptodate import fetch_latest_release

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=_ATOM)

    info = asyncio.run(
        fetch_latest_release(transport=httpx.MockTransport(handler), channel="beta")
    )
    assert seen == [_FEED_URL], "beta 渠道必须打 atom 订阅，不能走 api.github.com"
    assert info["version"] == "2.7.0-beta.1"


def test_fetch_latest_release_unknown_channel_falls_back_to_stable(monkeypatch):
    from skysheep.core.uptodate import fetch_latest_release

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "/Sky-scrape/SkySheep/releases/tag/v2.6.0"})

    info = asyncio.run(
        fetch_latest_release(transport=httpx.MockTransport(handler), channel="nonsense")
    )
    assert seen == ["https://github.com/Sky-scrape/SkySheep/releases/latest"]
    assert info["version"] == "2.6.0"


# ---- 更新前备份与 marker（SKYSHEEP_HOME 隔离）----


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE t (x TEXT)")
        conn.execute("INSERT INTO t VALUES ('你好')")
        conn.commit()
    finally:
        conn.close()


def test_backup_user_data_copies_files_and_db(home):
    home_dir = Path(home) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text('default = "deepseek"\n', encoding="utf-8")
    _make_db(home_dir / "skysheep.db")

    bdir, failures = remote_mod._backup_user_data()

    assert failures == [], "全部数据备份成功时不应有失败项"
    assert bdir is not None and bdir.is_dir()
    assert bdir.parent == home_dir / "backups"
    assert bdir.name.startswith("preupdate-")
    assert (bdir / "config.toml").read_text(encoding="utf-8").startswith("default")
    check = sqlite3.connect(bdir / "skysheep.db")
    try:
        assert check.execute("SELECT x FROM t").fetchone() == ("你好",)
    finally:
        check.close()


def test_backup_user_data_prunes_old_dirs(home):
    import os

    home_dir = Path(home) / "home"
    backups = home_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    old_ts = 1_700_000_000  # 固定过去时刻：新建的目录必须严格更新，修剪才有确定性
    for i in range(5):
        d = backups / f"preupdate-2.5.0-2026010{i}-000000"
        d.mkdir()
        (d / "keep.txt").write_text(str(i), encoding="utf-8")
        os.utime(d, (old_ts + i, old_ts + i))

    remote_mod._backup_user_data()

    names = [p.name for p in backups.glob("preupdate-*")]
    # 滚动保留按「总数」计：本次新建的 1 份 + 旧 5 份裁到只剩最新 2 份 = 共 3 份
    assert len(names) == remote_mod.PREUPDATE_BACKUP_KEEP, "滚动保留份数要生效"
    assert any(n.startswith("preupdate-2.5.0-") is False or "20261011" in n for n in names)
    assert "preupdate-2.5.0-20260104-000000" in names
    assert "preupdate-2.5.0-20260103-000000" in names
    assert "preupdate-2.5.0-20260102-000000" not in names
    assert "preupdate-2.5.0-20260101-000000" not in names
    assert "preupdate-2.5.0-20260100-000000" not in names


# ---- marker：写入 → 启动对账（成功清标记 / 失败亮现场）----


class _Holder:
    """只挂更新对账/执行相关的最小宿主：不走完整 ServerBackend 构建流程。"""

    consume_update_marker = remote_mod.RemoteMixin.consume_update_marker
    dismiss_update_failure = remote_mod.RemoteMixin.dismiss_update_failure
    apply_update = remote_mod.RemoteMixin.apply_update

    def __init__(self) -> None:
        self.update_success = None
        self.update_failure = None
        self._pending_update = None


def test_marker_roundtrip_success_clears_and_records(home):
    home_dir = Path(home) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    assert remote_mod._write_update_marker("C:\\tmp\\SkySheep-9.9.9-setup.exe", __version__, None)

    be = _Holder()
    be.consume_update_marker()

    assert be.update_failure is None
    assert be.update_success == {"from": __version__, "to": __version__}
    assert not remote_mod._update_marker_path().exists(), "成功对账要清掉标记"


def test_marker_mismatch_surfaces_failure_and_dismiss(home):
    home_dir = Path(home) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    backup = home_dir / "backups" / "preupdate-x"
    remote_mod._write_update_marker("C:\\tmp\\SkySheep-9.9.9-setup.exe", "9.9.9", backup)

    be = _Holder()
    be.consume_update_marker()

    assert be.update_success is None
    fail = be.update_failure
    assert fail and fail["target"] == "9.9.9"
    assert fail["backup_dir"] == str(backup)
    assert fail["log"].endswith("update-setup.log")
    # 标记保留：用户「知道了」之前，每次启动都要再提醒
    assert remote_mod._update_marker_path().exists()
    asyncio.run(be.dismiss_update_failure())
    assert be.update_failure is None and not remote_mod._update_marker_path().exists()


def test_apply_update_writes_backup_and_marker_before_launch(home, monkeypatch, tmp_path):
    """apply_update 启动安装器前必须先备份 + 落标记；启动动作本身被桩掉。"""
    home_dir = Path(home) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    _make_db(home_dir / "skysheep.db")
    pending = tmp_path / "SkySheep-9.9.9-setup.exe"
    pending.write_bytes(b"MZ\0\0")

    launched: list[list[str]] = []

    class _FakeProc:
        pid = 0

    def fake_popen(cmd, **kwargs):
        launched.append(list(cmd))
        return _FakeProc()

    monkeypatch.setattr(remote_mod.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(remote_mod, "spawn_bg", lambda coro: coro.close())
    # 日志与提权查询都碰真实机器状态（真实 ~/.skysheep 日志 / 注册表）：全部桩掉
    monkeypatch.setattr(remote_mod, "_prepare_update_log", lambda pending: None)
    monkeypatch.setattr(remote_mod, "_setup_privilege_override", lambda: "")

    be = _Holder()
    be._pending_update = str(pending)
    r = asyncio.run(be.apply_update())

    assert r["quitting"] is True
    assert launched and launched[0][0] == "cmd", "安装器经 apply-update.cmd 启动（行为不变）"
    backups = list((home_dir / "backups").glob("preupdate-*"))
    assert len(backups) == 1 and (backups[0] / "skysheep.db").is_file()
    marker = json.loads(remote_mod._update_marker_path().read_text(encoding="utf-8"))
    assert marker["target_version"] == "9.9.9"
    assert marker["backup_dir"] == str(backups[0])
    # 对账：目标版本不是当前版本 → 留下失败现场（本进程不会再跑，验证即可）
    be2 = _Holder()
    be2.consume_update_marker()
    assert be2.update_failure and be2.update_failure["target"] == "9.9.9"


def test_download_setup_refuses_without_checksum(tmp_path, monkeypatch):
    """缺 .sha256 时 _download_setup 直接拒绝（fail closed），不落任何文件。"""
    from skysheep.server.backend_parts import remote as _remote

    dest = tmp_path / "setup.exe"
    with pytest.raises(_remote.UpdateChecksumError, match="校验和"):
        _remote._download_setup("https://example.invalid/setup.exe", dest, "")
    assert not dest.exists()


def test_backup_user_data_reports_failures(home, monkeypatch):
    """备份失败要如实回报失败项（不再只记日志后当作「已备份」）。"""
    home_dir = Path(home) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / "config.toml").write_text('default = "x"\n', encoding="utf-8")
    _make_db(home_dir / "skysheep.db")

    real_copy2 = remote_mod.shutil.copy2

    def flaky_copy2(src, dst, *a, **kw):
        if str(src).endswith("config.toml"):
            raise OSError(13, "permission denied (probe)")
        return real_copy2(src, dst, *a, **kw)

    monkeypatch.setattr(remote_mod.shutil, "copy2", flaky_copy2)
    bdir, failures = remote_mod._backup_user_data()
    assert bdir is not None
    assert "config.toml" in failures


def test_update_marker_records_backup_failures(home, tmp_path):
    """marker 携带 backup_failures，失败提示能如实告知哪些数据未备份。"""
    pending = tmp_path / "SkySheep-9.9.9-setup.exe"
    pending.write_bytes(b"MZ\0\0")
    assert remote_mod._write_update_marker(str(pending), "9.9.9", None, ["config.toml"])
    be = _Holder()
    be.consume_update_marker()
    assert be.update_failure is not None
    assert be.update_failure["backup_failures"] == ["config.toml"]
