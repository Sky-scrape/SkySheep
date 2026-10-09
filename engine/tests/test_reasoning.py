"""思考强度：档位映射、配置落盘与收敛、WS 端到端热生效。"""

from __future__ import annotations

import asyncio

import pytest

from skysheep.config import (
    REASONING_EFFORT_LABELS,
    REASONING_EFFORTS,
    ConfigError,
    ProviderConfig,
    load_config,
    update_provider_in_config,
)
from skysheep.models.anthropic_provider import AnthropicProvider
from skysheep.models.factory import build_provider
from skysheep.models.openai_compat import OpenAICompatProvider


class FakeOpenAIStream:
    """模拟 openai AsyncStream：async-with 退出即置 closed（与 SDK __aexit__ 一致）。"""

    def __init__(self, chunks=()) -> None:
        self._chunks = list(chunks)
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()
        return False

    def __aiter__(self):
        async def _gen():
            for c in self._chunks:
                yield c

        return _gen()

    async def close(self):
        self.closed = True


def _empty_openai_client(captured: dict):
    class FakeCompletions:
        async def create(self, **params):
            captured.update(params)
            return FakeOpenAIStream()

    class FakeClient:
        class chat:  # noqa: N801
            completions = FakeCompletions()

    return FakeClient()


def _empty_anthropic_client(captured: dict):
    class FakeMessages:
        def stream(self, **kwargs):
            captured.update(kwargs)

            class _Stream:
                async def __aenter__(self):
                    class _Iter:
                        def __aiter__(self):
                            async def _gen():
                                if False:  # pragma: no cover
                                    yield None
                            return _gen()

                    return _Iter()

                async def __aexit__(self, *a):
                    return False

            return _Stream()

    class FakeClient:
        messages = FakeMessages()

    return FakeClient()


def _consume(provider) -> None:
    async def go():
        async for _ in provider.stream([], []):
            pass

    asyncio.new_event_loop().run_until_complete(go())


def test_effort_constants():
    assert REASONING_EFFORTS == ("auto", "low", "medium", "high")
    assert set(REASONING_EFFORT_LABELS) == set(REASONING_EFFORTS)


def test_provider_defaults_to_auto_and_normalizes():
    p = OpenAICompatProvider("x", "m", "k")
    assert p.reasoning_effort == "auto" and p.supports_reasoning is False
    assert p.set_reasoning_effort("HIGH") == "high"
    assert p.set_reasoning_effort("Medium") == "medium"
    assert p.set_reasoning_effort("bogus") == "auto"  # 非法值回落自动
    assert p.set_reasoning_effort(None) == "auto"


def test_openai_payload_includes_effort_only_when_supported():
    """请求体只在声明支持且非 auto 时带 reasoning_effort。"""
    captured: dict = {}
    p = OpenAICompatProvider("x", "m", "k", client=_empty_openai_client(captured))
    p.supports_reasoning = True

    _consume(p)
    assert "reasoning_effort" not in captured, "auto 档不传参，保持服务默认"

    p.set_reasoning_effort("high")
    _consume(p)
    assert captured.get("reasoning_effort") == "high"


def test_unsupported_provider_ignores_effort():
    """未声明支持时即使设了档位也不传参（不给不支持的服务发无效参数）。"""
    captured: dict = {}
    p = OpenAICompatProvider("x", "m", "k", client=_empty_openai_client(captured))
    p.supports_reasoning = False
    p.set_reasoning_effort("high")
    _consume(p)
    assert "reasoning_effort" not in captured


def test_anthropic_thinking_budget_mapping():
    """anthropic 档位 → thinking.budget_tokens；auto 不启用。"""
    captured: dict = {}
    p = AnthropicProvider("a", "claude", "k", client=_empty_anthropic_client(captured))
    p.supports_reasoning = True

    for effort, budget in (("auto", None), ("low", 4096), ("medium", 10240), ("high", 24576)):
        captured.clear()
        p.set_reasoning_effort(effort)
        _consume(p)
        if budget is None:
            assert "thinking" not in captured, "auto 档不启用扩展思考"
        else:
            assert captured["thinking"] == {"type": "enabled", "budget_tokens": budget}


def test_config_persists_and_validates_effort(home):
    update_provider_in_config("deepseek", reasoning_effort="high")
    cfg = load_config()
    assert cfg.providers["deepseek"].reasoning_effort == "high"
    # 默认对所有服务提供该能力（auto 档不传参，无副作用）；可在设置里显式关闭
    assert cfg.providers["anthropic"].supports_reasoning is True
    assert cfg.providers["deepseek"].supports_reasoning is True
    assert cfg.providers["ollama"].supports_reasoning is True

    # 显式关闭后落盘、可被读回
    update_provider_in_config("ollama", supports_reasoning=False)
    assert load_config().providers["ollama"].supports_reasoning is False

    with pytest.raises(ConfigError):
        update_provider_in_config("deepseek", reasoning_effort="extreme")


def test_factory_applies_effort(home):
    cfg = ProviderConfig(kind="openai", base_url="http://x", api_key="k",
                         model="m", supports_reasoning=True, reasoning_effort="low")
    p = build_provider("custom", cfg)
    assert p.supports_reasoning is True and p.reasoning_effort == "low"

    # 显式关闭 → 控件隐藏且不传参
    cfg2 = ProviderConfig(kind="openai", base_url="http://x", api_key="k", model="m",
                          supports_reasoning=False)
    p2 = build_provider("custom2", cfg2)
    assert p2.supports_reasoning is False and p2.reasoning_effort == "auto"


# ---- WS：状态查询 + 设置热生效 ----


def test_reasoning_state_via_ws(home):
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "model.reasoning", "params": {}})
        st = recv_until(ws, "r1")["result"]["reasoning"]
        assert st["efforts"] == ["auto", "low", "medium", "high"]
        assert st["labels"]["auto"] == "自动"
        # 注入的 fake provider 未声明能力 → 控件隐藏（可用性以 provider 实例为准）
        assert st["supported"] is False
        assert "deepseek" in st["providers"]

        # 不支持时明确报错，而不是静默无效
        ws.send_json({"id": "s1", "method": "model.set_reasoning",
                      "params": {"effort": "high"}})
        fail = recv_until(ws, "s1")
        assert not fail["ok"] and "不支持" in fail["error"]

        # 内置预设默认可用 → 成功并落盘
        ws.send_json({"id": "s2", "method": "model.set_reasoning",
                      "params": {"effort": "high", "provider": "deepseek"}})
        ok = recv_until(ws, "s2")
        assert ok["ok"], ok.get("error")
        assert ok["result"]["reasoning"]["providers"]["deepseek"]["effort"] == "high"
        assert load_config().providers["deepseek"].reasoning_effort == "high"

        # 非法档位拒绝
        ws.send_json({"id": "s3", "method": "model.set_reasoning",
                      "params": {"effort": "extreme", "provider": "deepseek"}})
        bad = recv_until(ws, "s3")
        assert not bad["ok"]


def test_set_reasoning_applies_to_current_provider(home):
    """当前服务可调档时，调档立即生效到正在用的 provider（下一轮即生效）。"""
    from test_server import make_client, recv_until

    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([[TextBlock(text="好")]])
    provider.supports_reasoning = True
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "model.reasoning", "params": {}})
        assert recv_until(ws, "r1")["result"]["reasoning"]["supported"] is True

        ws.send_json({"id": "s1", "method": "model.set_reasoning",
                      "params": {"effort": "medium"}})
        ok = recv_until(ws, "s1")
        assert ok["ok"], ok.get("error")
        assert ok["result"]["reasoning"]["effort"] == "medium"

        ws.send_json({"id": "r2", "method": "model.reasoning", "params": {}})
        assert recv_until(ws, "r2")["result"]["reasoning"]["effort"] == "medium"


def test_set_reasoning_skips_private_sessions(home):
    """全局调档不对模型/档位私有的会话施为（blocking② 第三条腿的协议锁）：
    覆盖腿与档位腿的会话挂着各自的 detached provider——对象不被换、档位不被
    改；无覆盖会话与基底照旧跟全局换档。走真实构建（factory 注入会返回同一
    实例，分不出对象身份）。"""
    from fastapi.testclient import TestClient
    from test_server import recv_until

    from skysheep.config import add_provider_to_config
    from skysheep.server import create_app

    add_provider_to_config("skp-on", kind="openai", base_url="http://x",
                           model="skp-1", api_key="k-skp")
    app = create_app(working_dir=home / "proj", provider_name="skp-on")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend

        def rpc(mid, method, params=None):
            ws.send_json({"id": mid, "method": method, "params": params or {}})
            return recv_until(ws, mid)

        # 覆盖腿：s1 会话级换模型（detached 实例，档位按服务配置 = auto）
        ws.send_json({"id": "n1", "method": "session.new"})
        s1 = recv_until(ws, "n1")["result"]["id"]
        sw = rpc("sw", "session.model_switch",
                 {"id": s1, "name": "skp-on", "model": "skp-1"})
        assert sw["ok"], sw.get("error")
        # 档位腿：s3 只调过档位（session.new_task 不打默认模型标记）
        nt = rpc("nt", "session.new_task", {})
        s3 = nt["result"]["id"]
        rs = rpc("rs", "session.reasoning_set", {"id": s3, "effort": "high"})
        assert rs["ok"], rs.get("error")
        # 跟全局腿：s4 无任何覆盖
        ws.send_json({"id": "n4", "method": "session.new"})
        s4 = recv_until(ws, "n4")["result"]["id"]

        # 全局调到 medium
        ok = rpc("s1", "model.set_reasoning", {"effort": "medium"})
        assert ok["ok"], ok.get("error")

        # 覆盖腿：对象不换、档位不被全局调档改动
        p1 = backend.runtimes[s1].agent.provider
        assert p1 is not backend.provider
        assert getattr(p1, "reasoning_effort", "auto") == "auto"
        # 档位腿：对象不换、会话档位保持 high
        p3 = backend.runtimes[s3].agent.provider
        assert p3 is not backend.provider
        assert getattr(p3, "reasoning_effort", "auto") == "high"
        # 跟全局腿：与基底一样共享全局 provider，档位跟着变
        assert backend.runtimes[s4].agent.provider is backend.provider
        assert backend.provider.reasoning_effort == "medium"


def test_effort_rebuild_replays_privately(home):
    """runtime 淘汰重建后的档位恢复：会话私有档位重放到 detached 私有副本上，
    绝不落到共享的全局 provider（修复前全局对象被原地调档，所有无覆盖会话
    ——含此后新建的——一起被改档，跨会话串档）。"""
    from fastapi.testclient import TestClient
    from test_server import recv_until

    from skysheep.config import add_provider_to_config
    from skysheep.server import create_app

    add_provider_to_config("reb-on", kind="openai", base_url="http://x",
                           model="reb-1", api_key="k-reb")
    app = create_app(working_dir=home / "proj", provider_name="reb-on")
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend

        def rpc(mid, method, params=None):
            ws.send_json({"id": mid, "method": method, "params": params or {}})
            return recv_until(ws, mid)

        ws.send_json({"id": "n1", "method": "session.new"})
        s1 = recv_until(ws, "n1")["result"]["id"]
        rs = rpc("rs", "session.reasoning_set", {"id": s1, "effort": "high"})
        assert rs["ok"], rs.get("error")
        assert backend.provider.reasoning_effort == "auto"  # 设置时就不动全局

        # LRU 淘汰：把池上限压到 1，新建会话把 s1 挤出去
        backend.MAX_RUNTIMES = 1
        ws.send_json({"id": "n2", "method": "session.new"})
        recv_until(ws, "n2")
        assert s1 not in backend.runtimes

        # 重新激活 s1：runtime 重建 + 档位恢复
        act = rpc("act", "session.activate", {"id": s1})
        assert act["ok"], act.get("error")
        p1 = backend.runtimes[s1].agent.provider
        assert getattr(p1, "reasoning_effort", "auto") == "high", "重建后档位没恢复"
        assert p1 is not backend.provider, "档位回放到了共享的全局 provider 上（跨会话串档）"
        assert backend.provider.reasoning_effort == "auto", "全局档位被会话私有档位污染"


def test_config_save_provider_toggles_reasoning(home):
    """设置页开关：保存 supports_reasoning=false 后详情与状态都反映为关闭。"""
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "d1", "method": "config.providers", "params": {}})
        detail = recv_until(ws, "d1")["result"]["providers"]["deepseek"]
        assert detail["supports_reasoning"] is True, "默认对内置/自定义服务都提供该能力"
        assert detail["reasoning_effort"] == "auto"

        ws.send_json({"id": "s1", "method": "config.save_provider", "params": {
            "name": "deepseek", "supports_reasoning": False}})
        assert recv_until(ws, "s1")["ok"]

        ws.send_json({"id": "d2", "method": "config.providers", "params": {}})
        got = recv_until(ws, "d2")["result"]["providers"]["deepseek"]
        assert got["supports_reasoning"] is False

        # 关闭后对该服务调档应报错（不再给出无效选项）
        ws.send_json({"id": "s2", "method": "model.set_reasoning",
                      "params": {"effort": "high", "provider": "deepseek"}})
        bad = recv_until(ws, "s2")
        assert not bad["ok"] and "不支持" in bad["error"]


# ---- 思考过程流式透出：ThinkingDelta 事件 + 历史块 ----

from types import SimpleNamespace  # noqa: E402

from skysheep.messages import TextBlock, ThinkingBlock  # noqa: E402
from skysheep.models.base import ProviderReasoning  # noqa: E402


def _reasoning_agent(tmp_path):
    from skysheep.core.agent import Agent
    from skysheep.core.context import estimate_tokens
    from skysheep.models.fake import FakeProvider
    from skysheep.security.gate import PermissionGate
    from skysheep.tools.base import ToolRegistry

    provider = FakeProvider([[ThinkingBlock(text="让我想一想"), TextBlock(text="答案是 4")]])
    return Agent(
        provider=provider, registry=ToolRegistry([]), gate=PermissionGate(),
        working_dir=tmp_path, context_limit_tokens=estimate_tokens([]) + 10_000,
    )


def test_agent_emits_thinking_delta_and_persists_block(tmp_path):
    """脚本含思考块：流式产出 thinking_delta 事件，收尾 ThinkingBlock 并入历史。"""
    import asyncio

    from skysheep.events import ThinkingDelta

    agent = _reasoning_agent(tmp_path)
    kinds: list[str] = []
    deltas: list[str] = []

    async def go():
        async for ev in agent.run_turn("1+1=?"):
            kinds.append(ev.kind)
            if isinstance(ev, ThinkingDelta):
                deltas.append(ev.text)

    asyncio.new_event_loop().run_until_complete(go())
    assert "thinking_delta" in kinds
    assert "".join(deltas) == "让我想一想"
    last = agent.history[-1]
    assert last.role == "assistant"
    tb = [b for b in last.content if isinstance(b, ThinkingBlock)]
    assert tb and tb[0].text == "让我想一想"


def test_openai_provider_streams_reasoning_incrementally():
    """reasoning_content 增量逐段产出 ProviderReasoning（不再攒整块）。"""
    from skysheep.models.openai_compat import OpenAICompatProvider

    c1 = SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(reasoning_content="思考A", content=None, tool_calls=None),
        finish_reason=None)])
    c2 = SimpleNamespace(usage=None, choices=[SimpleNamespace(
        delta=SimpleNamespace(reasoning_content=None, content="正文", tool_calls=None),
        finish_reason="stop")])

    class FakeCompletions:
        async def create(self, **params):
            return FakeOpenAIStream([c1, c2])

    class FakeClient:
        class chat:  # noqa: N801
            completions = FakeCompletions()

    p = OpenAICompatProvider("x", "m", "k", client=FakeClient())
    got: list = []

    async def go():
        async for pe in p.stream([], []):
            got.append(pe)

    asyncio.new_event_loop().run_until_complete(go())
    reasoning = [pe for pe in got if isinstance(pe, ProviderReasoning)]
    assert [r.text for r in reasoning] == ["思考A"], "思考在前、正文在后逐段透出"


def test_proxy_config_roundtrip(home):
    """代理地址：保存 → 落盘 → 构建带 http_client 的 provider → 清除。"""
    update_provider_in_config("deepseek", proxy="http://127.0.0.1:7890")
    cfg = load_config()
    assert cfg.providers["deepseek"].proxy == "http://127.0.0.1:7890"

    from skysheep.config import ProviderConfig
    from skysheep.models.factory import build_provider

    build_provider("x", ProviderConfig(
        kind="openai", base_url="http://x", api_key="k", model="m",
        proxy="http://127.0.0.1:7890"))

    # 非法格式拒绝；空串 = 清除
    from skysheep.config import ConfigError
    with pytest.raises(ConfigError):
        update_provider_in_config("deepseek", proxy="ftp://x")
    update_provider_in_config("deepseek", proxy="")
    assert load_config().providers["deepseek"].proxy == ""


def test_proxy_via_ws(home):
    """WS 端到端：save_provider 带 proxy → providers_detail 回读。"""
    from test_server import make_client, recv_until

    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "config.save_provider",
                      "params": {"name": "deepseek", "proxy": "http://127.0.0.1:7890"}})
        ok = recv_until(ws, "p1")
        assert ok["ok"], ok.get("error")

        ws.send_json({"id": "d1", "method": "config.providers", "params": {}})
        got = recv_until(ws, "d1")["result"]["providers"]["deepseek"]
        assert got["proxy"] == "http://127.0.0.1:7890"

        # 非法格式报错不落盘
        ws.send_json({"id": "p2", "method": "config.save_provider",
                      "params": {"name": "deepseek", "proxy": "bogus"}})
        bad = recv_until(ws, "p2")
        assert not bad["ok"]
