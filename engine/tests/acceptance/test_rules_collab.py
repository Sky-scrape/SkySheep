"""D 组 · 多模型协作验收：圆桌 / 团队 / 对抗的对外可观察结果。

重点不是内部编排细节，而是「用户切换了协作模式后，事件流、消息元数据与
落库结果是否对得上」。四模式的逐分支单测在 tests/test_roundtable.py、
test_team.py、test_adversarial.py、test_relay_*.py，这里只走整链。
"""

from __future__ import annotations

import json

import pytest
from _helpers import kinds, make_client, of_kind, rpc, run_turn

from skysheep.messages import TextBlock

pytestmark = pytest.mark.acceptance

FINDINGS = json.dumps({
    "findings": [
        {"category": "bug", "severity": "high", "location": "a.py:10",
         "description": "空列表调用 pop 会越界", "evidence": "没有判空"},
        {"category": "security", "severity": "critical", "location": "b.py:3",
         "description": "拼接 SQL", "evidence": "格式化字符串进查询"},
    ]
}, ensure_ascii=False)
VERDICTS = json.dumps({
    "verdicts": [
        {"id": "F-001", "verdict": "confirmed", "reason": "确实没有判空"},
        {"id": "F-002", "verdict": "refuted", "reason": "参数化查询已兜底"},
    ]
}, ensure_ascii=False)
SOLUTIONS = json.dumps({
    "solutions": [
        {"id": "F-001", "fix": "先判空再取元素", "risk": "低", "priority": "P0"},
    ]
}, ensure_ascii=False)
REPORT = "## 对抗审查报告\n\n成立 1 条：a.py:10 空列表越界。"


def test_d1_collab_modes_are_mutually_exclusive(home):
    """D1 同轮互斥：team + roundtable 直接拒绝；活动团队期间不能再开圆桌。"""
    scripts = [
        [[TextBlock(text="主模型回答")]],
        [[TextBlock(text="成员汇报")]],
    ]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        bad = rpc(ws, "m1", "chat.send", {
            "text": "问题", "team": True, "roundtable": True,
            "members": [{"provider": "fa", "model": "ma"}],
        })
        assert bad["ok"] is False and "不能同时开启" in bad["error"]

        # 空态：没有团队时工单方法给可读错误，而不是静默
        empty = rpc(ws, "g0", "team.get")
        assert empty["result"]["active"] is False and empty["result"]["team"] is None
        err = rpc(ws, "e1", "team.task_add", {"title": "t", "assignee": "x"})
        assert err["ok"] is False and "没有进行中的团队" in err["error"]

        # 建队后：同会话再开圆桌被挡（提示先收队）
        team = rpc(ws, "m2", "chat.send", {
            "text": "@小研 开工", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研"}],
        })
        assert team["ok"], team
        sid = team["result"]["session_id"]
        blocked = rpc(ws, "m3", "chat.send", {
            "text": "换个开法", "roundtable": True, "session_id": sid,
        })
        assert blocked["ok"] is False and "请先收队" in blocked["error"]


def test_d2_team_board_lifecycle_and_delivery(home):
    """D2 团队：建队 → 登记工单 → 派工 → 验收通过 → 交付，频道留全程记录。"""
    scripts = [
        [[TextBlock(text="本轮团队纪要")]],   # 主模型
        [[TextBlock(text="小研的汇报")]],     # 队员
    ]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        r1 = rpc(ws, "r1", "chat.send", {
            "text": "@小研 先做调研", "team": True,
            "members": [{"provider": "fa", "model": "ma", "name": "小研", "persona": "研究员"}],
        })
        assert r1["ok"], r1
        sid = r1["result"]["session_id"]
        meta = r1["result"]["team"]
        assert meta["mode"] == "team" and meta["team"]["status"] == "active"
        team_id = meta["team"]["team_id"]

        added = rpc(ws, "k1", "team.task_add", {
            "title": "写调研报告", "assignee": "小研", "type": "exec", "accept": "有结论",
        })["result"]["task"]
        tid = added["id"]
        assert added["status"] == "pending"

        dispatched = rpc(ws, "k2", "team.task_update",
                         {"task_id": tid, "status": "in_progress"})["result"]
        assert dispatched["task"]["status"] == "in_progress"
        assert dispatched["assign_message"]["msg_kind"] == "assign"

        for status in ("review", "done"):
            out = rpc(ws, f"k-{status}", "team.task_update",
                      {"task_id": tid, "status": status})["result"]
            assert out["task"]["status"] == status

        board = rpc(ws, "g1", "team.get", {"session_id": sid})["result"]
        assert board["active"] is True
        assert board["team"]["tasks"][0]["status"] == "done"

        delivered = rpc(ws, "d1", "team.deliver", {"session_id": sid})["result"]
        assert delivered["status"] == "done"
        assert "《交付说明》" in delivered["summary"]
        assert delivered["team"]["status"] == "done"

        log = rpc(ws, "lg", "team.log", {"session_id": sid, "team_id": team_id})["result"]
        bodies = [m.get("text", "") for m in log["messages"]]
        assert any("先做调研" in b for b in bodies), "用户指令要进频道"
        assert any("写调研报告" in b for b in bodies), "派工要进频道"

        # 交付后回到普通回合
        normal = rpc(ws, "r2", "chat.send", {"text": "收尾", "session_id": sid})
        assert normal["ok"] and normal["result"]["team"] is None


def test_d3_adversarial_four_stage_report(home):
    """D3 对抗：发现 → 调查 → 建议 → 裁判四阶段，终审报告的统计与裁决正确。"""
    scripts = [
        [[TextBlock(text=REPORT)]],      # 裁判（主模型）
        [[TextBlock(text=FINDINGS)]],    # 发现者
        [[TextBlock(text=VERDICTS)]],    # 调查者
        [[TextBlock(text=SOLUTIONS)]],   # 建议者
    ]
    with make_client(home, scripts) as client, client.websocket_connect("/ws") as ws:
        frame, evs = run_turn(ws, "a1", {
            "text": "审查这段代码：print(pop([]))",
            "adversarial": True,
            "members": [
                {"provider": "fa", "model": "ma"},
                {"provider": "fb", "model": "mb"},
                {"provider": "fc", "model": "mc"},
            ],
        })
        assert frame["ok"] and frame["result"]["done"]
        kinds_seen = kinds(evs)
        assert "adversarial_started" in kinds_seen
        assert kinds_seen.count("adversarial_finding_proposed") == 2
        assert kinds_seen.count("adversarial_verdict") == 2

        meta = frame["result"]["adversarial"]
        assert meta["mode"] == "adversarial" and meta["status"] == "done"
        assert meta["stats"]["total"] == 2
        assert meta["stats"]["confirmed"] == 1
        assert meta["stats"]["refuted"] == 1
        assert [f["verdict"] for f in meta["findings"]] == ["confirmed", "refuted"]
        report = "".join(
            block.get("text", "")
            for data in of_kind(evs, "assistant_message")
            for block in data["message"]["content"]
            if block.get("type") == "text"
        )
        assert "对抗审查报告" in report
        assert frame["result"]["adversarial_seq"] > 0, "报告落库后应回填消息序号"
