# 本机专属方法清单（2026-09-25 审查专项 2 的回归固化）。
#
# 回环免令牌模型下，安全姿态的关键是「放宽/凭据/无人值守执行类方法必须在
# LOCAL_ONLY_METHODS 里」。这份清单是 curated 最小集：如果有人删掉了其中任何
# 一个的保护，或者重构后清单挪了位置，测试立刻红。
# 新增方法不强制进这份文件；但凡是「远程持令牌客户端不该能调」的方法，
# 合入前应该把它加进来。
import re
from pathlib import Path

import pytest

from skysheep.server import app as server_app

# 每类至少抽一个代表 + 全部高危项：凭据读写、无人值守执行面、信任与放宽档、
# 降防护操作、重启/更新。
MUST_BE_LOCAL = (
    # 凭据与 provider 配置
    "config.save_provider", "config.add_provider_model", "config.probe_models",
    "websearch.save", "imagegen.save", "speech.save", "settings.export", "settings.import",
    # 无人值守执行面（渠道/定时/流水线的 allowed_tools 预授权）
    "cron.add", "cron.update", "cron.run_now",
    "pipeline.create", "pipeline.start", "pipeline.import", "pipeline.add_task",
    # 技能与 MCP（prompt 注入 / stdio 命令）
    "skills.install", "skills.toggle", "skills.delete",
    "mcp.import", "mcp.save_server", "mcp.delete", "mcp.reconnect",
    # 信任（放宽向）与系统级
    "app.install_update", "app.apply_update", "app.restart",
    "app.export_diagnostics", "project.switch", "project.delete", "session.restore_backup",
    "lan.enable", "remote.enable", "lan.rotate_token",
    "advanced.save", "hooks.save", "memory.save",
    "subagent.save", "default_model.set",
    # 本机体验/信息面（审查 P1-3 收口）
    "app.notify", "fs.open", "term.close", "memory.get", "model.switch",
)

# 不在集合里、但方法体内有散点本机检查/远端降级的例外（审查专项 2 逐一核实过）。
# 断言它们的 dispatch 分支源码里确实出现 local 检查——有人删守卫时测试变红。
INLINE_GUARDED = (
    "permission.set_mode",  # 放宽档仅本机（收紧档远端可切）
    "trust.grant",          # 信任授予仅本机（撤销是收紧向，远端可）
    "ui.save",              # 远端可调，accept_edits 字段对远端剥除
    "term.spawn", "term.input",  # 终端三件套仅本机（term.resize 是同块 else 兜底，无独立分支）
    "channel.save", "channel.enable", "channel.weixin_login_start",  # 渠道配置仅本机
    "trust.list", "session.search", "session.list",  # 远端收窄枚举面
)


def _local_only_set() -> set[str]:
    return set(server_app.LOCAL_ONLY_METHODS)


def test_local_only_covers_curated_high_risk_methods():
    missing = [m for m in MUST_BE_LOCAL if m not in _local_only_set()]
    assert not missing, f"以下高危方法不在 LOCAL_ONLY_METHODS，远程客户端将可调用：{missing}"


def test_local_only_methods_all_exist_in_dispatch():
    """清单里的方法必须真实存在于 dispatch 分支（防改名单时留下僵尸条目）。"""
    dispatch_src = Path(server_app.__file__).read_text(encoding="utf-8")
    methods = set(re.findall(r'method == "([a-z_.]+)"', dispatch_src))
    assert methods, "dispatch 源码里解析不到任何方法分支，解析逻辑可能已失效"
    stale = [m for m in server_app.LOCAL_ONLY_METHODS if m not in methods]
    assert not stale, f"LOCAL_ONLY_METHODS 里的方法在 dispatch 中不存在：{stale}"


def test_remote_client_blocked_for_entire_local_only_set(home, monkeypatch):
    """整表回归：REMOTE 来源调 LOCAL_ONLY 里任何一个方法都必须被拒。"""
    from starlette.testclient import TestClient

    monkeypatch.setattr(server_app, "_client_is_local", lambda ws: False)
    from skysheep.models.fake import FakeProvider

    app = server_app.create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: FakeProvider([]),
    )
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        while True:
            frame = ws.receive_json()
            if frame.get("id") == "b":
                assert frame["ok"]
                break
        for method in sorted(server_app.LOCAL_ONLY_METHODS):
            ws.send_json({"id": "x-" + method, "method": method, "params": {}})
            # 不逐条等回复（部分方法会先推事件），收完 N 条回复再统一断言
        replies = {}
        while len(replies) < len(server_app.LOCAL_ONLY_METHODS):
            frame = ws.receive_json()
            if "event" in frame:
                continue
            replies[frame["id"]] = frame
        denied = [mid for mid, f in replies.items() if f.get("ok") is not False]
        assert not denied, f"远程客户端竟能成功调用的本机方法：{denied}"


@pytest.mark.parametrize("method", INLINE_GUARDED)
def test_inline_guarded_methods_have_local_check(method):
    """内联守卫例外：方法分支（或其外层守卫块）源码里必须真的有 local 检查。

    term.* 的守卫在包住三个分支的外层块里，所以取分支前后各一段窗口做断言；
    有人把守卫挪出窗口或删掉时测试变红，人工确认后再更新窗口口径。
    """
    src = Path(server_app.__file__).read_text(encoding="utf-8")
    i = src.find(f'method == "{method}"')
    assert i >= 0, f'dispatch 里找不到方法分支：{method}'
    window = src[max(0, i - 1600) : i + 1600]
    assert "not local" in window, (
        f"{method} 前后窗口内没有 local 检查（审查专项 2 记录的守卫被移除或挪远了？）"
    )
