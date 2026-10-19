"""Mods Node.js 运行时（P1）测试。

覆盖：
- probe_node 探测
- 简单 Mod 加载与 hook 注册
- 中间件链执行 + verdict 语义
- $ API（state / fs / http.fetch / clock）
- 异常与超时
- 关闭与资源清理
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from skysheep.core.mods_node import (
    NodeRuntime,
    NodeRuntimeError,
    find_node,
    probe_node,
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None and find_node() is None,
    reason="需要 Node.js 18+",
)


def _write_mod(tmp_path: Path, main_js: str) -> Path:
    mod_dir = tmp_path / "mymod"
    mod_dir.mkdir()
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")
    return mod_dir


@pytest.mark.asyncio
async def test_probe_node():
    ok, version = probe_node()
    assert ok
    assert version.startswith("v")


@pytest.mark.asyncio
async def test_load_and_list_hooks(tmp_path):
    mod_dir = _write_mod(
        tmp_path,
        """
        register('tool_pre', () => undefined);
        register('tool_post', () => undefined);
        register('turn_stop', () => undefined);
        """,
    )
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    hooks = await rt.start()
    try:
        assert set(hooks) == {"tool_pre", "tool_post", "turn_stop"}
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_middleware_chain_interrupt(tmp_path):
    """handler 不调用 next() 即中断链：下游不执行，verdict 为中断处返回值。"""
    mod_dir = _write_mod(
        tmp_path,
        """
        register('tool_pre', { priority: 10 }, ($, e, next) => {
          e.payload.seen.push('a');
          return next();
        });
        register('tool_pre', { priority: 5 }, ($, e, next) => {
          e.payload.seen.push('b');
          return { action: 'allow', seen: [...e.payload.seen] };
        });
        register('tool_pre', { priority: 1 }, ($, e, next) => {
          e.payload.seen.push('c');
          return next();
        });
        """,
    )
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    try:
        verdict, _state, err = await rt.call("tool_pre", {"seen": []}, {})
        assert err == ""
        # h2 未调用 next：h3 不执行，verdict 为 h2 的返回值
        assert verdict == {"action": "allow", "seen": ["a", "b"]}
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_middleware_chain_full(tmp_path):
    """全部 handler 调用 next()：值向外层冒泡，最外层 handler 最终决定 verdict。

    这是中间件语义（与 Express/Koa 一致）：await next() 的返回值来自更深层，
    外层可以忽略、覆盖或透传。框架最终看到的是最外层 handler 的 return。
    """
    mod_dir = _write_mod(
        tmp_path,
        """
        register('tool_pre', { priority: 10 }, async ($, e, next) => {
          e.payload.seen.push('a');
          await next();
          return { from: 'h1' };
        });
        register('tool_pre', { priority: 5 }, async ($, e, next) => {
          e.payload.seen.push('b');
          await next();
          return { from: 'h2' };
        });
        register('tool_pre', { priority: 1 }, async ($, e, next) => {
          e.payload.seen.push('c');
          await next();
          return { from: 'h3' };
        });
        """,
    )
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    try:
        verdict, _state, err = await rt.call("tool_pre", {"seen": []}, {})
        assert err == ""
        # 最外层 h1 的 return 是框架最终看到的
        assert verdict == {"from": "h1"}
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_middleware_chain_passthrough(tmp_path):
    """外层透传 next() 返回值：内层可以决定最终 verdict。"""
    mod_dir = _write_mod(
        tmp_path,
        """
        register('tool_pre', { priority: 10 }, ($, e, next) => {
          return next();
        });
        register('tool_pre', { priority: 5 }, ($, e, next) => {
          return next();
        });
        register('tool_pre', { priority: 1 }, ($, e, next) => {
          return { from: 'deepest' };
        });
        """,
    )
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    try:
        verdict, _state, err = await rt.call("tool_pre", {}, {})
        assert err == ""
        assert verdict == {"from": "deepest"}
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_state_roundtrip(tmp_path):
    mod_dir = _write_mod(
        tmp_path,
        """
        register('turn_stop', ($) => {
          const count = ($.state.get('count') || 0) + 1;
          $.state.set('count', count);
          return { count };
        });
        """,
    )
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    try:
        state = {}
        v1, state, e1 = await rt.call("turn_stop", {}, state)
        assert e1 == ""
        assert v1 == {"count": 1}
        assert state == {"count": 1}
        v2, state, e2 = await rt.call("turn_stop", {}, state)
        assert e2 == ""
        assert v2 == {"count": 2}
        assert state == {"count": 2}
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_fs_api(tmp_path):
    """$ .fs 是 Node 原生文件系统——无沙箱。"""
    mod_dir = _write_mod(
        tmp_path,
        """
        register('turn_stop', async ($) => {
          const target = $.mod.dir() + '/hello.txt';
          await $.fs.write(target, 'world');
          const back = await $.fs.read(target);
          return { back, modId: $.mod.id() };
        });
        """,
    )
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    try:
        verdict, _, err = await rt.call("turn_stop", {}, {})
        assert err == ""
        assert verdict == {"back": "world", "modId": "mymod"}
        assert (mod_dir / "hello.txt").read_text(encoding="utf-8") == "world"
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_handler_exception_reported(tmp_path):
    mod_dir = _write_mod(
        tmp_path,
        """
        register('tool_pre', () => { throw new Error('boom'); });
        """,
    )
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    try:
        verdict, _, err = await rt.call("tool_pre", {}, {})
        assert verdict is None
        assert "boom" in err
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_unregistered_event_returns_null(tmp_path):
    mod_dir = _write_mod(tmp_path, "register('tool_pre', () => undefined);")
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    try:
        verdict, _, err = await rt.call("turn_stop", {}, {})
        assert err == ""
        assert verdict is None
    finally:
        await rt.close()


@pytest.mark.asyncio
async def test_load_error_syntax(tmp_path):
    mod_dir = _write_mod(tmp_path, "this is not valid javascript {{{")
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    with pytest.raises(NodeRuntimeError):
        await rt.start()


@pytest.mark.asyncio
async def test_entry_missing(tmp_path):
    rt = NodeRuntime("mymod", tmp_path / "missing.js", mod_dir=tmp_path)
    with pytest.raises(NodeRuntimeError):
        await rt.start()


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path):
    mod_dir = _write_mod(tmp_path, "register('tool_pre', () => undefined);")
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    await rt.close()
    await rt.close()  # 二次关闭不报错


@pytest.mark.asyncio
async def test_call_after_close(tmp_path):
    mod_dir = _write_mod(tmp_path, "register('tool_pre', () => ({ ok: 1 }));")
    rt = NodeRuntime("mymod", mod_dir / "main.js", mod_dir=mod_dir)
    await rt.start()
    await rt.close()
    verdict, _, err = await rt.call("tool_pre", {}, {})
    assert verdict is None
    assert "已关闭" in err
