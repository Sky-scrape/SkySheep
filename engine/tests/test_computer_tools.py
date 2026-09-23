"""电脑控制工具测试。

红线：绝不真实注入鼠标/键盘（SendInput 不做真调用），只覆盖纯函数、
参数校验、权限门控与被 monkeypatch 的抓屏链路；无副作用的真实操作
（窗口枚举、剪贴板读取）仅做冒烟。
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import pytest
from conftest import FakeProvider
from PIL import Image

from skysheep.core import Agent
from skysheep.messages import ImageBlock, TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import (
    ClipboardReadTool,
    ClipboardWriteTool,
    KeyboardTool,
    MouseTool,
    ScreenshotTool,
    ToolContext,
    ToolError,
    ToolRegistry,
    WindowListTool,
    WindowTool,
    default_tools,
)
from skysheep.tools.computer import IS_WINDOWS, _to_abs, parse_hotkeys


def ctx(tmp_path):
    return ToolContext(working_dir=tmp_path)


# ---- 纯函数：坐标换算 / 热键解析 ----


def test_to_abs_primary_corners():
    assert _to_abs(0, 0, 0, 0, 1920, 1080) == (0, 0)
    assert _to_abs(1919, 1079, 0, 0, 1920, 1080) == (65535, 65535)
    nx, ny = _to_abs(960, 540, 0, 0, 1920, 1080)
    assert 32000 < nx < 33600 and 32000 < ny < 33600


def test_to_abs_negative_origin_clamps():
    # 副屏在主屏左侧（vx=-1920）、虚拟屏 3840 宽的双屏场景
    assert _to_abs(-1920, 0, -1920, 0, 3840, 1080) == (0, 0)
    nx, _ = _to_abs(0, 0, -1920, 0, 3840, 1080)
    assert 32000 < nx < 33600  # 主屏左边缘 ≈ 虚拟屏中线
    assert _to_abs(-5000, -10, -1920, 0, 3840, 1080)[0] == 0  # 越界钳制


def test_parse_hotkeys():
    assert parse_hotkeys("ctrl+shift+esc") == [0x11, 0x10, 0x1B]
    assert parse_hotkeys("win+r") == [0x5B, 0x52]
    assert parse_hotkeys("Enter") == [0x0D]
    assert parse_hotkeys("a") == [0x41]
    with pytest.raises(ToolError, match="不支持的按键名"):
        parse_hotkeys("ctrl+mega")
    with pytest.raises(ToolError):
        parse_hotkeys("++")


# ---- args 校验（不触发任何注入） ----


def test_keyboard_args_requires_exactly_one():
    with pytest.raises(ValueError):
        KeyboardTool().args_model()
    with pytest.raises(ValueError):
        KeyboardTool().args_model(text="hi", keys="ctrl+s")
    m = KeyboardTool().args_model(keys="ctrl+s")
    assert m.text is None


def test_mouse_args_validation():
    with pytest.raises(ValueError):
        MouseTool().args_model(action="move")  # move 需要 x/y
    with pytest.raises(ValueError):
        MouseTool().args_model(action="drag", x=1, y=1)  # drag 需要终点
    with pytest.raises(ValueError):
        MouseTool().args_model(action="click", x=1)  # x/y 必须成对
    MouseTool().args_model(action="click")  # 原地点击合法
    MouseTool().args_model(action="scroll", amount=-3)


async def test_keyboard_invalid_key_no_injection(tmp_path):
    with pytest.raises(ToolError, match="不支持的按键名"):
        await KeyboardTool().run(
            KeyboardTool().args_model(keys="ctrl+.INVALID"), ctx(tmp_path)
        )


# ---- arg_text 与权限白名单 ----


def test_arg_text_and_rules():
    mt, kt, wt = MouseTool(), KeyboardTool(), WindowTool()
    assert mt.arg_text({"action": "click", "x": 10, "y": 20}) == "click x=10 y=20"
    assert mt.arg_text({"action": "scroll", "amount": 3}) == "scroll amount=3"
    assert kt.arg_text({"keys": "ctrl+s"}) == "hotkey ctrl+s"
    assert kt.arg_text({"text": "你好"}) == "type 你好"
    assert wt.arg_text({"action": "activate", "title": "记事本"}) == "activate 记事本"
    for tool, inp, prefix in [
        (mt, {"action": "click", "x": 10, "y": 20}, "click"),
        (wt, {"action": "activate", "title": "记事本"}, "activate"),
    ]:
        rule = PermissionGate.rule_for(tool, inp)
        assert rule.kind == "prefix"
        assert rule.pattern == prefix
    # 键盘例外：type / hotkey 的内容就是对焦点窗口的任意操作，按动作放行等于放行任意输入，
    # 所以只固化当次内容（exact），而不是发一条 prefix="type" 的规则
    for inp in ({"keys": "ctrl+s"}, {"text": "你好"}):
        rule = PermissionGate.rule_for(kt, inp)
        assert rule.kind == "exact"
        assert rule.pattern == kt.arg_text(inp)


async def test_dangerous_tools_need_confirmation():
    gate = PermissionGate()
    for tool, inp in [
        (MouseTool(), {"action": "click", "x": 1, "y": 2}),
        (KeyboardTool(), {"keys": "ctrl+s"}),
        (WindowTool(), {"action": "close", "title": "x"}),
        (ClipboardWriteTool(), {"text": "hi"}),
    ]:
        pending = await gate.authorize(tool, inp)
        assert pending is not None, tool.name
        pending.resolve("deny")


async def test_whitelisted_action_passes_others_still_ask():
    gate = PermissionGate()
    mt = MouseTool()
    gate.add_session_rule(PermissionGate.rule_for(mt, {"action": "click", "x": 1, "y": 2}))
    assert await gate.authorize(mt, {"action": "click", "x": 9, "y": 9}) is None
    assert await gate.authorize(mt, {"action": "scroll", "amount": 3}) is not None


async def test_keyboard_rule_is_exact_only():
    """键盘不按动作放行：批准一次 ctrl+s 不等于放行任意按键/任意文本输入。"""
    gate = PermissionGate()
    kt = KeyboardTool()
    gate.add_session_rule(PermissionGate.rule_for(kt, {"keys": "ctrl+s"}))
    assert await gate.authorize(kt, {"keys": "ctrl+s"}) is None
    assert await gate.authorize(kt, {"keys": "ctrl+a"}) is not None
    assert await gate.authorize(kt, {"text": "whoami"}) is not None
    # 确认请求上带一句解释，用户不会误以为白名单坏了
    p = await gate.authorize(kt, {"text": "whoami"})
    assert "键盘" in p.note


# ---- screenshot（monkeypatch 抓屏，不真截） ----


def _fake_grab(monkeypatch, w=64, h=48):
    img = Image.new("RGB", (w, h), color=(200, 100, 50))
    monkeypatch.setattr("skysheep.tools.computer._grab_image", lambda all_screens: img)
    return img


async def test_screenshot_attaches_image(tmp_path, monkeypatch, home):
    _fake_grab(monkeypatch)
    c = ctx(tmp_path)
    out = await ScreenshotTool().run(ScreenshotTool().args_model(), c)
    assert "64x48" in out
    assert len(c.images) == 1
    raw = base64.b64decode(c.images[0].data)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    saved = home / "home" / "screenshots"
    assert saved.is_dir() and list(saved.iterdir()), "副本应落在 SKYSHEEP_HOME/screenshots"


async def test_screenshot_scales_big_capture(tmp_path, monkeypatch, home):
    _fake_grab(monkeypatch, w=3000, h=1200)
    c = ctx(tmp_path)
    out = await ScreenshotTool().run(ScreenshotTool().args_model(), c)
    assert "缩放" in out
    im = Image.open(io.BytesIO(base64.b64decode(c.images[0].data)))
    assert im.size == (2400, 960)


async def test_screenshot_failure_is_toolerror(tmp_path, monkeypatch):
    def boom(all_screens):
        raise RuntimeError("no display")

    monkeypatch.setattr("skysheep.tools.computer._grab_image", boom)
    with pytest.raises(ToolError, match="截屏失败"):
        await ScreenshotTool().run(ScreenshotTool().args_model(), ctx(tmp_path))


# ---- agent 级：截图作为 user 消息进入历史与下一次模型调用 ----


async def test_agent_screenshot_becomes_user_image_message(tmp_path, monkeypatch, home):
    _fake_grab(monkeypatch)
    provider = FakeProvider(
        [
            [ToolUseBlock(id="s1", name="screenshot", input={"screen": "primary"})],
            [TextBlock(text="看到了")],
        ]
    )
    agent = Agent(
        provider=provider,
        # 电脑控制工具默认关（M16），这个用例要的就是它，显式打开
        registry=ToolRegistry(default_tools(computer_control=True)),
        gate=PermissionGate(),
        working_dir=tmp_path,
    )
    events = []
    async for ev in agent.run_turn("看看屏幕"):
        events.append(ev)

    # 历史: user, assistant(tool_use), tool, user(图片), assistant(text)
    roles = [m.role for m in agent.history]
    assert roles == ["user", "assistant", "tool", "user", "assistant"]
    img_msg = agent.history[3]
    assert any(isinstance(b, ImageBlock) for b in img_msg.content)
    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert finished.images and finished.images[0]["media_type"] == "image/png"
    # 第二次模型调用能看到这张图
    assert any(
        isinstance(b, ImageBlock)
        for m in provider.calls[1]
        if m.role == "user"
        for b in m.content
    )


# ---- 真实但无副作用的冒烟（仅 Windows） ----


@pytest.mark.skipif(not IS_WINDOWS, reason="window_list 依赖 Win32")
async def test_window_list_smoke():
    out = await WindowListTool().run(WindowListTool().args_model(), ctx(Path.cwd()))
    assert isinstance(out, str)
    assert "可见窗口" in out or "没有可见" in out


@pytest.mark.skipif(not IS_WINDOWS, reason="clipboard_read 依赖 Win32")
async def test_clipboard_read_smoke(tmp_path):
    out = await ClipboardReadTool().run(ClipboardReadTool().args_model(), ctx(tmp_path))
    assert isinstance(out, str)


# ---- 截图副本保留策略 ----


def test_prune_screenshots_keeps_recent(tmp_path):
    """副本只保留最近 KEEP_SCREENSHOTS 张（删最旧的），否则会无声涨满磁盘。"""
    import time as _t

    from skysheep.tools.computer import KEEP_SCREENSHOTS, _prune_screenshots

    shots = tmp_path / "screenshots"
    shots.mkdir()
    total = KEEP_SCREENSHOTS + 12
    names: list[str] = []
    for i in range(total):
        name = f"20260101_0000{i:02d}_aaaa.png"
        names.append(name)
        (shots / name).write_bytes(b"\x89PNG\r\n\x1a\n")
        # mtime 必须错开：_prune 按 mtime 排序淘汰
        _t.sleep(0.002)
    _prune_screenshots(shots)
    left = sorted(shots.glob("*.png"))
    assert len(left) == KEEP_SCREENSHOTS
    remaining = {p.name for p in left}
    # 最先写的 12 张（最旧）被删，最后一张（最新）留着
    assert set(names[:12]).isdisjoint(remaining)
    assert names[-1] in remaining


def test_prune_screenshots_noop_below_limit(tmp_path):
    from skysheep.tools.computer import _prune_screenshots

    shots = tmp_path / "screenshots"
    shots.mkdir()
    (shots / "a.png").write_bytes(b"x")
    (shots / "b.png").write_bytes(b"x")
    _prune_screenshots(shots)
    assert len(list(shots.glob("*.png"))) == 2


def test_prune_screenshots_ignores_non_png(tmp_path):
    """用户自己放进目录的其它文件不该被清理动作波及。"""
    from skysheep.tools.computer import KEEP_SCREENSHOTS, _prune_screenshots

    shots = tmp_path / "screenshots"
    shots.mkdir()
    (shots / "notes.txt").write_text("keep me", encoding="utf-8")
    for i in range(KEEP_SCREENSHOTS + 5):
        (shots / f"s{i}.png").write_bytes(b"x")
    _prune_screenshots(shots)
    assert (shots / "notes.txt").exists()
    assert len(list(shots.glob("*.png"))) == KEEP_SCREENSHOTS


def test_prune_screenshots_missing_dir_is_silent(tmp_path):
    from skysheep.tools.computer import _prune_screenshots

    _prune_screenshots(tmp_path / "nope")  # 不抛
