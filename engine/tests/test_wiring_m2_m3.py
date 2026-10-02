"""接线层测试（记忆二期 + 无人值守三期）：WS 方法分支与前端静态接线。

引擎层的能力各有自己的测试文件（候选抽取/去重/采纳在 test_memory_distill.py，
webhook 适配器与日报汇总在 test_webhook_channel.py / test_daily_report.py）；
这里只锁「协议与前端接上、且挂对了守卫」这一层：

- WS 层用既有 make_client 模式（test_server.py 同款 TestClient + websocket）；
- 前端接线走 test_frontend_wiring 模式（读拼接后的前端源码断言接线存在）。

安全相关断言：新增方法凡属「看对话摘录 / 改记忆 / 开无人值守推送」的，
必须在 LOCAL_ONLY_METHODS 里被远端拒绝；开关类只读布尔的方法远端可调。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from conftest import read_app_bundle
from fastapi.testclient import TestClient

from skysheep.channels.webhook import SIGNATURE_HEADER
from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider
from skysheep.server import create_app
from skysheep.tools.memory_distill import (
    DISTILL_STATE_FILE,
    add_candidates,
)


def make_client(home, script, provider=None):
    """provider 可注入预构建的 FakeProvider（test_server.py 同款）。"""
    provider = provider or FakeProvider(script)
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: provider,
    )
    return TestClient(app)


def _ws_call(ws, mid, method, params=None):
    ws.send_json({"id": mid, "method": method, "params": params or {}})
    while True:
        frame = ws.receive_json()
        if frame.get("id") == mid:
            return frame


# ---------- 记忆二期：memory.candidates / candidate_adopt / candidate_ignore / distill_save ----------


def test_memory_candidate_ws_roundtrip(home):
    """开关读写 → 待审列表 → 采纳进全局记忆 → 忽略防复提，整条链在 WS 层走通。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "m1", "memory.distill_save", {"enabled": True})
        assert frame["ok"] and frame["result"]["enabled"] is True

        frame = _ws_call(ws, "m2", "memory.candidates")
        assert frame["result"] == {"enabled": True, "pending": []}

        # 直接走引擎层塞两条候选（抽取侧由 test_memory_distill.py 覆盖）
        added = add_candidates(["用户团队用 uv 管理 Python 依赖", "用户项目都放在 D 盘"])
        assert len(added) == 2

        frame = _ws_call(ws, "m3", "memory.candidates")
        pending = frame["result"]["pending"]
        assert [e["text"] for e in pending] == [
            "用户团队用 uv 管理 Python 依赖", "用户项目都放在 D 盘",
        ]

        frame = _ws_call(ws, "m4", "memory.candidate_adopt",
                         {"candidate_id": pending[0]["id"]})
        assert frame["ok"] and frame["result"]["adopted"] is True
        frame = _ws_call(ws, "m5", "memory.candidate_ignore",
                         {"candidate_id": pending[1]["id"]})
        assert frame["ok"] and frame["result"]["ignored"] is True

        frame = _ws_call(ws, "m6", "memory.candidates")
        assert frame["result"]["pending"] == []
        # 采纳的候选已落 memory.md（引擎自有文件，恒 UTF-8）
        mem_text = (home / "home" / "memory.md").read_text(encoding="utf-8")
        assert "用户团队用 uv 管理 Python 依赖" in mem_text
        # 待审状态文件确实写在了隔离的 SKYSHEEP_HOME 下
        assert (home / "home" / DISTILL_STATE_FILE).exists()


def test_memory_distill_save_invalid_params_tolerated(home):
    """开关方法的参数宽容度：缺 enabled 按关处理，不 500。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "m1", "memory.distill_save", {})
        assert frame["ok"] and frame["result"]["enabled"] is False


def test_memory_candidate_ws_local_guards(home, monkeypatch):
    """远端守卫：候选列表/采纳/忽略仅本机；总闸只切布尔，远端可调。"""
    from skysheep.server import app as server_app

    monkeypatch.setattr(server_app, "_client_is_local", lambda ws: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "r1", "memory.candidates")
        assert frame["ok"] is False and "本机" in frame["error"]
        frame = _ws_call(ws, "r2", "memory.candidate_adopt", {"candidate_id": "x"})
        assert frame["ok"] is False and "本机" in frame["error"]
        frame = _ws_call(ws, "r3", "memory.candidate_ignore", {"candidate_id": "x"})
        assert frame["ok"] is False and "本机" in frame["error"]
        frame = _ws_call(ws, "r4", "memory.distill_save", {"enabled": True})
        assert frame["ok"] is True and frame["result"]["enabled"] is True


def test_turn_distill_hook_fires_after_turn(home):
    """轮末挂点（backend._run_turn_pipeline → schedule_turn_distill）真实生效：
    开关打开后跑一轮对话，候选在轮次收尾后被抽进待审列表。

    用户消息 ≥300 字（过 DIGEST_MIN_CHARS 的寒暄阈值）；provider 脚本第一组
    是轮次回复、第二组是候选清单。沉淀是 spawn_bg 的后台任务：轮次回复到达
    后轮询待审列表等它落地。
    """
    long_text = (
        "我们团队的 Python 依赖统一用 uv 管理，项目都放在 D 盘的 repos 目录下，"
        "以后生成的脚本与文档请默认遵循这两条约定。" + "补充一些背景，让正文过提炼阈值。" * 20
    )
    distill_reply = "- 用户团队用 uv 管理 Python 依赖\n- 用户项目都放在 D 盘"
    provider = FakeProvider([
        [TextBlock(text="好的，记下了")],
        [TextBlock(text=distill_reply)],
    ])
    with make_client(home, [], provider=provider) as client:
        with client.websocket_connect("/ws") as ws:
            assert _ws_call(ws, "s0", "memory.distill_save", {"enabled": True})["ok"]
            frame = _ws_call(ws, "s1", "chat.send", {"text": long_text})
            assert frame["ok"] is True
            # 后台沉淀是异步的：轮询待审列表等它落地
            pending: list[dict] = []
            for i in range(50):
                fr = _ws_call(ws, f"p{i}", "memory.candidates")
                pending = fr["result"]["pending"]
                if pending:
                    break
                time.sleep(0.1)
            assert [e["text"] for e in pending] == [
                "用户团队用 uv 管理 Python 依赖", "用户项目都放在 D 盘",
            ]
            assert len(provider.calls) == 2  # 轮次一次 + 沉淀一次


# ---------- 无人值守三期：automation.daily_report_* ----------


def test_daily_report_ws_roundtrip(home):
    """日报设置：默认关 → 保存开 + 时刻 → 状态回读；非法时刻报错不落盘。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "d1", "automation.daily_report_status")
        assert frame["ok"]
        assert frame["result"] == {"enabled": False, "time": "09:00", "last_sent_date": ""}

        frame = _ws_call(ws, "d2", "automation.daily_report_save",
                         {"enabled": True, "time": "07:30"})
        assert frame["ok"]
        assert frame["result"] == {"enabled": True, "time": "07:30", "last_sent_date": ""}

        frame = _ws_call(ws, "d3", "automation.daily_report_save", {"time": "25:00"})
        assert frame["ok"] is False and "HH:MM" in frame["error"]
        # 非法时刻没有写脏状态：回读仍是上一次的合法值
        frame = _ws_call(ws, "d4", "automation.daily_report_status")
        assert frame["result"]["time"] == "07:30"
        # 引擎自有状态文件落在隔离的 SKYSHEEP_HOME，且是合法 JSON（原子写的产物）
        raw = json.loads((home / "home" / "daily_report.json").read_text(encoding="utf-8"))
        assert raw == {"enabled": True, "time": "07:30", "last_sent_date": ""}


def test_daily_report_ws_local_guards(home, monkeypatch):
    """远端守卫：开日报属无人值守推送面，仅本机；状态回读各端可看。"""
    from skysheep.server import app as server_app

    monkeypatch.setattr(server_app, "_client_is_local", lambda ws: False)
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "r1", "automation.daily_report_status")
        assert frame["ok"] is True and frame["result"]["enabled"] is False
        frame = _ws_call(ws, "r2", "automation.daily_report_save", {"enabled": True})
        assert frame["ok"] is False and "本机" in frame["error"]


# ---------- 无人值守三期：渠道设置里的 Webhook 卡片（channel.* 对 webhook 的路由） ----------


class _Collector:
    """127.0.0.1 上的临时 HTTP server：验证「发送测试推送」真的出了网。"""

    def __init__(self):
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append({
                    "body": self.rfile.read(length),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                })
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_args):  # 静音默认访问日志
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/hook"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_webhook_channel_ws_roundtrip(home):
    """Webhook 卡片的全链路：状态出卡片字段 → 未填 URL 启用被拒 → 保存 URL/密钥
    → 启用 → 测试推送带 HMAC 签名头真实送达本机临时端点。"""
    sink = _Collector()
    try:
        with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
            frame = _ws_call(ws, "w1", "channel.status")
            assert frame["ok"]
            assert "webhook" in frame["result"]["supported"]
            card = next(c for c in frame["result"]["channels"] if c["name"] == "webhook")
            assert card["has_url"] is False and card["has_secret"] is False

            # 未填 URL 不许启用（报错文案指路，不静默）
            frame = _ws_call(ws, "w2", "channel.enable", {"name": "webhook"})
            assert frame["ok"] is False and "URL" in frame["error"]

            frame = _ws_call(ws, "w3", "channel.save",
                             {"name": "webhook", "url": sink.url, "secret": "s1"})
            assert frame["ok"]
            card = next(c for c in frame["result"]["channels"] if c["name"] == "webhook")
            assert card["has_url"] is True and card["has_secret"] is True
            # 凭据不回显，只回「已填」
            assert "url" not in card and "secret" not in card

            frame = _ws_call(ws, "w4", "channel.enable", {"name": "webhook"})
            assert frame["ok"]
            card = next(c for c in frame["result"]["channels"] if c["name"] == "webhook")
            assert card["enabled"] is True

            # 纯出站渠道的「发送测试推送」：无需 chat_id
            frame = _ws_call(ws, "w5", "channel.test", {"name": "webhook"})
            assert frame["ok"] and frame["result"]["ok"] is True
            deadline = time.time() + 5
            while not sink.requests and time.time() < deadline:
                time.sleep(0.05)
            assert sink.requests, "测试推送没有到达本机接收端"
            req = sink.requests[0]
            assert req["headers"].get("content-type") == "application/json"
            sig = req["headers"].get(SIGNATURE_HEADER.lower()) or ""
            assert sig.startswith("sha256=")
            body = json.loads(req["body"].decode("utf-8"))
            assert body["channel"] == "webhook"
            assert "测试" in body["text"]
    finally:
        sink.close()


def test_webhook_channel_test_unreachable_reports_failure(home):
    """端点不可达时的「发送测试推送」：WS 层不抛，失败原因经 result.error 透出
    （前端 channelMsg 就地显示，而非无声失败）。保存的 URL 未启用也允许先试发。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        frame = _ws_call(ws, "w1", "channel.save",
                         {"name": "webhook", "url": "http://127.0.0.1:9/hook"})
        assert frame["ok"]
        frame = _ws_call(ws, "w2", "channel.test", {"name": "webhook"})
        assert frame["ok"] is True, "方法本身不该抛错"
        assert frame["result"]["ok"] is False
        assert frame["result"]["error"], "失败原因要带给前端"


# ---------- 前端静态接线（test_frontend_wiring 模式） ----------


def test_frontend_memory_candidates_wiring():
    """记忆候选区接在记忆地图面板（app-memmap.js），空列表折叠不显眼。"""
    js = read_app_bundle()
    # WS 方法四件套全部接上
    for method in ("memory.candidates", "memory.candidate_adopt",
                   "memory.candidate_ignore", "memory.distill_save"):
        assert f'"{method}"' in js, f"前端没有接 {method}"
    # 候选区渲染在 app-memmap.js（记忆地图分区），入口元素在 index.html
    assert "function loadMemoryCandidates" in js
    assert "function renderMemoryCandidates" in js
    assert "loadMemoryCandidates();" in js, "记忆地图加载时没有拉候选"
    # 空列表折叠不显眼：没开沉淀且没有候选时整块隐藏
    assert "if (!n && !distillState.enabled)" in js
    assert 'box.classList.add("hidden")' in js
    html = read_static_html()
    assert 'id="map-distill"' in html and 'id="map-distill-body"' in html
    css = read_static_css()
    assert ".map-distill" in css


def test_frontend_webhook_card_wiring():
    """渠道设置里 Webhook 卡片：URL + 可选签名密钥 + 测试推送，名单区不出现。"""
    js = read_app_bundle()
    assert 'webhook: "Webhook"' in js, "CHANNEL_LABEL 缺 webhook"
    assert 'class="channel-input channel-url"' in js
    assert 'class="channel-input channel-secret"' in js
    assert 'c.has_url ? "已保存（留空则不修改）"' in js
    assert 'c.has_secret ? "已保存（留空则不修改）"' in js
    # 保存回调把 URL / 密钥回传后端（留空 = 不修改）
    assert "payload.url = urlEl.value.trim()" in js
    assert "payload.secret = secretEl.value.trim()" in js
    # 测试推送分支：webhook 无需 chat_id，直接 channel.test
    assert 'if (name === "webhook")' in js
    assert "发送测试推送" in js
    # 无入站名单语义：webhook 卡片不渲染允许名单文本域（isWebhook 分流）
    assert "const isWebhook = c.name === \"webhook\"" in js


def test_frontend_daily_report_row_wiring():
    """自动化页的日报开关行：控件在 index.html，读写走 automation.daily_report_*。"""
    js = read_app_bundle()
    assert '"automation.daily_report_status"' in js
    assert '"automation.daily_report_save"' in js
    assert "function loadDailyReportRow" in js
    assert "function saveDailyReportRow" in js
    # 两个分段（定时任务 / 任务编排）打开时都刷新这行
    assert js.count("loadDailyReportRow(); //") == 2, "日报行应挂在 loadCron 与 loadPipelines 两处"
    html = read_static_html()
    assert 'id="daily-report-row"' in html
    assert 'id="daily-report-toggle"' in html
    assert 'id="daily-report-time"' in html
    assert "每日运行日报" in html
    css = read_static_css()
    assert ".rp-daily" in css and "#daily-report-time" in css


def read_static_html() -> str:
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static" / "index.html"
    return p.read_text(encoding="utf-8")


def read_static_css() -> str:
    from pathlib import Path

    p = Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static" / "app.css"
    return p.read_text(encoding="utf-8")
