"""Mods 扩展（实验性）核心单测：清单校验、沙箱执行、事件分发、状态与降级。"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from conftest import FakeProvider

from skysheep.config import _read_raw_config, set_mods_in_config
from skysheep.core.agent import Agent
from skysheep.core.mods import (
    MAX_STATE_BYTES,
    STATE_SESSION_LRU,
    ModError,
    ModManager,
    assemble_entry_source,
    load_manifest_from_dir,
    load_mod_state,
    mods_root,
    mods_template,
    parse_manifest,
    save_mod_state,
)
from skysheep.events import ModUI, NoticeEvent, PermissionRequest
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.security.gate import PermissionGate
from skysheep.tools import ToolRegistry, default_tools

# ---- 夹具与助手 ----


def install_mod(
    mod_id: str, main_js: str, manifest: dict | None = None,
    *, filename: str = "mod.json", readme: str = "",
) -> Path:
    """把一个 Mod 写进隔离 home 的安装根目录，返回其目录。"""
    mod_dir = mods_root() / mod_id
    mod_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"id": mod_id, "name": mod_id, "version": "1.0.0",
                "hooks": [], "permissions": "observe", **(manifest or {})}
    manifest["id"] = mod_id
    (mod_dir / filename).write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    (mod_dir / "main.js").write_text(main_js, encoding="utf-8")
    if readme:
        (mod_dir / "README.md").write_text(readme, encoding="utf-8")
    return mod_dir


def make_manager(*ids: str, enabled: bool = True) -> ModManager:
    return ModManager(enabled=enabled, enabled_ids=list(ids))


def make_agent(provider, tmp_path, mods=None, gate=None):
    return Agent(
        provider=provider,
        registry=ToolRegistry(default_tools()),
        gate=gate or PermissionGate(working_dir=tmp_path),
        working_dir=tmp_path,
        mods=mods,
    )


async def collect(agent, text, auto_respond="allow_once"):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request" and auto_respond:
            agent.respond_permission(ev.request_id, auto_respond)
    return events


# ---- 清单校验 ----


def test_manifest_rejects_unknown_fields(home):
    """清单未知字段拒装（fail-closed）：闭集外的键一律 ModError。"""
    with pytest.raises(ModError, match="未知字段"):
        parse_manifest({"id": "x", "name": "x", "bogus": 1})


def test_manifest_accepts_mods_json_alias(home):
    """CC 别名 mods.json 与 mod.json 两都认。"""
    install_mod("alias-mod", "export default {}", {}, filename="mods.json")
    manifest, path = load_manifest_from_dir(mods_root() / "alias-mod")
    assert path.name == "mods.json"
    assert manifest["id"] == "alias-mod"
    assert manifest["api"] == 1  # CC 清单缺 api 按 api=1 收敛


def test_manifest_rejects_bad_ids(home):
    for bad in ("../evil", "C:evil", "a/b", "a\\b", "...", ""):
        with pytest.raises(ModError):
            parse_manifest({"id": bad})


def test_manifest_rejects_reserved_ids(home):
    """保留名拒装：state 是全部 Mod 持久状态的存放目录，点开头目录 load_all
    永远跳过——装进去要么覆盖安装时换掉状态目录，要么是永不加载的僵尸目录。"""
    from skysheep.core.mods import _mod_target

    for bad in ("state", "State", ".hidden"):
        with pytest.raises(ModError, match="保留名"):
            parse_manifest({"id": bad})
        # 安装/删除落点同口径：装/删 id="state" 在落点校验就被拒
        with pytest.raises(ModError, match="保留名"):
            _mod_target(mods_root(), bad)
    # 拒绝发生在任何 rmtree 之前：已有 Mod 的持久状态原样还在
    save_mod_state("victim", {"s": {"n": 1}})
    with pytest.raises(ModError, match="保留名"):
        _mod_target(mods_root(), "state")
    assert load_mod_state("victim") == {"s": {"n": 1}}
    assert load_mod_state("state") == {}


def test_manifest_permissions_two_levels_and_cc_wider_mapping(home):
    """permissions 只认 observe/tighten；CC 更宽档收敛为 tighten 并提示。"""
    assert parse_manifest({"id": "x", "permissions": "observe"})["permissions"] == "observe"
    assert parse_manifest({"id": "x", "permissions": "tighten"})["permissions"] == "tighten"
    narrowed = parse_manifest({"id": "x", "permissions": "all"})
    assert narrowed["permissions"] == "tighten"
    assert any("tighten" in n for n in narrowed["notes"])
    with pytest.raises(ModError):
        parse_manifest({"id": "x", "permissions": "sudo"})


def test_manifest_declarative_only_for_tighten(home):
    """declarative 收紧表只有 tighten 档允许声明。"""
    with pytest.raises(ModError, match="tighten"):
        parse_manifest({"id": "x", "permissions": "observe",
                        "declarative": {"deny_tools": ["write_file"]}})
    ok = parse_manifest({"id": "x", "permissions": "tighten",
                         "declarative": {"deny_tools": ["write_file"]}})
    assert ok["declarative"]["deny_tools"] == ["write_file"]


def test_manifest_sanitize_name_description(home):
    """name/description 压空白限长（120 / 1000）。"""
    m = parse_manifest({"id": "x", "name": "  a\t\nb  ", "description": "长" * 2000})
    assert m["name"] == "a b"
    assert m["description"].endswith("…") and len(m["description"]) <= 1001


def test_load_all_shows_broken_manifest_as_error_entry(home):
    """坏清单不静默消失：进清单、带 load_error（设置页可见）。"""
    broken = mods_root() / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "mod.json").write_text("{ 不是 json", encoding="utf-8")
    mgr = make_manager()
    assert "broken" in mgr.mods
    assert "JSON" in mgr.mods["broken"].load_error


def test_official_label_requires_bundled_source(home):
    """「官方示例」标记只认 .source.json 的 source=bundled，清单自声明不算数。

    修复前 _is_official 读清单里的 author / cc_mods.upstream 自声明字段——第三方
    把 author 写成 SkySheep 就能骗取官方信任指示。
    """
    install_mod("self-claimed", "export default {}",
                {"author": "SkySheep",
                 "cc_mods": {"compatible": True,
                             "upstream": "https://github.com/Sky-scrape/SkySheep"}})
    mgr = make_manager("self-claimed")
    assert mgr.mods["self-claimed"].source_label == "本地安装"
    mgr.close()
    # 经 install_official_mod 落盘的凭证（.source.json）才换官方标记
    (mods_root() / "self-claimed" / ".source.json").write_text(
        json.dumps({"source": "bundled"}), encoding="utf-8")
    mgr = make_manager("self-claimed")
    assert mgr.mods["self-claimed"].source_label == "官方示例"
    mgr.close()


# ---- 沙箱执行与动作解析 ----


async def test_handler_dispatch_and_action_result(home):
    """默认导出对象 handler 分发：note / ui 正常回传并带 [Mod·<id>] 前缀。"""
    install_mod("note-mod",
                "export default { toolPre(p){ return {note: 'checked ' + p.tool,"
                " ui: [{kind: 'badge', slot: 'stream', text: 'B'}]} } }",
                {"hooks": ["tool_pre"], "permissions": "observe"})
    mgr = make_manager("note-mod")
    r = await mgr.on_tool_pre("write_file", {"path": "a"}, "s1", 10)
    assert r["note"] == "[Mod·note-mod] checked write_file"
    assert r["ui"] and r["ui"][0]["mod_id"] == "note-mod"
    assert r["ui"][0]["text"] == "B"
    mgr.close()


async def test_closed_action_set_drops_unknown_keys(home):
    """动作闭集：modifyInput / allow / decision 等闭集外键一律丢弃并计错误。"""
    install_mod("greedy-mod",
                "export default { toolPre(p){ return {modifyInput: {tool: 'x'},"
                " allow: true, decision: 'allow_once', note: 'kept'} } }",
                {"hooks": ["tool_pre"], "permissions": "observe"})
    mgr = make_manager("greedy-mod")
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["note"] == "[Mod·greedy-mod] kept"  # 合法键保留
    assert r["deny"] is None and not r["ui"]
    assert any("modifyInput" in e for e in r["errors"])
    assert any("allow" in e for e in r["errors"])
    assert any("decision" in e for e in r["errors"])
    # 丢弃计入 mod.errors 并留 dropped 执行记录（设置页可见，不再恒 0）
    mod = mgr.mods["greedy-mod"]
    assert mod.errors == 3
    assert mod.recent[-1]["status"] == "dropped"
    assert "modifyInput" in mod.recent[-1]["output"]
    mgr.close()


async def test_runtime_errors_leave_recent_records(home, monkeypatch):
    """非解析类运行期错误也留执行记录：状态落盘失败与 run_hook 外意外异常
    都在「最近执行」里查得到（「错误 N」chip 的明细指引在这类错误上成立）。"""
    import skysheep.core.mods as mods_module

    install_mod("save-mod",
                "export default { toolPre(p){ sky.state.n = 1; return {} } }",
                {"hooks": ["tool_pre"]})
    install_mod("boom-mod", "export default {}", {"hooks": ["tool_pre"]})
    mgr = make_manager("save-mod", "boom-mod")

    # 状态写盘失败：handler 本身跑成功（已记 "ok"），补一条 error 记录
    def boom_touch(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(mods_module, "touch_mod_state", boom_touch)
    r = await mgr.on_tool_pre("a", {}, "s", 0)
    assert any("状态保存失败" in e for e in r["errors"])
    save = mgr.mods["save-mod"]
    assert save.errors == 1
    assert save.recent[-1]["status"] == "error"
    assert "状态保存失败" in save.recent[-1]["output"]

    # run_hook 外意外异常：同样补一条 error 记录（run_hook 没跑到自己的记数）
    async def boom_hook(*a, **k):
        raise RuntimeError("bridge exploded")
    boom = mgr.mods["boom-mod"]
    boom.run_hook = boom_hook
    errors: list[str] = []
    await mgr._run(boom, "tool_pre", {"tool": "a"}, "s",
                   allow_deny=False, errors=errors)
    assert errors and "bridge exploded" in errors[0]
    assert boom.errors == 1
    assert boom.recent[-1]["status"] == "error"
    assert "bridge exploded" in boom.recent[-1]["output"]
    mgr.close()


async def test_observe_mod_cannot_deny(home):
    """observe 档的 deny 被丢弃并计错误：观察档永远拦不住调用。"""
    install_mod("peek-mod",
                "export default { toolPre(p){ return {deny: 'nope'} } }",
                {"hooks": ["tool_pre"], "permissions": "observe"})
    mgr = make_manager("peek-mod")
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["deny"] is None
    assert any("observe 档不允许 deny" in e for e in r["errors"])
    assert mgr.mods["peek-mod"].errors == 1
    mgr.close()


async def test_clean_run_counts_no_errors(home):
    """正常执行（闭集内动作）不产生错误计数与 dropped 记录。"""
    install_mod("clean-mod",
                "export default { toolPre(p){ return {note: 'ok'} } }",
                {"hooks": ["tool_pre"], "permissions": "observe"})
    mgr = make_manager("clean-mod")
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["errors"] == []
    mod = mgr.mods["clean-mod"]
    assert mod.errors == 0
    assert [x["status"] for x in mod.recent] == ["ok"]
    mgr.close()


async def test_tighten_mod_can_deny(home):
    install_mod("guard-mod",
                "export default { toolPre(p){ if (p.tool === 'write_file')"
                " return {deny: 'forbidden by policy'} } }",
                {"hooks": ["tool_pre"], "permissions": "tighten"})
    mgr = make_manager("guard-mod")
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["deny"] == "[Mod·guard-mod] forbidden by policy"
    mgr.close()


async def test_cc_named_export_bridge(home):
    """CC 兼容桥接：具名导出 onToolCall 映射为 tool_pre；宽档 permissions 收敛。"""
    install_mod("cc-style", "export function onToolCall(p){ return {deny: 'cc-block'} }",
                {"id": "cc-style", "name": "cc", "permissions": "write",
                 "cc_mods": {"compatible": True}},
                filename="mods.json")
    mgr = make_manager("cc-style")
    assert mgr.mods["cc-style"].manifest["permissions"] == "tighten"
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert r["deny"] == "[Mod·cc-style] cc-block"
    mgr.close()


async def test_import_statement_rejected(home):
    """main.js 里 import 语句拒载（沙箱没有模块加载器，也不提供外部依赖）。"""
    install_mod("importer",
                "import x from 'y';\nexport default { toolPre(p){ return {} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("importer")
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert not r["ui"] and not r["note"]
    assert "import" in mgr.mods["importer"].load_error
    mgr.close()


async def test_widget_vocabulary_enforced(home):
    """Widget 词表：未知 kind / 错配 slot / 超长文本被丢弃或截断。"""
    install_mod("widget-mod",
                "export default { toolPre(p){ return {ui: ["
                " {kind: 'stat', slot: 'tray', text: 'T', level: 'warn'},"
                " {kind: 'stat', slot: 'stream', text: 'bad slot'},"
                " {kind: 'nope', slot: 'tray', text: 'x'}] } } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("widget-mod")
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    kinds = [(w["kind"], w["slot"]) for w in r["ui"]]
    assert kinds == [("stat", "tray")]
    mgr.close()


async def test_executor_serializes_calls_on_one_thread(home):
    """per-Mod 单线程 executor：两次耗时 handler 串行执行（墙钟 ≥ 2×单次）。"""
    install_mod("busy-mod",
                "export default { toolPre(p){ var t = sky.now();"
                " while (sky.now() - t < 80) {} return {} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("busy-mod")
    t0 = time.monotonic()
    await mgr.on_tool_pre("a", {}, "s", 0)
    await mgr.on_tool_pre("a", {}, "s", 0)
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.15, "同一 Mod 的两次调用必须串行（单线程 executor）"
    mgr.close()


async def test_timeout_discards_and_auto_deactivates(home):
    """超时：结果丢弃 + 计错误；连续 3 次超时进程内自动停用（重启复活）。"""
    install_mod("loop-mod", "export default { toolPre(p){ while(true){} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("loop-mod")
    for _ in range(3):
        r = await mgr.on_tool_pre("write_file", {}, "s", 0)
        assert r["deny"] is None and not r["note"]  # 结果已丢弃
        assert any("超时" in e for e in r["errors"])
    assert mgr.mods["loop-mod"].auto_deactivated
    # 自动停用后不再执行（不再产生新的超时错误）
    r = await mgr.on_tool_pre("write_file", {}, "s", 0)
    assert not r["errors"]
    # 重启（重建 Manager）复活
    mgr.close()
    mgr2 = make_manager("loop-mod")
    assert not mgr2.mods["loop-mod"].auto_deactivated
    mgr2.close()


# ---- 状态（sky.state，引擎代存） ----


async def test_sky_state_session_keyed_and_persisted(home):
    """sky.state 按 (Mod, 会话) 分键持久化：跨会话互不覆盖，落盘可读回。"""
    install_mod("state-mod",
                "export default { toolPre(p){ sky.state.n = (sky.state.n || 0) + 1;"
                " return {} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("state-mod")
    await mgr.on_tool_pre("a", {}, "sessA", 0)
    await mgr.on_tool_pre("a", {}, "sessA", 0)
    await mgr.on_tool_pre("a", {}, "sessB", 0)
    state = load_mod_state("state-mod")
    assert state["sessA"] == {"n": 2}
    assert state["sessB"] == {"n": 1}
    mgr.close()


async def test_concurrent_dispatch_keeps_both_session_states(home):
    """真并发回归：同一 Mod 被基础 Agent 与会话 Agent 并发派发，两个会话键都存活。

    对应 ModManager._run 的状态保存路径「重读全量 → 摘插本会话键 → 原子写回」
    （touch_mod_state）。无锁推演（能红的场景）：两条 _run 若都在对方落盘之前
    完成重读——只要这条临界区里出现任何可切走的 await（或改走线程执行）就会——
    后写者拿旧快照整键覆盖，先写者的会话键丢失（last-writer-wins）；加锁后
    临界区按 mod 串行，后写者的重读必见先写者的写回，两键共存（转绿）。
    如实说明（经无锁旁路实验证实）：当前 touch_mod_state 是纯同步读改写且写入时
    重读全量，单事件循环内没有可切入的 await——无锁旁路下本用例的数据断言同样
    通过——这正是上轮复核无法复现确证的原因。因此本用例锁定的是不变量本身：
    两个任务真并发（经 run_hook 的 await 真实交错）驱动同一 Mod 的状态保存路径，
    任一会话键丢失即失败；日后临界区一旦引入 await（或改走线程），无锁版本在
    本场景即转红，加锁版本恒绿。锁的构造契约另见 test_state_lock_is_per_mod，
    「锁确实圈住了状态写入」的可证伪观测另见 test_state_lock_wraps_state_write。
    """
    install_mod("race-mod",
                "export default { toolPre(p){ sky.state.n = (sky.state.n || 0) + 1;"
                " return {} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("race-mod")

    async def drive(session_id: str, rounds: int) -> None:
        for _ in range(rounds):
            await mgr.on_tool_pre("write_file", {"path": "x"}, session_id, 0)
            await asyncio.sleep(0)  # 主动让位，放大两任务的交错机会

    await asyncio.gather(drive("sessA", 8), drive("sessB", 8))
    state = load_mod_state("race-mod")
    assert state.get("sessA") == {"n": 8}, "并发派发不得丢 sessA 的会话状态"
    assert state.get("sessB") == {"n": 8}, "并发派发不得丢 sessB 的会话状态"
    mgr.close()


async def test_state_lock_wraps_state_write(home, monkeypatch):
    """锁的证伪用例：touch_mod_state 必须只发生在对应 _state_lock 持有期间。

    上一条并发用例锁定的是数据不变量，但当前临界区（touch_mod_state）纯同步、
    单事件循环内没有可切入的 await，无锁旁路下它照样绿——对「锁还在不在」没有
    证伪能力。本用例直接观测结构契约：并发派发期间给 touch_mod_state 挂探针，
    断言每次调用都落在该 mod 锁的 acquire 与 release 之间。谁删了锁（或让
    _run 绕开 _state_lock 写状态），本用例即转红。锁的复用契约另见
    test_state_lock_is_per_mod。
    """
    import skysheep.core.mods as mods_core

    install_mod("probe-mod",
                "export default { toolPre(p){ sky.state.n = (sky.state.n || 0) + 1;"
                " return {} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("probe-mod")

    held: set[str] = set()
    touched: list[str] = []
    unlocked_calls: list[str] = []
    real_state_lock = ModManager._state_lock

    def probing_state_lock(mod_id: str):
        lock = real_state_lock(mgr, mod_id)

        class _ProbedLock:
            async def __aenter__(self):
                await lock.acquire()
                held.add(mod_id)

            async def __aexit__(self, *exc):
                held.discard(mod_id)
                lock.release()

        return _ProbedLock()

    # 实例属性遮蔽类方法：_run 经 self._state_lock(mod.id) 取锁，走的是探针
    mgr._state_lock = probing_state_lock

    real_touch = mods_core.touch_mod_state

    def probing_touch(mod_id: str, session_id: str, new_state: dict) -> None:
        # 不就地 raise：_run 对状态写入有 try/except 兜底，异常到不了测试
        if mod_id not in held:
            unlocked_calls.append(session_id)
        touched.append(session_id)
        real_touch(mod_id, session_id, new_state)

    monkeypatch.setattr(mods_core, "touch_mod_state", probing_touch)

    await asyncio.gather(
        mgr.on_tool_pre("write_file", {"path": "x"}, "sessA", 0),
        mgr.on_tool_pre("write_file", {"path": "x"}, "sessB", 0),
    )
    assert sorted(touched) == ["sessA", "sessB"], "探针必须真实命中（防断言空转）"
    assert not unlocked_calls, f"touch_mod_state 在未持锁时被调用：{unlocked_calls}"
    state = load_mod_state("probe-mod")
    assert state["sessA"] == {"n": 1} and state["sessB"] == {"n": 1}
    mgr.close()


def test_state_lock_is_per_mod(home):
    """_state_lock 助手契约：同一 mod id 复用同一把锁，不同 mod 各一把。"""
    mgr = make_manager("lock-a", "lock-b")
    assert mgr._state_lock("lock-a") is mgr._state_lock("lock-a")
    assert mgr._state_lock("lock-a") is not mgr._state_lock("lock-b")
    mgr.close()


def test_state_lru_and_size_cap(home):
    """状态文件：会话键 LRU 20（读取侧截断）、单 Mod ≤64KB（超限丢最旧会话键）。"""
    big = {"k": "x" * 400}
    state_in = {f"s{i}": dict(big) for i in range(STATE_SESSION_LRU + 10)}
    save_mod_state("big-mod", state_in)
    state = load_mod_state("big-mod")
    assert len(state) == STATE_SESSION_LRU
    assert "s0" not in state and f"s{STATE_SESSION_LRU + 9}" in state
    # 尺寸上限：单会话超大 → 截到空
    save_mod_state("huge-mod", {"s": {"k": "y" * (MAX_STATE_BYTES)}})
    assert load_mod_state("huge-mod") == {}


def test_state_touch_is_true_lru(home):
    """touch_mod_state 把刚更新的会话键移到末尾：超限截断丢「最久未写」而非「最早创建」。

    修复前写入侧只改值不 re-insert，截断按创建序取尾部——最早创建但仍在活跃的
    会话状态先被丢，「LRU 20」实为「按创建序保留 20」。
    """
    from skysheep.core.mods import touch_mod_state

    save_mod_state("lru-mod", {f"s{i}": {} for i in range(STATE_SESSION_LRU)})
    # 最早创建的 s0 被更新：re-insert 后它比 s1..s19 都「新」
    touch_mod_state("lru-mod", "s0", {"n": 1})
    # 再写一个新键挤掉最久未写的 s1（而不是刚写过的 s0）
    touch_mod_state("lru-mod", "s20", {"n": 2})
    state = load_mod_state("lru-mod")
    assert len(state) == STATE_SESSION_LRU
    assert state.get("s0") == {"n": 1}, "刚更新过的最早键必须活下来"
    assert "s1" not in state, "被挤掉的应是最久未写的键"
    assert state.get("s20") == {"n": 2}


def test_write_is_atomic_layout(home):
    """状态经 textio.write_text_atomic 落盘（引擎自有状态文件不裸写）。"""
    save_mod_state("atomic-mod", {"s": {"n": 1}})
    from skysheep.textio import is_textio_tmp_name

    names = [p.name for p in (mods_root() / "state").iterdir()]
    assert "atomic-mod.json" in names
    assert not [n for n in names if is_textio_tmp_name(n)]


# ---- 事件 payload 裁剪 ----


async def test_payload_is_trimmed(home):
    """payload 只含白名单字段：不带历史、系统提示词与任何引擎内部状态。"""
    install_mod("spy-mod",
                "export default { toolPre(p){ sky.state.keys = Object.keys(p);"
                " return {} }, toolPost(p){ sky.state.postKeys = Object.keys(p);"
                " return {} } }",
                {"hooks": ["tool_pre", "tool_post"]})
    mgr = make_manager("spy-mod")
    await mgr.on_tool_pre("write_file", {"path": "a"}, "s", 5)
    assert load_mod_state("spy-mod")["s"]["keys"] == ["tool", "input", "sessionId", "contextTokens"]
    await mgr.on_tool_post("write_file", {"path": "a"}, "res", False, 1, "s", 5, 100)
    post_keys = load_mod_state("spy-mod")["s"]["postKeys"]
    assert post_keys == ["tool", "input", "resultPreview", "isError", "durationMs",
                         "sessionId", "contextTokens", "contextLimitTokens"]
    mgr.close()


# ---- declarative 收紧声明（纯 Python，不依赖 JS） ----


def test_declarative_deny_and_require_confirm(home):
    """deny_tools / require_confirm_tools 纯 Python 求值，fnmatch 通配。"""
    install_mod("decl-mod", "export default {}",
                {"permissions": "tighten",
                 "declarative": {"deny_tools": ["delete_*"],
                                 "require_confirm_tools": ["run_command"]}})
    mgr = make_manager("decl-mod")
    assert "decl-mod" in mgr.declarative_deny("delete_file", {})
    assert mgr.declarative_deny("write_file", {}) == ""
    assert mgr.requires_confirm("run_command")
    assert not mgr.requires_confirm("write_file")
    assert "decl-mod" in mgr.describe_confirm("run_command")
    mgr.close()


def test_declarative_works_without_js_runtime(home, monkeypatch):
    """降级态：JS 运行时不可用时 declarative 仍生效（收紧类不依赖 JS）。"""
    import skysheep.core.mods as mods_module

    monkeypatch.setattr(mods_module, "probe_quickjs", lambda: (False, ""))
    install_mod("decl-only-mod", "export default { toolPre(p){ return {note: 'x'} } }",
                {"permissions": "tighten",
                 "declarative": {"deny_tools": ["write_file"]}})
    mgr = make_manager("decl-only-mod")
    assert not mgr.runtime_available
    assert "decl-only-mod" in mgr.declarative_deny("write_file", {})
    r = asyncio.run(mgr.on_tool_pre("write_file", {}, "s", 0))
    assert not r["note"] and not r["ui"]  # JS handler 不执行
    mgr.close()


# ---- 与权限门/Agent 循环的集成 ----


async def test_declarative_deny_blocks_before_gate_no_pending(home):
    """deny_tools 门前 deny：不弹确认卡、不创建 pending，历史落错误结果。"""
    install_mod("front-deny", "export default {}",
                {"permissions": "tighten",
                 "declarative": {"deny_tools": ["write_file"]}})
    mgr = make_manager("front-deny")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file", input={"path": "a.txt", "content": "x"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "write a.txt")
    kinds = [e.kind for e in events]
    assert "permission_request" not in kinds  # 比门更早，不弹卡
    assert not agent._pending
    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert finished.is_error and "[mod:front-deny]" in finished.preview
    tool_texts = [
        blk.content
        for m in agent.history if m.role == "tool"
        for blk in m.content if getattr(blk, "content", None)
    ]
    assert any("[mod:front-deny]" in t for t in tool_texts)
    assert not (home / "proj" / "a.txt").exists()
    mgr.close()


async def test_mod_veto_leaves_no_pending_residue(home):
    """权限请求否决：不发 PermissionRequest、pending 必被摘除（M12）。"""
    install_mod("veto-mod",
                "export default { permissionRequest(p){ return {deny: 'vetoed'} } }",
                {"hooks": ["permission_request"], "permissions": "tighten"})
    mgr = make_manager("veto-mod")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file", input={"path": "v.txt", "content": "x"})],
        [TextBlock(text="ok")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "write v.txt", auto_respond=None)
    kinds = [e.kind for e in events]
    assert "permission_request" not in kinds
    assert agent._pending == {}  # 无残留：respond_permission 不可能迟到「成功」
    assert agent.respond_permission("whatever-id", "allow_once") is False
    finished = [e for e in events if e.kind == "tool_call_finished"][0]
    assert finished.is_error and "veto" in finished.preview
    assert not (home / "proj" / "v.txt").exists()
    mgr.close()


async def test_mod_note_travels_on_permission_request(home):
    """permissionRequest 的 note 进 PermissionRequest.mod_note（带来源前缀）。"""
    install_mod("note-perm",
                "export default { permissionRequest(p){ return {note: 'please review'} } }",
                {"hooks": ["permission_request"]})
    mgr = make_manager("note-perm")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="write_file", input={"path": "n.txt", "content": "x"})],
        [TextBlock(text="ok")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "write n.txt")
    perms = [e for e in events if isinstance(e, PermissionRequest)]
    assert perms and perms[0].mod_note == "[Mod·note-perm] please review"
    mgr.close()


async def test_tool_post_fires_on_both_harvest_paths(home):
    """tool_post 两路收割点：并发只读批（无 toolPre/declarative 时批照常）与串行。"""
    (home / "proj" / "a.txt").write_text("A", encoding="utf-8")
    (home / "proj" / "b.txt").write_text("B", encoding="utf-8")
    (home / "proj" / "c.txt").write_text("C", encoding="utf-8")
    install_mod("post-mod",
                "export default { toolPost(p){ sky.state[p.tool] ="
                " (sky.state[p.tool] || 0) + 1; return {} } }",
                {"hooks": ["tool_post"]})
    mgr = make_manager("post-mod")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"}),
         ToolUseBlock(id="t2", name="read_file", input={"path": "b.txt"})],
        [ToolUseBlock(id="t3", name="read_file", input={"path": "c.txt"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    await collect(agent, "read files")
    counts = load_mod_state("post-mod")[""]
    assert counts.get("read_file") == 3, "批内两个与串行一个都要触发 tool_post"
    mgr.close()


async def test_iteration_start_fires_per_iteration(home):
    """iteration_start 是迭代级：一次提交（两次模型调用）触发两次。"""
    install_mod("iter-mod",
                "export default { iterationStart(p){ sky.state.n ="
                " (sky.state.n || 0) + 1;"
                " return {ui: [{kind: 'badge', slot: 'stream', text: 'it' + p.iteration}]} } }",
                {"hooks": ["iteration_start"]})
    mgr = make_manager("iter-mod")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "go")
    assert load_mod_state("iter-mod")[""]["n"] == 2
    mod_ui = [e for e in events if isinstance(e, ModUI)]
    assert mod_ui  # 迭代事件照常下发
    mgr.close()


async def test_turn_stop_carries_stop_reason(home):
    install_mod("stop-mod",
                "export default { turnStop(p){ sky.state.reason = p.stopReason;"
                " return {} } }",
                {"hooks": ["turn_stop"]})
    mgr = make_manager("stop-mod")
    provider = FakeProvider([[TextBlock(text="hi")]])
    agent = make_agent(provider, home / "proj", mods=mgr)
    await collect(agent, "hello")
    assert load_mod_state("stop-mod")[""]["reason"] == "end_turn"
    mgr.close()


async def test_mod_widgets_come_out_as_modui_events(home):
    """handler 产出的 widget 以 ModUI 事件下发（slot 原样、mod_id 正确）。"""
    install_mod("ui-mod",
                "export default { toolPre(p){ return {ui: [{kind: 'badge',"
                " slot: 'stream', text: 'seen'}]} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("ui-mod")
    (home / "proj" / "a.txt").write_text("A", encoding="utf-8")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "read")
    mod_ui = [e for e in events if isinstance(e, ModUI)]
    assert any(w.widget.get("text") == "seen" and w.mod_id == "ui-mod" for w in mod_ui)
    assert all(e.slot in ("tray", "stream", "perm") for e in mod_ui)
    mgr.close()


async def test_mod_notice_fires_notice_event(home):
    install_mod("notice-mod",
                "export default { toolPre(p){ return {note: 'watching'} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("notice-mod")
    (home / "proj" / "a.txt").write_text("A", encoding="utf-8")
    provider = FakeProvider([
        [ToolUseBlock(id="t1", name="read_file", input={"path": "a.txt"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, home / "proj", mods=mgr)
    events = await collect(agent, "read")
    notices = [e for e in events if isinstance(e, NoticeEvent)
               and "[Mod·notice-mod]" in e.message]
    assert notices
    mgr.close()


# ---- 启停与热重载 ----


def test_manager_reload_config_toggles(home):
    """reload_config 热改启用名单：关掉的 Mod 不再分发、声明也失效。"""
    install_mod("t-mod", "export default { toolPre(p){ return {note: 'n'} } }",
                {"hooks": ["tool_pre"]})
    mgr = make_manager("t-mod")
    assert asyncio.run(mgr.on_tool_pre("write_file", {}, "s", 0))["note"]
    mgr.reload_config(enabled=True, enabled_ids=[])
    r = asyncio.run(mgr.on_tool_pre("write_file", {}, "s", 0))
    assert not r["note"]
    assert not asyncio.run(mgr.has_tool_pre())
    mgr.close()


def test_set_mods_in_config_roundtrip(home):
    """[mods] 表读写走 config.py（不手拼 TOML）；两组全空删整表。"""
    set_mods_in_config(enabled=True, enabled_mods=["a", "b", "a"])
    _p, raw = _read_raw_config()
    assert raw["mods"] == {"enabled": True, "enabled_mods": ["a", "b"]}
    set_mods_in_config(enabled=False, enabled_mods=[])
    _p, raw = _read_raw_config()
    assert "mods" not in raw


def test_mods_template_is_nonempty_chinese():
    """「让 AI 写 Mod」模板随方法下发（前端不写长文本）。"""
    text = mods_template()
    assert "mod.json" in text and "toolPre" in text and "没有 allow" in text


def test_entry_traversal_rejected(home):
    """清单 entry 指向 Mod 目录之外：装载拒绝（防清单穿越读文件）。"""
    mod_dir = install_mod("traversal-mod", "export default {}",
                          {"entry": "../outside.js"})
    (mods_root() / "outside.js").write_text("export default {}", encoding="utf-8")
    mgr = make_manager("traversal-mod")
    assert mod_dir.is_dir()

    async def main():
        await mgr.mods["traversal-mod"]._ensure_probed()

    asyncio.run(main())
    assert "Mod 目录之外" in mgr.mods["traversal-mod"].load_error
    mgr.close()


def test_assemble_entry_source_forms(home):
    """入口装配：default 导出 / 具名导出 / export { } 三种形状都收对。"""
    js = assemble_entry_source("export default {a: 1}")
    assert "__sky_default__ = {a: 1}" in js
    js = assemble_entry_source("export function onToolCall(p){ return 1 }")
    assert '__sky_named__["onToolCall"] = function onToolCall' in js
    js = assemble_entry_source("export async function boom(){}")
    assert '__sky_named__["boom"] = async function boom' in js
    js = assemble_entry_source("const x = 1; export { x }")
    assert '__sky_named__["x"]' in js
    js = assemble_entry_source("const x = 1; export { x as y }")
    assert '__sky_named__["y"]' in js
