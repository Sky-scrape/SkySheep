"""协作模板（L1）验收：模板册全链路与模板圆桌的对外可观察结果。

只走 WS 面（save → list → preview → remove）与 chat.send{template} 的整链，
不 mock 被测模块内部函数；模型用 FakeProvider 脚本回放，数据目录落 home 夹具。
"""

from __future__ import annotations

import pytest
from _helpers import make_client, of_kind, recv_until, rpc

from skysheep.messages import TextBlock

pytestmark = pytest.mark.acceptance

ROUNDTABLE_TEMPLATE = {
    "name": "验收圆桌模板",
    "kind": "roundtable",
    "desc": "验收用三模型评审",
    "roles": [
        {"key": "r1", "name": "评审甲", "provider": "fa", "model": "ma",
         "prompt": "你是甲，只说实话。", "needs": "read"},
        {"key": "r2", "name": "评审乙", "provider": "fb", "model": "mb",
         "prompt": "你是乙，只补事实。", "needs": "read"},
        {"key": "r3", "name": "评审丙", "provider": "fc", "model": "mc",
         "prompt": "你是丙，只给结论。", "needs": "read"},
    ],
    "budget": {"max_calls": 8, "max_seconds": 600},
}


def test_collab_template_save_list_preview_remove_chain(home):
    """保存 → 列表 → 预览 → 删除整链；内置名保留、删除后查不到。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        saved = rpc(ws, "s1", "collab.template_save",
                    {"template": ROUNDTABLE_TEMPLATE})
        assert saved["ok"], saved
        assert saved["result"]["source"] == "collab"

        listed = rpc(ws, "l1", "collab.template_list")
        templates = listed["result"]["templates"]
        names = [t["name"] for t in templates]
        assert "圆桌·多模型评审" in names
        assert names[-1] == "验收圆桌模板"
        assert next(t for t in templates
                    if t["name"] == "验收圆桌模板")["source"] == "collab"

        preview = rpc(ws, "p1", "collab.preview", {"name": "验收圆桌模板"})
        assert preview["ok"], preview
        pv = preview["result"]
        assert pv["kind"] == "roundtable" and pv["needs"] == "read"
        assert [r["name"] for r in pv["roles"]] == ["评审甲", "评审乙", "评审丙"]
        assert pv["tools"] == [] and pv["command_prefixes"] == []
        assert pv["estimated_calls"] == 4  # 3 成员 + 1 融合（模板轮主席不出草稿）

        # 内置名保留：用户另存必须改名
        builtin = rpc(ws, "s2", "collab.template_save", {
            "template": dict(ROUNDTABLE_TEMPLATE, name="圆桌·多模型评审"),
        })
        assert builtin["ok"] is False and "内置" in builtin["error"]

        removed = rpc(ws, "d1", "collab.template_remove",
                      {"name": "验收圆桌模板", "source": "collab"})
        assert removed["ok"], removed
        gone = rpc(ws, "p2", "collab.preview", {"name": "验收圆桌模板"})
        assert gone["ok"] is False and "找不到协作模板" in gone["error"]


def test_collab_template_roundtable_runs_with_roles_and_fusion(home):
    """模板圆桌跑通：角色名进事件、成员草稿落 meta、主席只做融合。"""
    scripts = [
        [[TextBlock(text="融合后的最终答案")]],  # 主模型（主席）
        [[TextBlock(text="甲的回答")]],
        [[TextBlock(text="乙的回答")]],
        [[TextBlock(text="丙的回答")]],
    ]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        saved = rpc(ws, "s1", "collab.template_save",
                    {"template": ROUNDTABLE_TEMPLATE})
        assert saved["ok"], saved

        events: list = []
        ws.send_json({"id": "t1", "method": "chat.send", "params": {
            "text": "请评审这段方案", "template": "验收圆桌模板",
        }})
        reply = recv_until(ws, "t1", events)
        assert reply["ok"], reply

        started = of_kind(events, "roundtable_started")
        assert started, "roundtable_started 必须到达"
        assert [m["name"] for m in started[0]["members"]] == [
            "评审甲", "评审乙", "评审丙"]
        assert started[0]["rounds"] == 1

        finished = of_kind(events, "roundtable_member_finished")
        assert [e["status"] for e in finished] == ["done", "done", "done"]

        result = reply["result"]["roundtable"]
        assert result["template"] == "验收圆桌模板"
        assert result["template_kind"] == "roundtable"
        assert result["chair_answers"] is False
        assert [m["name"] for m in result["members"]] == [
            "评审甲", "评审乙", "评审丙"]
        assert result["members"][1]["draft"] == "乙的回答"

        assistant = of_kind(events, "assistant_message")
        assert assistant[-1]["message"]["content"][0]["text"] == "融合后的最终答案"
