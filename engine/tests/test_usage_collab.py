"""usage.collab_report / usage.pricing_set 协议测试：协作形态判定、
今日 / 近 30 天双窗口、价目折算与保存。

形态判定依据（server/backend.py 的 _MARK_*）：圆桌 / 团队 meta 落在 assistant
消息的 roundtable 槽（团队 mode == "team"），对抗在独立的 adversarial 槽；
普通消息这些槽是 null——LIKE 标记必须带 "{"（对象开头）才不会把每条普通
消息都误判成圆桌。这里用真实 Message.model_dump_json() 构造消息 JSON，
保证落库形态与线上一致。
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from skysheep.config import db_path
from skysheep.messages import Message, TextBlock


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """隔离的 SkySheep home + 临时项目目录（与 conftest 同款；本文件自带）。"""
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()  # 价目测试要在应用启动前写 config.toml
    (tmp_path / "proj").mkdir()
    return tmp_path


def _msg_json(text, meta=None):
    """按线上持久化形态构造 assistant 消息 JSON（含 null 的元数据槽）。"""
    m = Message.assistant([TextBlock(text=text)])
    for key, value in (meta or {}).items():
        setattr(m, key, value)
    return m.model_dump_json()


def _seed(home, rows):
    """直接向 skysheep.db 写会话 / 消息 / 用量行（应用启动、schema 已建后调）。"""

    async def go():
        db = await aiosqlite.connect(db_path())
        try:
            # 应用启动时已把工作目录登记成项目（scoped 聚合按 s.project_id 过滤），
            # 种子会话必须挂到这个项目上才进得了报表
            cur = await db.execute("SELECT id FROM projects ORDER BY id LIMIT 1")
            prow = await cur.fetchone()
            pid = prow[0] if prow else None
            for r in rows:
                ts = r.get("ts", time.time())
                await db.execute(
                    "INSERT OR IGNORE INTO sessions (id, project_id, title, summary,"
                    " tags, pinned, archived, memory_digested, created_at, updated_at)"
                    " VALUES (?, ?, ?, '', '', 0, 0, 0, ?, ?)",
                    (r["sid"], pid, r["title"], ts, ts),
                )
                for i, content in enumerate(r.get("messages", []), start=1):
                    await db.execute(
                        "INSERT INTO messages (session_id, seq, role, content, created_at)"
                        " VALUES (?, ?, 'assistant', ?, ?)",
                        (r["sid"], i, content, ts),
                    )
                for u in r.get("usage", []):
                    await db.execute(
                        "INSERT INTO usage_log (session_id, provider, model, ts,"
                        " in_tokens, out_tokens, cached_tokens)"
                        " VALUES (?, ?, 'model-x', ?, ?, ?, ?)",
                        (r["sid"], u.get("provider", "prov-a"),
                         u["ts"], u["in"], u["out"], u.get("cached", 0)),
                    )
            await db.commit()
        finally:
            await db.close()

    asyncio.run(go())


def _touch_store(ws):
    """让后端先把 store 连上（connect 在启动路径；此调用兜底触发）。"""
    from test_server import recv_until

    ws.send_json({"id": "touch", "method": "session.list", "params": {}})
    assert recv_until(ws, "touch")["ok"]


def _collab(ws, rid, **params):
    from test_server import recv_until

    ws.send_json({"id": rid, "method": "usage.collab_report", "params": params})
    frame = recv_until(ws, rid)
    assert frame["ok"], frame
    return frame["result"]


def test_collab_report_forms_and_windows(home):
    """四种形态归位 + 近 30 天 / 今日双窗口 + 未配价目时只报 token。"""
    from test_server import make_client

    now = time.time()
    rows = [
        # 普通会话：roundtable 槽是 null（线上普通消息的真实形态）；两行入账，
        # 今天一行 + 前天一行——近 30 天全算，今日只算前者
        {"sid": "s-normal", "title": "普通会话",
         "messages": [_msg_json("你好")],
         "usage": [{"ts": now, "in": 100, "out": 50, "cached": 10},
                   {"ts": now - 2 * 86400, "in": 200, "out": 100}]},
        {"sid": "s-rt", "title": "圆桌会话",
         "messages": [_msg_json("融合回答", {"roundtable": {"mode": "roundtable"}})],
         "usage": [{"ts": now, "in": 1000, "out": 500}]},
        {"sid": "s-team", "title": "团队会话",
         "messages": [_msg_json("交付说明", {"roundtable": {"mode": "team", "team": {}}})],
         "usage": [{"ts": now, "in": 2000, "out": 800}]},
        {"sid": "s-adv", "title": "对抗会话",
         "messages": [_msg_json("审查报告", {"adversarial": {"mode": "adversarial"}})],
         "usage": [{"ts": now - 40 * 86400, "in": 999, "out": 999},
                   {"ts": now, "in": 300, "out": 100}]},
    ]
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        _touch_store(ws)
        _seed(home, rows)
        r = _collab(ws, "uc1", days=30)
        assert r["days"] == 30
        # 未配价目：不计价，界面据此只展示 token
        assert r["has_price"] is False
        assert r["pricing"] == {"price_in": 0.0, "price_out": 0.0, "price_cache": 0.0}
        f = r["forms"]
        assert f["normal"]["in"] == 300 and f["normal"]["out"] == 150
        assert f["normal"]["turns"] == 2 and f["normal"]["sessions"] == 1
        assert f["roundtable"]["in"] == 1000 and f["roundtable"]["sessions"] == 1
        assert f["team"]["in"] == 2000 and f["team"]["sessions"] == 1
        # 对抗会话 40 天前那行落在 30 天窗外，不计入
        assert f["adversarial"]["in"] == 300 and f["adversarial"]["turns"] == 1
        t = r["today"]
        assert t["normal"]["in"] == 100 and t["normal"]["turns"] == 1
        assert t["normal"]["cached"] == 10
        assert t["roundtable"]["in"] == 1000 and t["team"]["in"] == 2000
        assert t["adversarial"]["in"] == 300
        # 项目态下发按会话明细（B14：无项目态才隐藏）：形态逐会话可对
        assert {s["sid"]: s["form"] for s in r["by_session"]} == {
            "s-normal": "normal", "s-rt": "roundtable",
            "s-team": "team", "s-adv": "adversarial",
        }


def test_collab_report_mixed_form_priority(home):
    """同会话既有对抗又有圆桌标记：固定优先级判对抗（整会话归一类，不硬拆）。"""
    from test_server import make_client

    now = time.time()
    rows = [{"sid": "s-mix", "title": "混合会话",
             "messages": [_msg_json("圆桌融合", {"roundtable": {"mode": "roundtable"}}),
                          _msg_json("审查报告", {"adversarial": {"mode": "adversarial"}})],
             "usage": [{"ts": now, "in": 500, "out": 250}]}]
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        _touch_store(ws)
        _seed(home, rows)
        r = _collab(ws, "uc2")
        assert r["forms"]["adversarial"]["in"] == 500
        assert r["forms"]["roundtable"]["in"] == 0
        assert r["forms"]["normal"]["in"] == 0


def test_collab_report_cost_with_pricing(home):
    """[usage_pricing] 配了价目才折算费用：默认价 + 缓存拆算 + 按服务覆盖。"""
    from test_server import make_client

    # 启动前落 config.toml（load_config 在后端装配时读）
    cfg = home / "home" / "config.toml"
    cfg.write_text(
        "[usage_pricing]\n"
        "price_in = 10.0\n"
        "price_out = 20.0\n"
        "price_cache = 1.0\n"
        "\n"
        "[usage_pricing.providers.prov-b]\n"
        "price_in = 100.0\n"
        "price_out = 0.0\n",
        encoding="utf-8",
    )
    now = time.time()
    rows = [
        # 默认价 + 缓存命中拆算：
        # (1M-200k)/1M*10 + 200k/1M*1 + 100k/1M*20 = 8 + 0.2 + 2 = 10.2 元
        {"sid": "s-a", "title": "会话A",
         "messages": [_msg_json("hi")],
         "usage": [{"ts": now, "in": 1_000_000, "out": 100_000, "cached": 200_000}]},
        # prov-b 按服务覆盖：1M/1M*100 + 50k/1M*0 = 100 元
        {"sid": "s-b", "title": "会话B",
         "messages": [_msg_json("hi")],
         "usage": [{"provider": "prov-b", "ts": now, "in": 1_000_000, "out": 50_000}]},
    ]
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        _touch_store(ws)
        _seed(home, rows)
        r = _collab(ws, "uc3")
        assert r["has_price"] is True
        assert r["pricing"]["price_in"] == 10.0
        assert r["forms"]["normal"]["cost"] == pytest.approx(110.2, abs=1e-3)
        # 两行都在今天：今日口径费用与 30 天口径一致
        assert r["today"]["normal"]["cost"] == pytest.approx(110.2, abs=1e-3)


def test_usage_pricing_set_via_ws(home):
    """usage.pricing_set：保存回传新价目、真落盘 config.toml、报表热生效、负数被拒。"""
    from test_server import make_client, recv_until

    from skysheep.config import load_config

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        _touch_store(ws)
        ws.send_json({"id": "ps1", "method": "usage.pricing_set",
                      "params": {"price_in": 3.5, "price_out": 7, "price_cache": 0.5}})
        r = recv_until(ws, "ps1")
        assert r["ok"], r
        assert r["result"] == {"price_in": 3.5, "price_out": 7.0,
                               "price_cache": 0.5, "has_price": True}
        # 真落盘（_read_raw_config/_write_raw_config 路径，load_config 读得回）
        cfg = load_config()
        assert cfg.usage_pricing.price_in == 3.5
        assert cfg.usage_pricing.price_out == 7.0
        assert cfg.usage_pricing.price_cache == 0.5
        # 报表即时反映新价目（保存后热生效）
        res = _collab(ws, "uc4")
        assert res["has_price"] is True and res["pricing"]["price_in"] == 3.5
        # 非法值（负数）要报错，不静默夹取
        ws.send_json({"id": "ps2", "method": "usage.pricing_set",
                      "params": {"price_in": -1}})
        assert recv_until(ws, "ps2")["ok"] is False
