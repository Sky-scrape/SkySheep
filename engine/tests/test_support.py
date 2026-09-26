# 诊断包脱敏（support.py）回归：2026-09-25 审查专项 7 的自动化断言。
#
# 诊断包是要发给外部开发者的，config.toml 里的明文凭据（provider api_key、
# server.token、渠道 app_secret/bot_token）一旦进包就等同泄露。这里锁三件事：
# 打码按键名包含匹配且覆盖渠道凭据、诊断包内不含会话库与全局记忆、
# open_external 只放行 http(s)。
import zipfile

import pytest

from skysheep import support


@pytest.fixture()
def fake_home(tmp_path):
    """一份带凭据的最小 SKYSHEEP_HOME 结构。"""
    (tmp_path / "logs").mkdir()
    (tmp_path / "sessions").mkdir()
    (tmp_path / "config.toml").write_text(
        "\n".join([
            "[providers.openai]",
            "kind = \"openai\"",
            "api_key = \"sk-SUPER-SECRET\"",
            "model = \"gpt-x\"",
            "",
            "[server]",
            "token = \"lan-token-SECRET\"",
            "",
            "[channels.platforms.feishu]",
            "app_id = \"cli_x\"",
            "app_secret = \"feishu-SECRET\"",
            "bot_token = \"weixin-SECRET\"",
            "allowed_ids = [\"u1\"]",
            "",
            "[ui]",
            "theme = \"paper\"",
        ]),
        encoding="utf-8",
    )
    (tmp_path / "logs" / "desktop.log").write_text("line1\nline2\n", encoding="utf-8")
    (tmp_path / "memory.md").write_text("用户偏好记录", encoding="utf-8")
    (tmp_path / "sessions" / "skysheep.db").write_bytes(b"SQLite format 3")
    return tmp_path


def test_redact_masks_all_secret_key_shapes(fake_home):
    """api_key / token / app_secret / bot_token 全部打码，普通键原样保留。"""
    redacted = support._redact_toml(fake_home / "config.toml")
    assert "sk-SUPER-SECRET" not in redacted
    assert "lan-token-SECRET" not in redacted
    assert "feishu-SECRET" not in redacted
    assert "weixin-SECRET" not in redacted
    assert 'app_secret = "***已打码***"' in redacted
    assert 'bot_token = "***已打码***"' in redacted
    assert 'api_key = "***已打码***"' in redacted
    assert 'token = "***已打码***"' in redacted
    # 非凭据行不受影响
    assert 'model = "gpt-x"' in redacted
    assert 'theme = "paper"' in redacted
    assert 'allowed_ids = ["u1"]' in redacted


def test_diagnostic_zip_contains_no_secrets(fake_home, tmp_path):
    """诊断包内容审计：配置打码、不打包会话库与记忆正文、环境信息在场。"""
    out = support.build_diagnostic_zip(fake_home, dest_dir=tmp_path)
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        config = zf.read("config.redacted.toml").decode("utf-8")
        assert "SkySheep" in zf.read("env.txt").decode("utf-8")
        listing = zf.read("files.txt").decode("utf-8")
    for secret in ("sk-SUPER-SECRET", "lan-token-SECRET", "feishu-SECRET", "weixin-SECRET"):
        assert secret not in config, secret
    assert "config.redacted.toml" in names
    assert "env.txt" in names
    # 会话库与记忆正文不进包（少一份泄漏面）
    assert not any(n.endswith(".db") for n in names)
    assert "memory.md" not in names
    # 目录清单只有文件名与大小，没有内容
    assert "skysheep.db" in listing
    assert "SQLite format 3" not in listing


def test_diagnostic_zip_handles_missing_files(tmp_path):
    """空 home 也能出包（新装环境排障）：不抛异常、缺文件就是没有对应条目。"""
    out = support.build_diagnostic_zip(tmp_path, dest_dir=tmp_path)
    with zipfile.ZipFile(out) as zf:
        assert "config.redacted.toml" not in zf.namelist()
        assert "env.txt" in zf.namelist()


def test_redact_keeps_comments_and_empty_values(tmp_path):
    """注释行与空值凭据键：注释不动，空值写成空串（保结构）。"""
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        "# api_key = \"注释里的不算\"\napi_key = \"\"\nother = \"v\"\n",
        encoding="utf-8",
    )
    redacted = support._redact_toml(cfg)
    assert "# api_key = \"注释里的不算\"" in redacted
    assert 'api_key = ""' in redacted
    assert 'other = "v"' in redacted


def test_open_external_rejects_non_http(tmp_path):
    """open_external 的唯一放行面是 http(s)（SSRF/任意协议跳转的底线）。"""
    for bad in ("file:///C:/Windows/System32/calc.exe", "javascript:alert(1)",
                "ftp://example.com/x", "C:\\some\\path", "", "  "):
        with pytest.raises(RuntimeError):
            support.open_external(bad)


def test_is_launchable_whitelist(tmp_path):
    """危险扩展名（.exe/.bat/.ps1/.cmd/.msi）不允许直接启动，退化为定位。"""
    ok = tmp_path / "a.txt"
    ok.write_text("x", encoding="utf-8")
    assert support.is_launchable(ok)
    ok2 = tmp_path / "a.PDF"
    ok2.write_bytes(b"x")
    assert support.is_launchable(ok2)  # 大小写不敏感
    for dangerous in ("a.exe", "a.bat", "a.ps1", "a.cmd", "a.msi", "a.scr", "a.vbs"):
        assert not support.is_launchable(tmp_path / dangerous), dangerous
    assert not support.is_launchable(tmp_path / "no_ext")
    assert not support.is_launchable(tmp_path / "ghost.txt")  # 不存在的文件不可启动
