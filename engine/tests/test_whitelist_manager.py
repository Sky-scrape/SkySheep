"""白名单管家：设置页白名单列表的 stale 失效标记与配套接线。

- store 层：remove_rule 按项目归属删除（列表/删除基础语义已在 test_store.py 的
  test_rules / test_rules_order_and_clear 覆盖，这里补归属隔离）；
- stale 判定：run_command 的 docker/ssh/scp 两词前缀命中、git status 不命中、
  exact 不标——判定与 gate 匹配侧 fail-closed 同源（security/gate.py 的
  _is_arbitrary_exec_prefix，backend 只做转发，不造第二套）；
- WS 接线：whitelist.list 返回 stale / stale_reason 字段，whitelist.remove 照常可删；
- 前端接线：失效徽章 / 刷新按钮的源码锚点（零构建、无 JS 运行时测试，
  沿用 test_frontend_wiring 的「源码里接线存在」惯例）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from conftest import read_app_bundle
from test_server import make_client, recv_until

from skysheep.server.backend import ServerBackend

# 用例可能从任意 cwd 启动：静态资源按本文件定位成绝对路径（与 test_frontend_wiring 同源）
STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static"


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


STALE_REASON = "安全收紧（2.3.0）：该类前缀已回落逐次确认，规则不再命中"


# ---------- store 层：删除的项目归属（安全审查 B11 的存量行为，回归锁定） ----------


async def test_remove_rule_scoped_to_project(store):
    """remove_rule 带 project_id 时只删本项目的规则：凭枚举到的 rule_id
    不能删掉别的项目的白名单；不带 project_id 才是全局删。"""
    mine = await store.get_or_create_project("/tmp/demo-wl-mine")
    other = await store.get_or_create_project("/tmp/demo-wl-other")
    await store.add_rule(mine.id, "run_command", "prefix", "git status")
    await store.add_rule(other.id, "run_command", "prefix", "git push")
    mine_rules = await store.list_rules(mine.id)
    other_rules = await store.list_rules(other.id)

    # 用别的项目的 rule_id + 本项目归属条件删：删不动
    await store.remove_rule(other_rules[0]["id"], project_id=mine.id)
    assert len(await store.list_rules(other.id)) == 1
    assert len(await store.list_rules(mine.id)) == 1

    # 归属对上才真删
    await store.remove_rule(mine_rules[0]["id"], project_id=mine.id)
    assert await store.list_rules(mine.id) == []
    assert len(await store.list_rules(other.id)) == 1


# ---------- stale 判定（backend 转发 gate 的 _is_arbitrary_exec_prefix） ----------


def test_stale_marker_hits_legacy_entry_exec_prefixes():
    """run_command 的 docker/ssh/scp 两词前缀 = 历史遗留失效规则：stale=True。
    .exe 后缀与路径前缀的写法经 _norm_exe_name 归一后同样命中（与 gate 同一口径）。"""
    for pattern in ("docker run", "Docker run --rm", "docker.exe run",
                    "C:\\Tools\\docker.exe run -v C:\\:\\/host alpine",
                    "ssh host", "scp local remote:"):
        rule = ServerBackend._stale_fields(
            {"tool": "run_command", "kind": "prefix", "pattern": pattern}
        )
        assert rule["stale"] is True, pattern
        assert rule["stale_reason"] == STALE_REASON, pattern


def test_stale_marker_skips_normal_rules():
    """git status 前缀、exact / always 类型、非 run_command 工具、空 pattern：不标。"""
    cases = [
        {"tool": "run_command", "kind": "prefix", "pattern": "git status"},
        {"tool": "run_command", "kind": "prefix", "pattern": "kubectl get pods"},
        {"tool": "run_command", "kind": "exact", "pattern": "docker run"},  # exact 不标
        {"tool": "run_command", "kind": "always", "pattern": ""},
        {"tool": "browser", "kind": "prefix", "pattern": "open https://x"},  # 工具不符不标
        {"tool": "run_command", "kind": "prefix", "pattern": ""},  # 空 pattern 不标
    ]
    for rule in cases:
        marked = ServerBackend._stale_fields(dict(rule))
        assert marked["stale"] is False, rule
        assert marked["stale_reason"] == "", rule


# ---------- WS 接线：whitelist.list 带 stale 字段、whitelist.remove 照常可删 ----------


def test_ws_whitelist_list_stale_and_remove(home, monkeypatch):
    monkeypatch.setenv("SKYSHEEP_HOME", str(home / "home"))
    from skysheep.config import db_path
    from skysheep.session.store import SessionStore

    async def seed():
        store = await SessionStore(db_path()).connect()
        try:
            project = await store.get_or_create_project(str(home / "proj"))
            await store.add_rule(project.id, "run_command", "prefix", "docker run")
            await store.add_rule(project.id, "run_command", "prefix", "git status")
            await store.add_rule(project.id, "run_command", "exact", "docker run --rm x")
        finally:
            await store.close()
        # 同 test_server.test_settings_whitelist_remove：把种子项目记成当前项目
        (home / "home").mkdir(parents=True, exist_ok=True)
        (home / "home" / "ui.json").write_text(
            json.dumps({"active_project": project.id}), encoding="utf-8")

    asyncio.run(seed())

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "whitelist.list"})
        rules = recv_until(ws, "l1")["result"]["rules"]
        assert len(rules) == 3
        # 列表条目仍带基础字段（id/tool/kind/pattern/created_at）
        assert all({"id", "tool", "kind", "pattern", "created_at"} <= set(r) for r in rules)
        by = {(r["kind"], r["pattern"]): r for r in rules}
        # docker run 两词前缀 → 失效；git status 前缀、exact → 不失效
        assert by[("prefix", "docker run")]["stale"] is True
        assert by[("prefix", "docker run")]["stale_reason"] == STALE_REASON
        assert by[("prefix", "git status")]["stale"] is False
        assert by[("exact", "docker run --rm x")]["stale"] is False

        # 失效规则也能照常删除；删完列表不再含它
        stale_id = by[("prefix", "docker run")]["id"]
        ws.send_json({"id": "d1", "method": "whitelist.remove", "params": {"id": stale_id}})
        assert recv_until(ws, "d1")["ok"]
        ws.send_json({"id": "l2", "method": "whitelist.list"})
        rest = recv_until(ws, "l2")["result"]["rules"]
        assert len(rest) == 2 and all(r["id"] != stale_id for r in rest)
        assert all(r["stale"] is False for r in rest)


# ---------- 前端接线（源码锚点，test_frontend_wiring 模式） ----------


def test_frontend_stale_badge_wiring():
    """失效徽章：stale 条目渲染「已失效」徽章，悬停 title 显示后端下发的
    stale_reason；摘要行带失效条数提示。"""
    js = read_app_bundle()
    assert "r.stale" in js, "白名单列表没有读后端的 stale 标记"
    assert "chip-warn rule-stale" in js, "失效徽章（含悬停样式钩子）缺失"
    assert 'title="${escapeHtml(r.stale_reason' in js, "徽章悬停没有接 stale_reason"
    assert ">已失效</span>" in js
    assert "条已失效" in js, "摘要行缺少失效条数提示"


def test_frontend_rules_refresh_button_wiring():
    """刷新按钮：卡片头有按钮（悬停说明用途），app.js 接线后重拉 whitelist.list
    并给状态行反馈。"""
    html = read_static("index.html")
    assert 'id="btn-rules-refresh"' in html
    assert "重新读取本项目的白名单规则" in html
    js = read_app_bundle()
    assert 'getElementById("btn-rules-refresh").onclick' in js, "刷新按钮没接线"
    i = js.index('getElementById("btn-rules-refresh").onclick')
    seg = js[i:js.index("};", i)]
    assert "renderSettings()" in seg, "刷新必须重走 renderSettings（重新拉 whitelist.list）"
    assert "showRulesStatus(" in seg, "刷新要有状态行反馈"


def test_frontend_rules_empty_state_explains_origin():
    """空态一句话说明白名单怎么来的（对话里选「总是允许」沉淀 + 手动添加）。"""
    js = read_app_bundle()
    assert "empty-hint" in js
    assert "对话中选「总是允许」后会出现在这里" in js
