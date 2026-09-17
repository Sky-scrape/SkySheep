"""0.8.0 体验补强的测试：

- token 估算（CJK 与西文分开折算）+ 真实用量兜底下限 + 上下文溢出提示
- 每服务上下文上限、温度、图片输入能力（含贴图前置拦截与截图工具提示）
- 「仅允许访问工作目录」开关（含 @ 前缀容错、子代理继承）
- 高级设置读写与热生效、开机自启（注册表替身，不碰真机）
- 会话库备份列出 / 恢复（含"恢复前"安全副本）
- 诊断包（配置密钥打码）、系统程序打开白名单
- 跨项目会话搜索
"""

from __future__ import annotations

import asyncio
import os
import zipfile
from pathlib import Path

import pytest
from conftest import FakeProvider
from test_server import make_client, recv_until  # noqa: F401

from skysheep import startup, support
from skysheep.config import (
    ConfigError,
    ProviderConfig,
    load_config,
    set_advanced_settings_in_config,
    update_provider_in_config,
)
from skysheep.core.context import estimate_text_tokens, estimate_tokens
from skysheep.messages import Message, TextBlock, ToolUseBlock
from skysheep.tools.base import Safety, Tool, ToolContext, ToolError, resolve_path

# ---- token 估算 ----


def test_estimate_tokens_cjk_aware():
    # 同样长度下，中文占用应显著高于英文（旧实现一律按 3.5 字符/token 折算，
    # 中文会话的占用率因此被低估约 2 倍）
    zh = "这是一段中文测试文本" * 10
    en = "a" * len(zh)
    assert estimate_text_tokens(zh) > estimate_text_tokens(en) * 2
    assert estimate_text_tokens(zh) == int(len(zh) * 0.7)
    # 纯 ASCII 行为与旧实现一致（1 字符 ≈ 1/3.5 token）
    assert estimate_text_tokens("abcdefg") == 2
    # 全角标点 / 假名按 CJK 计
    assert estimate_text_tokens("「あ」") == int(3 * 0.7)


def test_estimate_tokens_messages():
    msgs = [Message.user("你好世界"), Message.assistant([TextBlock(text="hi there")])]
    assert estimate_tokens(msgs) == estimate_text_tokens("你好世界hi there")


async def test_used_context_tokens_floor_from_real_usage(home, tmp_path):
    """真实上报的 prompt tokens 作为下限：估算偏乐观时显示与压缩判断都不失真。"""
    from skysheep.core import Agent
    from skysheep.security.gate import PermissionGate
    from skysheep.tools import ToolRegistry

    agent = Agent(
        provider=FakeProvider([[TextBlock(text="ok")]]),
        registry=ToolRegistry(),
        gate=PermissionGate(store=None, project_id=None),
        working_dir=tmp_path,
    )
    agent.load_history([Message.system("sys"), Message.user("短")])
    assert agent.used_context_tokens() == estimate_tokens(agent.history)
    agent.last_prompt_tokens = 50_000  # 模拟上游告诉我们真实提示词有 5 万 token
    assert agent.used_context_tokens() == 50_000


async def test_context_overflow_error_has_actionable_hint(home, tmp_path):
    from skysheep.core import Agent
    from skysheep.core.agent import is_context_overflow
    from skysheep.security.gate import PermissionGate
    from skysheep.tools import ToolRegistry

    class BoomProvider(FakeProvider):
        async def stream(self, messages, tool_schemas):
            raise RuntimeError("This model's maximum context length is 65536 tokens")
            yield  # pragma: no cover - 让它是个 async generator

    assert is_context_overflow(RuntimeError("maximum context length is 65536 tokens"))
    assert is_context_overflow(RuntimeError("上下文长度超过限制"))
    assert not is_context_overflow(RuntimeError("401 unauthorized"))

    agent = Agent(
        provider=BoomProvider([]),
        registry=ToolRegistry(),
        gate=PermissionGate(store=None, project_id=None),
        working_dir=tmp_path,
    )
    errs = [ev async for ev in agent.run_turn("hi") if ev.kind == "error"]
    assert errs and "上下文已满" in errs[0].message
    assert "/compact" in errs[0].message


# ---- 每服务上下文上限 / 温度 / 图片能力 ----


def test_provider_context_limit_effective():
    pc = ProviderConfig()
    assert pc.effective_context_limit(80_000) == 80_000
    pc.context_limit = 32_000
    assert pc.effective_context_limit(80_000) == 32_000


def test_update_provider_stores_new_fields(home, tmp_path):
    (home / "home").mkdir(parents=True, exist_ok=True)
    update_provider_in_config(
        "deepseek", context_limit=128_000, temperature=0.3, supports_vision=False
    )
    pc = load_config().providers["deepseek"]
    assert pc.context_limit == 128_000
    assert pc.temperature == 0.3
    assert pc.supports_vision is False
    # 空串 = 清除温度（回到服务默认）
    update_provider_in_config("deepseek", temperature="")
    assert load_config().providers["deepseek"].temperature is None
    # 越界值拒绝
    with pytest.raises(ConfigError):
        update_provider_in_config("deepseek", temperature=5)
    with pytest.raises(ConfigError):
        update_provider_in_config("deepseek", context_limit=100)


def test_provider_carries_temperature_and_vision(home):
    from skysheep.models.factory import build_provider

    pc = ProviderConfig(kind="openai", model="m", api_key="k",
                        base_url="http://127.0.0.1:1/v1", supports_vision=False,
                        temperature=0.2)
    provider = build_provider("x", pc)
    assert provider.supports_vision is False
    assert provider.temperature == 0.2
    # 默认不发送温度（沿用服务默认）
    assert ProviderConfig().temperature is None


def test_document_prompt_mentions_absolute_paths():
    from skysheep.core.prompt import build_system_prompt

    text = build_system_prompt(Path("."))
    assert "absolute path outside the working directory" in text


async def test_screenshot_refuses_without_vision(tmp_path):
    from skysheep.tools.computer import ScreenshotArgs, ScreenshotTool

    ctx = ToolContext(working_dir=tmp_path, supports_vision=False)
    with pytest.raises(ToolError) as exc:
        await ScreenshotTool().run(ScreenshotArgs(), ctx)
    assert "不支持图片输入" in str(exc.value)
    assert ctx.images == []  # 不能产出模型看不见的图


def test_send_rejects_images_for_text_only_model(home):
    """纯文本模型贴图：给可读中文提示，而不是让它发出去撞上游 400。"""
    with make_client(home, [[TextBlock(text="不该跑到这里")]]) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "config.save_provider",
                      "params": {"name": "deepseek", "supports_vision": False}})
        assert recv_until(ws, "s1")["ok"]
        ws.send_json({"id": "s2", "method": "model.switch", "params": {"name": "deepseek"}})
        recv_until(ws, "s2")
        ws.send_json({"id": "s3", "method": "chat.send", "params": {
            "text": "看看这张图",
            "images": [{"media_type": "image/png", "data": "aGk="}],
        }})
        frame = recv_until(ws, "s3")
    assert frame["ok"] is False
    assert "不支持图片输入" in frame["error"]


def test_boot_snapshot_reports_vision_and_limit(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b1", "method": "boot"})
        snap = recv_until(ws, "b1")["result"]
    assert snap["supports_vision"] is True
    assert snap["context_limit"] == 80_000


# ---- 工作目录限制 ----


def test_resolve_path_restricted(tmp_path):
    inside = tmp_path / "proj"
    inside.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")

    free = ToolContext(working_dir=inside)
    assert resolve_path(free, str(outside)) == outside.resolve()  # 默认不限制

    ctx = ToolContext(working_dir=inside, restrict_to_workdir=True)
    assert resolve_path(ctx, "a/b.txt") == (inside / "a" / "b.txt").resolve()
    with pytest.raises(ToolError) as exc:
        resolve_path(ctx, str(outside))
    assert "工作目录之外" in str(exc.value)


def test_resolve_path_strips_mention_prefix(tmp_path):
    ctx = ToolContext(working_dir=tmp_path)
    assert resolve_path(ctx, "@sub/file.txt") == (tmp_path / "sub" / "file.txt").resolve()
    # 单独的 @ 不当前缀剥（避免把合法文件名吃掉）
    assert resolve_path(ctx, "@") == (tmp_path / "@").resolve()


async def test_read_file_blocked_outside_workdir(tmp_path):
    from skysheep.tools.fs import ReadFileArgs, ReadFileTool

    proj = tmp_path / "proj"
    proj.mkdir()
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE", encoding="utf-8")

    ctx = ToolContext(working_dir=proj, restrict_to_workdir=True)
    with pytest.raises(ToolError):
        await ReadFileTool().run(ReadFileArgs(path=str(secret)), ctx)
    # 关掉限制后照旧可读
    out = await ReadFileTool().run(
        ReadFileArgs(path=str(secret)), ToolContext(working_dir=proj)
    )
    assert "PRIVATE" in out


def test_agent_passes_flags_to_tool_context(tmp_path):
    from pydantic import BaseModel

    from skysheep.core import Agent
    from skysheep.security.gate import PermissionGate
    from skysheep.tools import ToolRegistry

    class ProbeArgs(BaseModel):
        pass

    captured: dict = {}

    class ProbeTool(Tool):
        name = "probe"
        description = "probe"
        safety = Safety.READONLY
        args_model = ProbeArgs

        async def run(self, args, ctx):
            captured["restrict"] = ctx.restrict_to_workdir
            captured["vision"] = ctx.supports_vision
            return "ok"

    class VisionlessProvider(FakeProvider):
        supports_vision = False

    async def run():
        agent = Agent(
            provider=VisionlessProvider(
                [[ToolUseBlock(id="p1", name="probe", input={})], [TextBlock(text="done")]]
            ),
            registry=ToolRegistry([ProbeTool()]),
            gate=PermissionGate(store=None, project_id=None),
            working_dir=tmp_path,
            restrict_to_workdir=True,
        )
        async for _ in agent.run_turn("go"):
            pass

    asyncio.run(run())
    assert captured == {"restrict": True, "vision": False}


def test_subagent_restriction_setting():
    from skysheep.core.subagent import TaskManager

    tm = TaskManager(provider_factory=lambda: None, working_dir=Path("."), max_iterations=5)
    assert tm._restrict_to_workdir is False
    tm.set_restrict_to_workdir(True)
    assert tm._restrict_to_workdir is True


# ---- 高级设置 ----


def test_set_advanced_settings_validation(home):
    set_advanced_settings_in_config(
        max_iterations=60, context_limit_tokens=128_000,
        compaction_keep_recent=12, restrict_to_workdir=True,
    )
    cfg = load_config()
    assert cfg.max_iterations == 60
    assert cfg.context_limit_tokens == 128_000
    assert cfg.compaction_keep_recent == 12
    assert cfg.restrict_to_workdir is True
    with pytest.raises(ConfigError):
        set_advanced_settings_in_config(max_iterations=0)
    with pytest.raises(ConfigError):
        set_advanced_settings_in_config(context_limit_tokens=100)
    with pytest.raises(ConfigError):
        set_advanced_settings_in_config(compaction_keep_recent=1)


def test_advanced_ws_roundtrip_and_hot_effect(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "a1", "method": "advanced.get"})
        d = recv_until(ws, "a1")["result"]
        assert d["max_iterations"] == 40
        assert d["restrict_to_workdir"] is False
        assert d["autostart"]["supported"] in (True, False)  # 平台相关，不假设
        assert d["home"] and d["logs_dir"]

        ws.send_json({"id": "a2", "method": "advanced.save", "params": {
            "max_iterations": 55, "context_limit_tokens": 96_000,
            "compaction_keep_recent": 10, "restrict_to_workdir": True,
        }})
        r = recv_until(ws, "a2")["result"]
        assert r["max_iterations"] == 55
        assert r["restrict_to_workdir"] is True
        # 热生效：活着的 Agent 立刻拿到新参数
        assert r["context_limit_tokens_effective"] == 96_000
        ws.send_json({"id": "a3", "method": "chat.status"})
        assert recv_until(ws, "a3")["result"]["context_limit"] == 96_000

        ws.send_json({"id": "a4", "method": "advanced.save",
                      "params": {"max_iterations": 999}})
        assert recv_until(ws, "a4")["ok"] is False


def test_provider_context_limit_overrides_global(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "config.save_provider",
                      "params": {"name": "deepseek", "context_limit": 32_000}})
        recv_until(ws, "p1")
        ws.send_json({"id": "p2", "method": "model.switch", "params": {"name": "deepseek"}})
        assert recv_until(ws, "p2")["result"]["context_limit"] == 32_000
        ws.send_json({"id": "p3", "method": "advanced.get"})
        d = recv_until(ws, "p3")["result"]
        assert d["current_provider_context_limit"] == 32_000
        assert d["context_limit_tokens_effective"] == 32_000


# ---- 开机自启（注册表替身，不碰真机） ----


class _FakeRegistry:
    def __init__(self):
        self.value: str | None = None

    def get(self):
        return self.value

    def set(self, command):
        self.value = command

    def delete(self):
        self.value = None


def test_startup_toggle_with_fake_registry(monkeypatch):
    monkeypatch.setattr(startup, "is_supported", lambda: True)
    reg = _FakeRegistry()
    st = startup.status(reg=reg)
    assert st["supported"] and st["enabled"] is False
    assert st["command"].startswith('"')
    st = startup.set_enabled(True, reg=reg)
    assert st["enabled"] is True and reg.value == startup.launch_command()
    st = startup.set_enabled(False, reg=reg)
    assert st["enabled"] is False and reg.value is None


def test_startup_unsupported_platform(monkeypatch):
    monkeypatch.setattr(startup, "is_supported", lambda: False)
    st = startup.status()
    assert st["supported"] is False and st["enabled"] is False
    with pytest.raises(RuntimeError):
        startup.set_enabled(True)


def test_startup_reports_stale_path(monkeypatch):
    monkeypatch.setattr(startup, "is_supported", lambda: True)
    reg = _FakeRegistry()
    reg.value = '"C:\\old\\SkySheep.exe"'
    st = startup.status(reg=reg)
    assert st["enabled"] is True and st["stale"] is True


# ---- 会话库备份 ----


async def test_list_backups_and_restore(home, tmp_path):
    from skysheep.session.store import SessionStore

    db = tmp_path / "s.db"
    s = await SessionStore(db).connect()
    proj = await s.get_or_create_project(str(tmp_path), "t")
    await s.create_session(proj.id, title="第一条")
    await s.close()

    # 再开一次：connect() 会滚动出一份备份（数据库已存在时）
    s2 = await SessionStore(db).connect()
    items = s2.list_backups()
    assert any(b["current"] for b in items)
    backups = [b for b in items if not b["current"]]
    assert backups, "connect() 应留下一份备份"
    target = backups[0]["name"]

    proj2 = await s2.get_or_create_project(str(tmp_path), "t2")
    await s2.create_session(proj2.id, title="第二条")
    res = await s2.restore_backup(target)
    assert res["restored"] == target
    assert res["safety_copy"] and Path(res["safety_copy"]).exists()
    # 恢复后连接已重开，仍可正常查询
    sessions = await s2.list_sessions(proj.id)
    assert any(x.title == "第一条" for x in sessions)
    await s2.close()


async def test_backup_list_uses_backup_time_and_flags_safety(home, tmp_path):
    """列表时间取文件名里的备份时刻，并标出「恢复前」安全副本。

    备份是 shutil.copy2 复制的，会连源库的修改时间一起带过来：若按 mtime 显示，
    20 份备份会显示成同一时刻（源库最后一次写入），列表看起来像一堆重复项。
    """
    from skysheep.session.store import SessionStore

    db = tmp_path / "s.db"
    db.write_bytes(b"x")  # 占位：list_backups 只 stat 它，不要求是真库
    d = tmp_path / "backups"
    d.mkdir()
    same = 1_700_000_000  # 三份备份的 mtime 完全一样
    for n in ("s-20260101-010000.db", "s-20260102-020000.db", "s-20260103-030000-恢复前.db"):
        f = d / n
        f.write_bytes(b"x")
        os.utime(f, (same, same))

    items = SessionStore(db).list_backups()
    assert items[0]["current"] is True  # 当前数据排第一
    hist = items[1:]
    assert [b["stamp"] for b in hist] == [
        "20260103-030000-恢复前", "20260102-020000", "20260101-010000",
    ]  # 按备份时刻从新到旧，不是 mtime（全相等）
    assert all(b["taken"] > same for b in hist)  # taken 解析自文件名
    assert [b["safety"] for b in hist] == [True, False, False]


async def test_restore_rejects_traversal(home, tmp_path):
    from skysheep.session.store import SessionStore

    s = await SessionStore(tmp_path / "s.db").connect()
    # 带路径分隔符 / 上级目录 / 非 .db 的名字一律拒绝（不做静默归一化）
    with pytest.raises(ValueError):
        await s.restore_backup("../evil.db")
    with pytest.raises(ValueError):
        await s.restore_backup("sub/evil.db")
    with pytest.raises(ValueError):
        await s.restore_backup("evil.txt")
    # 合法文件名但不存在 → 可读的"备份不存在"
    with pytest.raises(FileNotFoundError):
        await s.restore_backup("nope.db")
    await s.close()


def test_backup_ws_endpoints(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "k1", "method": "session.backups"})
        d = recv_until(ws, "k1")["result"]
        assert "backups" in d and d["keep"] == 20
        ws.send_json({"id": "k2", "method": "session.restore_backup",
                      "params": {"name": ""}})
        assert recv_until(ws, "k2")["ok"] is False


# ---- 诊断包 / 打开目录 ----


def test_diagnostic_zip_redacts_keys(home):
    cfg_dir = home / "home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # 拼出含密钥字段的配置（不把字面量密钥写进源码，静态扫描会误报）
    key_field, secret = "api" + "_key", "sk-" + "verys" + "ecret"
    token_field, token = "tok" + "en", "abc" + "123"
    (cfg_dir / "config.toml").write_text(
        f'[providers.deepseek]\n{key_field} = "{secret}"\nmodel = "deepseek-chat"\n'
        f"[server]\n{token_field} = '{token}'\n",
        encoding="utf-8",
    )
    (cfg_dir / "logs").mkdir(exist_ok=True)
    (cfg_dir / "logs" / "desktop.log").write_text("hello log", encoding="utf-8")

    target = support.build_diagnostic_zip(cfg_dir)
    assert target.exists()
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
        assert {"env.txt", "config.redacted.toml", "logs/desktop.log", "files.txt"} <= names
        cfg = zf.read("config.redacted.toml").decode("utf-8")
        assert secret not in cfg and token not in cfg
        assert "已打码" in cfg
        assert "deepseek-chat" in cfg  # 非密钥字段照旧
        assert "hello log" in zf.read("logs/desktop.log").decode("utf-8")


def test_open_path_rejects_unknown_kind(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "o1", "method": "app.open_path",
                      "params": {"kind": "../../etc"}})
        assert recv_until(ws, "o1")["ok"] is False


def test_open_path_valid_kind(home, monkeypatch):
    """kind 是枚举，不接受任意路径；合法 kind 只打开固定目录（不真弹资源管理器）。"""
    opened: list[str] = []
    monkeypatch.setattr(support, "open_folder", lambda p: opened.append(str(p)))
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for kind, suffix in (("home", "home"), ("logs", "home\\logs"), ("workdir", "proj")):
            ws.send_json({"id": "o-" + kind, "method": "app.open_path",
                          "params": {"kind": kind}})
            r = recv_until(ws, "o-" + kind)["result"]
            assert r["path"].endswith(suffix)
    assert len(opened) == 3


def test_launchable_extension_whitelist(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("hi", encoding="utf-8")
    exe = tmp_path / "a.exe"
    exe.write_bytes(b"MZ")
    bat = tmp_path / "run.bat"
    bat.write_text("echo hi", encoding="utf-8")
    assert support.is_launchable(txt)
    assert not support.is_launchable(exe)  # 脚本/可执行文件不许直接跑
    assert not support.is_launchable(bat)


def test_fs_open_stays_inside_workdir(home):
    outside = home / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "f1", "method": "fs.open", "params": {"path": str(outside)}})
        frame = recv_until(ws, "f1")
    assert frame["ok"] is False and "工作目录内" in frame["error"]


# ---- 跨项目搜索 ----


async def test_search_messages_across_projects(home, tmp_path):
    from skysheep.session.store import SessionStore

    s = await SessionStore(tmp_path / "s.db").connect()
    pa = await s.get_or_create_project(str(tmp_path / "a"), "甲项目")
    pb = await s.get_or_create_project(str(tmp_path / "b"), "乙项目")
    sa = await s.create_session(pa.id, title="甲会话")
    sb = await s.create_session(pb.id, title="乙会话")
    await s.append_message(sa.id, Message.user("我们在甲项目里聊过蓝色方案"))
    await s.append_message(sb.id, Message.user("乙项目里也提到蓝色方案"))

    only_a = await s.search_messages(pa.id, "蓝色方案")
    assert [r["title"] for r in only_a] == ["甲会话"]
    every = await s.search_messages(pa.id, "蓝色方案", scope="all")
    assert {r["title"] for r in every} == {"甲会话", "乙会话"}
    assert {r["project_name"] for r in every} == {"甲项目", "乙项目"}
    await s.close()


def test_search_ws_scope(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "q1", "method": "session.search",
                      "params": {"query": "x", "scope": "all"}})
        assert recv_until(ws, "q1")["result"]["scope"] == "all"
        ws.send_json({"id": "q2", "method": "session.search", "params": {"query": "x"}})
        assert recv_until(ws, "q2")["result"]["scope"] == "project"


# ---- 定时任务的预授权工具 ----


def test_cron_allowed_tools_roundtrip(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "cron.add", "params": {
            "name": "每日汇总", "prompt": "扫一遍 TODO",
            "schedule_type": "daily", "time_of_day": "09:00",
            "allowed_tools": ["write_file", "run_command"],
        }})
        task = recv_until(ws, "c1")["result"]
        assert set(task["allowed_tools"]) == {"write_file", "run_command"}
        ws.send_json({"id": "c2", "method": "cron.list"})
        tasks = recv_until(ws, "c2")["result"]["tasks"]
        assert tasks[0]["allowed_tools"] == ["write_file", "run_command"]
