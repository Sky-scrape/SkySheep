"""模型服务的备用模型解析与切换、新增服务激活分支（ProvidersMixin 单元级）。

用只挂 ProvidersMixin 的轻量桩，把「备用模型 provider:model 的解析」与
「主模型失败时切备用」的几条分支单独覆盖，不依赖真实网络与真实窗口。
"""

from __future__ import annotations

import asyncio

from skysheep.server.backend_parts.providers import ProvidersMixin


class _ProviderStub(ProvidersMixin):
    """只挂 ProvidersMixin：补上混入方法依赖的后端属性与钩子。"""

    def __init__(self, *, build=None, build_error=None):
        self.provider = None
        self.provider_error = None
        self.provider_name = ""
        self.provider_model = ""
        self.cfg = None
        self._build = build
        self._build_error = build_error

    # --- ProvidersMixin 依赖的后端钩子 ---
    def _build_provider(self, name, model=None):
        if self._build_error:
            raise RuntimeError(self._build_error)
        return self._build

    def _for_each_agent(self):
        return []

    def _context_limit(self):
        return 1000


class _PC:
    def __init__(self, kind="openai", model="glm-5.3"):
        self.kind = kind
        self.model = model


class _Cfg:
    def __init__(self, fallback="zhipu:glm-5.3", providers=None):
        self.fallback_model = fallback
        self.providers = providers if providers is not None else {"zhipu": _PC()}


# ---- 新增服务：缺模型时顺手启用 ----


def test_add_provider_activates_when_no_provider(home):
    class _P:
        model = "m1"

    stub = _ProviderStub(build=_P())
    out = asyncio.run(stub.add_provider("my-relay", base_url="http://x/v1", model="m1"))
    assert out["activated"] is True
    assert out["model"] == "m1"
    assert stub.provider_error is None


def test_add_provider_reports_build_failure(home):
    stub = _ProviderStub(build_error="缺 API Key")
    out = asyncio.run(stub.add_provider("my-relay2", base_url="http://x/v1", model="m1"))
    assert out["activated"] is False
    assert "缺 API Key" in out["provider_error"]


# ---- 备用模型解析 ----


def test_fallback_target_parsing():
    stub = _ProviderStub()
    stub.cfg = _Cfg()
    assert stub._fallback_target() == ("zhipu", "glm-5.3")

    stub.cfg = _Cfg(fallback="")
    assert stub._fallback_target() is None
    stub.cfg = _Cfg(fallback="nocolon")
    assert stub._fallback_target() is None
    stub.cfg = _Cfg(fallback="ghost:m")
    assert stub._fallback_target() is None  # 未知服务
    stub.cfg = _Cfg(fallback="zhipu:")  # 空模型名 → 用该服务默认模型
    assert stub._fallback_target() == ("zhipu", "glm-5.3")
    stub.cfg = _Cfg()
    stub.provider_name = "zhipu"
    assert stub._fallback_target() is None  # 指向当前服务


def test_fallback_target_skips_fake_provider():
    stub = _ProviderStub()
    stub.cfg = _Cfg(providers={"demo": _PC(kind="fake", model="演示")}, fallback="demo:x")
    assert stub._fallback_target() is None


# ---- 切备用 ----


def test_switch_to_fallback_paths():
    stub = _ProviderStub()
    stub.cfg = _Cfg()
    calls = []

    async def _switch(name, model=None):
        calls.append((name, model))

    stub.switch_model = _switch
    assert asyncio.run(stub._switch_to_fallback("why")) is True
    assert calls == [("zhipu", "glm-5.3")]

    # 已经在目标服务上 → 不重复切
    stub.provider_name, stub.provider_model = "zhipu", "glm-5.3"
    assert asyncio.run(stub._switch_to_fallback("why")) is False

    # 没有可用备用目标 → False
    stub.cfg = _Cfg(fallback="")
    stub.provider_name = ""
    assert asyncio.run(stub._switch_to_fallback("why")) is False

    # 切换本身失败 → False（不打断错误路径）
    stub.cfg = _Cfg()

    async def _boom(name, model=None):
        raise RuntimeError("switch failed")

    stub.switch_model = _boom
    assert asyncio.run(stub._switch_to_fallback("why")) is False
