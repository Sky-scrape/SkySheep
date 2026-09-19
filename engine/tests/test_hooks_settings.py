"""设置页 Hooks 面板：读取 / 保存 / 校验 / 热生效。

此前 config.toml 的 [hooks] 只能手改配置文件，界面里没有任何入口，
而 README 的特性对照表把 Hooks 列为已实现能力。
"""

from __future__ import annotations

import tomllib

import pytest
from test_server import make_client, recv_until  # noqa: F401  (helpers re-exported)

from skysheep.config import ConfigError, config_path, load_config, set_hooks_in_config
from skysheep.core.hooks import hooks_from_config, load_raw_config

# ---------------------------------------------------------------- config 层


def test_set_hooks_writes_and_reads_back(home):
    set_hooks_in_config(
        pre=[{"match": "write_file", "command": "check.bat", "timeout_s": 5}],
        post=[{"match": "*", "command": "notify.exe", "timeout_s": 20}],
    )
    raw = load_raw_config()
    pre, post = hooks_from_config(raw)
    assert len(pre) == 1 and pre[0].match == "write_file" and pre[0].command == "check.bat"
    assert len(post) == 1 and post[0].timeout_s == 20


def test_set_hooks_clears_table_when_empty(home):
    set_hooks_in_config(pre=[{"match": "*", "command": "x.bat"}])
    assert "hooks" in load_raw_config()
    set_hooks_in_config(pre=[], post=[])
    # 两组都空 → 整张表删掉（留空表会让人以为还在生效）
    assert "hooks" not in load_raw_config()


def test_set_hooks_keeps_other_group_when_only_one_passed(home):
    set_hooks_in_config(pre=[{"match": "*", "command": "a.bat"}])
    set_hooks_in_config(post=[{"match": "*", "command": "b.bat"}])
    raw = load_raw_config()
    pre, post = hooks_from_config(raw)
    assert len(pre) == 1 and pre[0].command == "a.bat"
    assert len(post) == 1 and post[0].command == "b.bat"


def test_set_hooks_rejects_empty_command(home):
    with pytest.raises(ConfigError) as ei:
        set_hooks_in_config(pre=[{"match": "*", "command": "   "}])
    assert "缺少 command" in str(ei.value)


def test_set_hooks_rejects_newline_in_command(home):
    """命令经 cmd /c 执行，换行等于塞进第二条命令。"""
    with pytest.raises(ConfigError) as ei:
        set_hooks_in_config(pre=[{"match": "*", "command": "a.bat\nrm -rf /"}])
    assert "不能包含换行" in str(ei.value)


def test_set_hooks_rejects_bad_timeout(home):
    with pytest.raises(ConfigError) as ei:
        set_hooks_in_config(pre=[{"match": "*", "command": "a.bat", "timeout_s": "abc"}])
    assert "timeout_s" in str(ei.value)
    with pytest.raises(ConfigError):
        set_hooks_in_config(pre=[{"match": "*", "command": "a.bat", "timeout_s": 0}])
    with pytest.raises(ConfigError):
        set_hooks_in_config(pre=[{"match": "*", "command": "a.bat", "timeout_s": 9999}])


def test_set_hooks_defaults_match_and_timeout(home):
    set_hooks_in_config(pre=[{"command": "a.bat"}])
    raw = load_raw_config()
    pre, _ = hooks_from_config(raw)
    assert pre[0].match == "*" and pre[0].timeout_s == 10


def test_set_hooks_preserves_unrelated_config(home):
    """写 hooks 不能冲掉别的设置项。"""
    from skysheep.config import set_advanced_settings_in_config

    set_advanced_settings_in_config(max_iterations=55)
    set_hooks_in_config(pre=[{"match": "*", "command": "a.bat"}])
    with open(config_path(), "rb") as f:
        raw = tomllib.load(f)
    assert raw["max_iterations"] == 55
    assert raw["hooks"]["pre_tool_use"][0]["command"] == "a.bat"


# ---------------------------------------------------------------- WS 层


def test_hooks_get_returns_rules_and_tool_names(home):
    set_hooks_in_config(pre=[{"match": "write_file", "command": "chk.bat", "timeout_s": 7}])
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "h1", "method": "hooks.get"})
        frame = recv_until(ws, "h1")
    assert frame["ok"], frame
    r = frame["result"]
    assert r["pre"] == [{"match": "write_file", "command": "chk.bat", "timeout_s": 7.0}]
    assert r["post"] == []
    assert r["active_pre"] == 1 and r["active_post"] == 0
    assert r["config_path"]
    # 工具名候选可供前端 datalist，写 match 不用凭记忆
    assert "read_file" in r["tool_names"] and "write_file" in r["tool_names"]


def test_hooks_save_roundtrip_and_hot_apply(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({
            "id": "s1", "method": "hooks.save",
            "params": {"pre": [{"match": "run_command", "command": "audit.bat", "timeout_s": 15}]},
        })
        frame = recv_until(ws, "s1")
        assert frame["ok"], frame
        assert frame["result"]["active_pre"] == 1
        # 落盘的确实是这一份
        ws.send_json({"id": "g1", "method": "hooks.get"})
        got = recv_until(ws, "g1")["result"]
    assert got["pre"][0]["command"] == "audit.bat"


def test_hooks_save_rejects_bad_rule_with_readable_error(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({
            "id": "s2", "method": "hooks.save",
            "params": {"pre": [{"match": "*", "command": ""}]},
        })
        frame = recv_until(ws, "s2")
    assert frame["ok"] is False
    assert "command" in frame["error"]


def test_hooks_save_can_clear_all(home):
    set_hooks_in_config(pre=[{"match": "*", "command": "a.bat"}])
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "c1", "method": "hooks.save", "params": {"pre": [], "post": []}})
        frame = recv_until(ws, "c1")
    assert frame["ok"], frame
    assert frame["result"]["active_pre"] == 0
    assert "hooks" not in load_raw_config()


async def test_hooks_are_wired_into_agent(home):
    """保存后的钩子要真的挂到活着的 Agent 上（热生效），不是只写进文件。"""
    from skysheep.server.backend import ServerBackend

    be = ServerBackend(working_dir=home / "proj")
    await be.setup()
    try:
        assert be.hooks is None  # 未配置时为空
        await be.save_hooks_settings({
            "pre": [{"match": "write_file", "command": "chk.bat", "timeout_s": 5}]
        })
        assert be.hooks is not None, "保存后应重建 HookRunner"
        assert be.hooks.has_pre
        assert be.hooks.pre_rules[0].command == "chk.bat"
        # 推给了基础 Agent（Agent 循环才能在调用前拦下）
        assert be._base_agent.hooks is be.hooks
        # 清空后回到 None，而不是留一个空 Runner 白跑一趟调用链
        await be.save_hooks_settings({"pre": [], "post": []})
        assert be.hooks is None
        assert be._base_agent.hooks is None
    finally:
        await be.shutdown()


def test_no_hooks_config_means_none_runner(home):
    """没配钩子时 hooks 为 None：Agent 循环里的 has_pre 判断不应被空 Runner 触发。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "g2", "method": "hooks.get"})
        r = recv_until(ws, "g2")["result"]
    assert r["pre"] == [] and r["post"] == []
    assert r["active_pre"] == 0


def test_load_config_unaffected_by_hooks(home):
    """hooks 是 config.toml 的额外表，不该让 load_config 解析失败。"""
    set_hooks_in_config(pre=[{"match": "*", "command": "a.bat"}])
    cfg = load_config()
    assert cfg.max_iterations >= 1
