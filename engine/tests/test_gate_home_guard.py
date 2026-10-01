# 引擎主目录写入守卫（2026-09-25 审查 P2-7 的回归）。
#
# ~/.skysheep 是凭据（config.toml / mcp.json）与全局技能（注入所有项目——
# 含未信任项目——的 system prompt）的落点。白名单沉淀的整工具 always 规则
# 一旦能免确认写这里，就等于跨信任边界的自由写入面。锁定口径：
# authorize 的规则放行分支必须拦下、rule_for 只允许固化当次参数（exact）。
import asyncio

import pytest

from skysheep.channels.gate import ChannelGate
from skysheep.core.subagent import IsolatedGate, SubagentGate
from skysheep.security.gate import Decision, HeadlessGate, PermissionGate, WhitelistRule
from skysheep.tools import MemoryWriteTool, ReadFileTool, WriteFileTool


@pytest.fixture()
def engine_home(home):
    """conftest 的 home 返回 tmp_path；SKYSHEEP_HOME 在其下的 home/ 子目录。"""
    return home / "home"


@pytest.fixture()
def gate(home):
    """工作目录指向测试项目（引擎主目录之外）的权限门。"""
    return PermissionGate(working_dir=home / "proj")


async def test_always_rule_cannot_write_engine_home(gate, engine_home):
    """即使沉淀了 write_file 整工具 always 规则，写 ~/.skysheep 仍要逐次确认。"""
    rule = gate.rule_for(WriteFileTool(), {"path": str(gate.working_dir / "a.txt"), "content": "x"},
                         gate.working_dir)
    assert rule.kind == "always"
    gate.session_rules.append(rule)

    inside = await gate.authorize(
        WriteFileTool(), {"path": str(gate.working_dir / "a.txt"), "content": "x"}
    )
    assert inside is None  # 工作目录内：整工具规则照常放行

    pending = await gate.authorize(
        WriteFileTool(),
        {"path": str(engine_home / "skills" / "evil" / "SKILL.md"), "content": "注入"},
    )
    assert pending is not None  # 引擎主目录内：规则失效，退回逐次确认
    pending.resolve(Decision.DENY)


@pytest.mark.parametrize("target", [
    "skills/evil/SKILL.md",       # 全局技能（prompt 注入面）
    "config.toml",                # 凭据本体
    "mcp.json",                   # stdio 命令执行面
    "workspace-trust.json",       # 信任记录本身
])
async def test_engine_home_subpaths_all_guarded(gate, engine_home, target):
    """主目录下任意落点都在守卫范围内（不只是 skills）。"""
    rule = gate.rule_for(WriteFileTool(), {"path": "a.txt", "content": "x"}, gate.working_dir)
    gate.session_rules.append(rule)
    pending = await gate.authorize(
        WriteFileTool(), {"path": str(engine_home / target), "content": "x"}
    )
    assert pending is not None, target


async def test_rule_for_yields_exact_for_engine_home_writes(gate, engine_home):
    """「总是允许」引擎主目录内的写入只固化当次参数，不产整工具规则。"""
    target = str(engine_home / "skills" / "x" / "SKILL.md")
    rule = gate.rule_for(
        WriteFileTool(), {"path": target, "content": "x"}, gate.working_dir
    )
    assert rule.kind == "exact"
    assert "SKILL.md" in rule.pattern and rule.kind == "exact"


async def test_auto_accept_write_still_guards_home(gate, engine_home):
    """自动编辑档（auto_accept_write）要求落点在工作目录内，主目录写入本就不过。"""
    gate.auto_accept_write = True
    pending = await gate.authorize(
        WriteFileTool(), {"path": str(engine_home / "config.toml"), "content": "x"}
    )
    assert pending is not None


async def test_relative_path_outside_workdir_not_flagged(tmp_path):
    """无工作目录的相对路径解析不了：不误报，交给工具层。"""
    gate = PermissionGate(working_dir=None)
    rule = gate.rule_for(WriteFileTool(), {"path": "a.txt", "content": "x"}, None)
    gate.session_rules.append(rule)
    result = await gate.authorize(WriteFileTool(), {"path": "a.txt", "content": "x"})
    assert result is None  # 相对路径 + 无工作目录：守卫不参与


async def test_headless_allowed_list_still_guards_engine_home(home, engine_home):
    """无人值守预授权名单也要过引擎主目录守卫（审查 P2-7 回归）。

    流水线节点用 HeadlessGate(allowed=node["allowed_tools"]) 构造：名单含
    write_file 时，写 ~/.skysheep/config.toml（凭据本体）或全局技能不得因
    名单免确认。无人值守没有用户可应答——命中守卫落父类 authorize 随即被
    resolve(DENY)，fail-closed。
    """
    gate = HeadlessGate(allowed=["write_file"], working_dir=home / "proj")
    # 名单内 + 工作目录内：照常放行（守卫不扩大打击面）
    assert (
        await gate.authorize(
            WriteFileTool(), {"path": str(gate.working_dir / "a.txt"), "content": "x"}
        )
        is None
    )
    # 名单内 + 引擎主目录内：不得放行，落成已拒绝
    pending = await gate.authorize(
        WriteFileTool(), {"path": str(engine_home / "config.toml"), "content": "x"}
    )
    assert pending is not None
    assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY


async def test_headless_allowed_list_still_covers_whole_engine_home(home, engine_home):
    """守卫范围是整个引擎主目录（skills / mcp.json / workspace-trust.json），不只是 config。"""
    gate = HeadlessGate(allowed=["write_file"], working_dir=home / "proj")
    for target in ("skills/evil/SKILL.md", "mcp.json", "workspace-trust.json"):
        pending = await gate.authorize(
            WriteFileTool(), {"path": str(engine_home / target), "content": "x"}
        )
        assert pending is not None, target
        assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY, target


# ---- 第二轮审查 FINDING 2：READONLY 名义的固定落点写入（memory_write） ----
# memory_write 是 READONLY 工具却写 ~/.skysheep/memory.md，而该文件注入所有
# 项目所有会话的 system prompt。旧实现 READONLY 短路在前，P2-7 守卫对它
# 结构性不可达：主门 / HeadlessGate / ChannelGate 全部零确认放行。
# 锁定口径：写引擎主目录的判定先于 READONLY 短路（各门同口径），命中即逐次
# 确认（无人值守/子代理 fail-closed 落 DENY），白名单（含整工具 always）沉淀
# 不出放行。remember_lines 后台自动归档不是工具调用、不经过门，属有意保留的
# 产品语义，不在本守卫范围（残余面注记见 tools/memory.py）。


def _memory_input(action: str = "append", content: str = "以后都用 uv 装依赖") -> dict:
    return {"action": action, "content": content}


async def test_memory_write_requires_confirmation_main_gate(gate):
    """主门：memory_write append 不再因 READONLY 零确认放行。"""
    pending = await gate.authorize(MemoryWriteTool(), _memory_input())
    assert pending is not None
    pending.resolve(Decision.DENY)


async def test_memory_write_whitelist_cannot_bypass(gate):
    """整工具 always 规则（历史遗留形态）也放不了 memory_write。"""
    gate.session_rules.append(WhitelistRule(tool="memory_write", kind="always"))
    assert await gate.authorize(MemoryWriteTool(), _memory_input()) is not None


async def test_memory_write_allow_always_yields_exact_and_still_confirms(gate):
    """「总是允许」只固化当次参数（exact），且 exact 规则同样不能免确认。"""
    tool = MemoryWriteTool()
    tool_input = _memory_input()
    rule = gate.rule_for(tool, tool_input, gate.working_dir)
    assert rule.kind == "exact"
    gate.session_rules.append(rule)
    assert await gate.authorize(tool, tool_input) is not None


async def test_memory_write_list_action_also_confirms(gate):
    """门按工具级粒度判定（固定落点写入者）：list 虽是纯读也一并确认——
    保守方向多问一次，避免在门里解析工具参数语义。"""
    assert await gate.authorize(MemoryWriteTool(), {"action": "list"}) is not None


async def test_memory_write_headless_denied_even_when_allowed(home):
    """无人值守门：memory_write 不因 READONLY 免确认；预授权名单也救不了它，
    落父类 authorize 后随即 resolve(DENY)，fail-closed。"""
    gate = HeadlessGate(allowed=["memory_write"], working_dir=home / "proj")
    pending = await gate.authorize(MemoryWriteTool(), _memory_input())
    assert pending is not None
    assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY


async def test_memory_write_channel_gate_requires_approval(home):
    """渠道门：未开审批 → 立即拒绝；开了审批 → 推聊天卡逐次确认。"""
    tool = MemoryWriteTool()
    closed = ChannelGate(working_dir=home / "proj")
    pending = await closed.authorize(tool, _memory_input())
    assert pending is not None
    assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY

    opened = ChannelGate(working_dir=home / "proj", approve_enabled=True, approve_timeout=5)

    async def approve(p):
        # 审批卡推到聊天窗口后用户回复「总是允许」：渠道层把聊天文本经
        # parse_decision 解析成决策值再投递 submit；「总是允许」在渠道端
        # 降级为单次放行（submit 的 _effective_decision）
        opened.submit(p.request_id, Decision.ALLOW_ALWAYS)

    opened.notify = approve
    pending2 = await opened.authorize(tool, _memory_input())
    assert pending2 is not None
    assert await asyncio.wait_for(pending2.wait(), timeout=2) == Decision.ALLOW_ONCE


async def test_memory_write_subagent_gates_fail_closed():
    """子代理门（只读放行 / 隔离工作区）同口径：memory_write 预拒绝，不留零确认放行。"""
    for gate in (SubagentGate(), IsolatedGate()):
        pending = await gate.authorize(MemoryWriteTool(), _memory_input())
        assert pending is not None
        assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY


async def test_other_readonly_tools_still_auto_allowed(home, gate):
    """守卫不扩大打击面：其余 READONLY 工具各门照旧免确认。"""
    assert await gate.authorize(ReadFileTool(), {"path": "a.txt"}) is None
    headless = HeadlessGate(allowed=[], working_dir=home / "proj")
    assert await headless.authorize(ReadFileTool(), {"path": "a.txt"}) is None
    channel = ChannelGate(allowed=[], working_dir=home / "proj")
    assert await channel.authorize(ReadFileTool(), {"path": "a.txt"}) is None
    assert await SubagentGate().authorize(ReadFileTool(), {"path": "a.txt"}) is None
    assert await IsolatedGate().authorize(ReadFileTool(), {"path": "a.txt"}) is None


# ---- 并发只读批的收集期预检（安全审查：只读批绕门回归） ----
# Agent 的并发批收集只看 safety 分级就会把 memory_write 收进批零确认执行，
# authorize 里的引擎主目录守卫被整段架空。收集期改问纯判定谓词
# gate.needs_confirm（不创建 PendingPermission、不触发 on_request），命中即
# 断批回退串行走真 authorize。这里钉两件事：谓词对 FINDING 2 各门口径的
# 判定正确；谓词与 authorize 的结论一致（防两处漂移）。


async def test_needs_confirm_flags_memory_write_not_plain_readonly(gate):
    """memory_write（名义 READONLY、落点引擎主目录）必须命中预检；真只读不命中。"""
    assert gate.needs_confirm(MemoryWriteTool(), _memory_input()) is True
    assert gate.needs_confirm(MemoryWriteTool(), {"action": "list"}) is True
    assert gate.needs_confirm(ReadFileTool(), {"path": "a.txt"}) is False


async def test_needs_confirm_auto_accept_all_clears_memory_write(gate):
    """完全访问档与 authorize 同界：auto_accept_all 连引擎主目录写入也放行。"""
    gate.auto_accept_all = True
    assert gate.needs_confirm(MemoryWriteTool(), _memory_input()) is False


async def test_needs_confirm_whitelist_cannot_clear_memory_write(gate):
    """白名单（含整工具 always）沉淀不出 memory_write 放行，预检同样命中。"""
    gate.session_rules.append(WhitelistRule(tool="memory_write", kind="always"))
    assert gate.needs_confirm(MemoryWriteTool(), _memory_input()) is True


async def test_needs_confirm_agrees_with_authorize_for_readonly_candidates(home, engine_home):
    """预检谓词与 authorize 对 READONLY 候选的结论一致（并发批的实际判定域）。

    各门 × 各档位 × {真只读, 名义只读写主目录}：needs_confirm 为 False 当且
    仅当 authorize 返回 None。两处口径一旦漂移，批路径就会多拦（体验回退）
    或漏放（安全回退），这组对照把它们钉死。
    """
    candidates = [
        (ReadFileTool(), {"path": "a.txt"}),
        (MemoryWriteTool(), _memory_input()),
    ]
    gates = []
    base = PermissionGate(working_dir=home / "proj")
    gates.append(base)
    all_access = PermissionGate(working_dir=home / "proj")
    all_access.auto_accept_all = True
    gates.append(all_access)
    auto_write = PermissionGate(working_dir=home / "proj")
    auto_write.auto_accept_write = True
    gates.append(auto_write)
    gates.append(HeadlessGate(allowed=["memory_write"], working_dir=home / "proj"))
    gates.append(ChannelGate(allowed=["memory_write"], working_dir=home / "proj"))
    gates.append(SubagentGate())
    gates.append(IsolatedGate())

    for g in gates:
        for tool, inp in candidates:
            pending = await g.authorize(tool, inp)
            expected = pending is not None
            assert g.needs_confirm(tool, inp) == expected, (type(g).__name__, tool.name)
            if pending is not None:
                pending.resolve(Decision.DENY)
