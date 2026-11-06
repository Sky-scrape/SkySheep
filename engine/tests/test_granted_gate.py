"""GrantedGate（运行级「模板授权」代理门）的单元测试。

GrantedGate 是协作模板「本次运行中不再逐次确认」的唯一实现点。它的安全
口径是**只收紧、不放宽**：模板 schema 里没有 gate 字段，本门也不碰
auto_accept_* / persist_rule，只认两种免确认面（具名 WRITE + 工作目录内
落点、命令前缀命中），其余一律交回会话门逐次确认。

这里逐条钉住实现契约里的判定清单：只读放行、工作区内写放行、目录外回退、
引擎主目录回退、删除回退、DANGEROUS 非前缀回退、前缀命中放行、拼接与
代码旗标回退、未列入工具回退，以及 needs_confirm 与 authorize 的一致性。
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from pydantic import BaseModel

from skysheep.security.egress import reset_cache
from skysheep.security.gate import (
    Decision,
    GrantedGate,
    PermissionGate,
    WhitelistRule,
)
from skysheep.tools import (
    EditFileTool,
    MemoryWriteTool,
    ReadFileTool,
    RunCommandTool,
    WriteFileTool,
)
from skysheep.tools.base import Safety, Tool, ToolContext
from skysheep.tools.fs import DeleteFileTool, MoveFileTool

# 命中前缀但要回退的拼接/替换形态（``%VAR%`` 只在 cmd.exe 上成立，
# 见 gate.py 的 _has_shell_chain，POSIX 下不算拼接面）
CHAIN_COMMANDS = [
    "git status; rm -rf /",
    "git status && whoami",
    "git status | cat",
    "git status $(whoami)",
    "git status\necho pwned",
]
if os.name == "nt":
    CHAIN_COMMANDS.append("git status %TEMP%")


class _DangerArgs(BaseModel):
    target: str = ""


class _DangerTool(Tool):
    """只用于证明「被模板显式列进工具表的 DANGEROUS 工具也不自动放行」。"""

    name = "danger_x"
    description = "测试用高危工具"
    safety = Safety.DANGEROUS
    args_model = _DangerArgs

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:  # pragma: no cover
        return "ok"


@pytest.fixture(autouse=True)
def _clear_egress_cache():
    """已知密钥表是进程级缓存：换 SKYSHEEP_HOME 后必须复位。"""
    reset_cache()
    yield
    reset_cache()


@pytest.fixture()
def project(home):
    """工作目录（conftest 的 home 已在 tmp_path/proj 建好）。"""
    return home / "proj"


@pytest.fixture()
def inner(project):
    """被包装的会话权限门（工作目录指向项目）。"""
    return PermissionGate(working_dir=project)


def _gate(inner, *, tools=("write_file", "edit_file", "move_file"), prefixes=()):
    return GrantedGate(
        inner,
        allowed_tools=tools,
        command_prefixes=prefixes,
        run_id="run123",
        label="模板「交付小组」",
    )


# ---- 放行面 ----


async def test_readonly_tool_is_allowed(inner, project):
    """READONLY 不写引擎主目录：与主门一致，直接放行。"""
    gate = _gate(inner)
    assert await gate.authorize(ReadFileTool(), {"path": str(project / "a.txt")}) is None


async def test_write_inside_workdir_allowed_when_listed(inner, project):
    """WRITE + 在 allowed_tools + 落点在工作目录内 → 免确认。"""
    gate = _gate(inner)
    target = str(project / "a.txt")
    assert await gate.authorize(WriteFileTool(), {"path": target, "content": "x"}) is None


async def test_run_command_prefix_hit_is_allowed(inner):
    """命令命中模板声明的前缀（完整词）→ 免确认。"""
    gate = _gate(inner, prefixes=["git status"])
    assert await gate.authorize(RunCommandTool(), {"command": "git status --short"}) is None
    # 词边界：``git statusx`` 不是 ``git status``
    assert await gate.authorize(RunCommandTool(), {"command": "git statusx"}) is not None


# ---- 回退面（必须交回会话门逐次确认） ----


async def test_write_outside_workdir_falls_back(inner, project):
    gate = _gate(inner)
    outside = str(project.parent / "outside.txt")
    pending = await gate.authorize(WriteFileTool(), {"path": outside, "content": "x"})
    assert pending is not None
    assert pending.origin == "模板「交付小组」"
    assert pending.note.startswith("本次运行模板授权：写入落点无法确认在工作目录内")
    pending.resolve(Decision.DENY)


async def test_write_tool_not_in_allowed_falls_back(inner, project):
    gate = _gate(inner, tools=("write_file",))
    pending = await gate.authorize(
        EditFileTool(),
        {"path": str(project / "a.txt"), "old_string": "a", "new_string": "b"},
    )
    assert pending is not None
    # 模板没声明这个工具：不写误导性的「模板授权」注记
    assert "本次运行模板授权" not in pending.note


async def test_engine_home_write_falls_back(inner, home):
    """引擎主目录（凭据 / 全局技能 / 记忆）内的写入永不因模板授权免确认。"""
    gate = _gate(inner)
    pending = await gate.authorize(
        WriteFileTool(),
        {"path": str(home / "home" / "config.toml"), "content": "x"},
    )
    assert pending is not None
    assert "引擎数据目录" in pending.note
    pending.resolve(Decision.DENY)


async def test_memory_write_falls_back(inner):
    """名义 READONLY 却写 ~/.skysheep/memory.md 的固定落点写入同口径。"""
    gate = _gate(inner)
    pending = await gate.authorize(MemoryWriteTool(), {"action": "append", "content": "x"})
    assert pending is not None
    pending.resolve(Decision.DENY)


async def test_delete_file_never_auto_granted(inner, project):
    """DANGEROUS 的删除面不在「本次不提醒」的自动放行范围。"""
    gate = GrantedGate(inner, allowed_tools=["delete_file"], label="模板「清理」")
    pending = await gate.authorize(DeleteFileTool(), {"path": str(project / "a.txt")})
    assert pending is not None
    pending.resolve(Decision.DENY)


async def test_declared_dangerous_tool_never_auto_granted(inner):
    """除 run_command 前缀例外外，任何 DANGEROUS 工具即使被显式列进工具表也不放行。"""
    gate = GrantedGate(inner, allowed_tools=["danger_x"], label="模板「测试」")
    pending = await gate.authorize(_DangerTool(), {"target": "x"})
    assert pending is not None
    pending.resolve(Decision.DENY)


async def test_move_file_overwriting_directory_falls_back(inner, project):
    """move_file(overwrite) 覆盖已有目录 = rmtree，删除语义一律回退。"""
    src = project / "srcdir"
    dst = project / "dstdir"
    (dst / src.name).mkdir(parents=True)
    src.mkdir()
    (src / "x.txt").write_text("x", encoding="utf-8")
    assert src.is_dir() and (dst / src.name).is_dir()  # 前置：确实是删除形态

    gate = _gate(inner)  # move_file 在 allowed_tools 里，双路径都在工作目录内
    pending = await gate.authorize(
        MoveFileTool(), {"source": "srcdir", "destination": "dstdir", "overwrite": True}
    )
    assert pending is not None
    assert "删除语义永不自动放行" in pending.note
    pending.resolve(Decision.DENY)


@pytest.mark.parametrize("command", CHAIN_COMMANDS)
async def test_shell_chain_falls_back(inner, command):
    """前缀命中但带 shell 拼接/替换 → 回退逐次确认。"""
    gate = _gate(inner, prefixes=["git status"])
    pending = await gate.authorize(RunCommandTool(), {"command": command})
    assert pending is not None
    assert "本次运行模板授权" in pending.note
    pending.resolve(Decision.DENY)


async def test_interpreter_code_flag_falls_back(inner):
    """解释器代码旗标（python -c）不因前缀授权放行。"""
    gate = _gate(inner, prefixes=["git status"])
    pending = await gate.authorize(RunCommandTool(), {"command": 'python -c "print(1)"'})
    assert pending is not None
    pending.resolve(Decision.DENY)


async def test_undeclared_prefix_falls_back(inner):
    gate = _gate(inner, prefixes=["git status"])
    pending = await gate.authorize(RunCommandTool(), {"command": "npm test"})
    assert pending is not None
    assert "本次运行模板授权" not in pending.note
    pending.resolve(Decision.DENY)


async def test_non_run_actions_fall_back(inner):
    """后台进程管理动作（read/kill/list）不是命令行，不在前缀授权范围。"""
    gate = _gate(inner, prefixes=["git status"])
    for tool_input in (
        {"action": "read", "command": "git status"},
        {"action": "kill", "command": "git status", "id": 1},
        {"action": "list"},
    ):
        pending = await gate.authorize(RunCommandTool(), tool_input)
        assert pending is not None, tool_input
        assert "后台进程管理动作" in pending.note
        pending.resolve(Decision.DENY)


# ---- 构造期兜底：危险前缀直接丢弃 ----


def test_unsafe_prefixes_are_dropped_at_construction(inner):
    """单词前缀 / 解释器前缀 / 自带拼接的前缀一律不进授权面。"""
    gate = GrantedGate(
        inner,
        allowed_tools=["run_command"],
        command_prefixes=["python", "python -c", "bash", "git status; rm -rf /", "git status"],
    )
    assert gate.command_prefixes == ("git status",)


async def test_interpreter_prefix_never_grants(inner):
    """兜底过滤后 ``python <任意脚本>`` 仍逐次确认（R1 形的放行面）。"""
    gate = GrantedGate(inner, command_prefixes=["python", "bash"])
    assert gate.command_prefixes == ()
    pending = await gate.authorize(RunCommandTool(), {"command": "python /tmp/evil.py"})
    assert pending is not None
    pending.resolve(Decision.DENY)


# ---- 委托语义 ----


async def test_force_pending_delegates(inner, project):
    """Mod 点名 ask（force_pending）压得住模板授权。"""
    gate = _gate(inner)
    pending = await gate.authorize(
        WriteFileTool(),
        {"path": str(project / "a.txt"), "content": "x"},
        force_pending=True,
    )
    assert pending is not None
    pending.resolve(Decision.DENY)


async def test_mods_tightening_delegates(project):
    """会话门的 Mods 收紧声明命中即跳过模板授权的全部放行分支。"""
    inner = PermissionGate(working_dir=project, extra_confirm=lambda tool, inp: True)
    gate = GrantedGate(inner, allowed_tools=["write_file"], command_prefixes=["git status"])
    pending = await gate.authorize(WriteFileTool(), {"path": str(project / "a.txt"), "content": "x"})
    assert pending is not None
    assert "Mods 收紧声明" in pending.note
    pending.resolve(Decision.DENY)
    assert await gate.authorize(RunCommandTool(), {"command": "git status"}) is not None


async def test_inner_note_is_kept(inner):
    """模板注记只做前缀，不清空内部门自己的说明。"""
    inner.add_session_rule(WhitelistRule(tool="run_command", kind="prefix", pattern="git status"))
    gate = _gate(inner, prefixes=["git status"])
    pending = await gate.authorize(RunCommandTool(), {"command": "git status; rm -rf /"})
    assert pending is not None
    assert pending.note.startswith("本次运行模板授权：命令含管道/拼接或解释器代码旗标；")
    assert "该命令以白名单里的前缀开头" in pending.note
    pending.resolve(Decision.DENY)


async def test_known_secret_is_pre_denied(home, project):
    """出站密钥防线先于一切放行分支：已知密钥值命中直接 DENY，模板也救不了。"""
    cfg_dir = home / "home"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "mcp.json").write_text(
        json.dumps({
            "mcpServers": {
                "notion": {
                    "url": "https://mcp.notion.example/mcp",
                    "headers": {"Authorization": "Bearer rmtp_live_grantedgate_001"},
                }
            }
        }),
        encoding="utf-8",
    )
    reset_cache()

    gate = _gate(PermissionGate(working_dir=project))
    pending = await gate.authorize(
        WriteFileTool(),
        {"path": str(project / "leak.txt"), "content": "Bearer rmtp_live_grantedgate_001"},
    )
    assert pending is not None
    assert await asyncio.wait_for(pending.wait(), timeout=2) == Decision.DENY


# ---- needs_confirm 与 authorize 一致性 ----


async def test_needs_confirm_agrees_with_authorize(inner, project):
    """并发只读批只看 needs_confirm 就执行（core/agent.py:935/959），
    两处口径必须一致：needs_confirm 为 False 当且仅当 authorize 返回 None。"""
    gate = _gate(inner, prefixes=["git status"])
    cases = [
        (ReadFileTool(), {"path": "a.txt"}),
        (MemoryWriteTool(), {"action": "append", "content": "x"}),
        (WriteFileTool(), {"path": str(project / "a.txt"), "content": "x"}),
        (WriteFileTool(), {"path": str(project.parent / "out.txt"), "content": "x"}),
        (DeleteFileTool(), {"path": str(project / "a.txt")}),
        (RunCommandTool(), {"command": "git status"}),
        (RunCommandTool(), {"command": "git status; rm -rf /"}),
        (RunCommandTool(), {"command": "npm test"}),
        (RunCommandTool(), {"action": "kill", "command": "git status", "id": 1}),
    ]
    for tool, tool_input in cases:
        pending = await gate.authorize(tool, tool_input)
        assert gate.needs_confirm(tool, tool_input) == (pending is not None), tool.name
        if pending is not None:
            pending.resolve(Decision.DENY)


# ---- 属性透传 ----


def test_attributes_pass_through_to_inner(inner):
    gate = GrantedGate(inner, allowed_tools=["write_file"], run_id="r1", label="L")
    assert gate.working_dir == inner.working_dir
    assert gate.run_id == "r1"
    assert gate.label == "L"
    assert gate.allowed_tools == frozenset({"write_file"})
    assert gate.command_prefixes == ()
    # 未定义的方法走 __getattr__ 落到内部门
    assert callable(gate.rule_for)
    assert gate.rule_for(WriteFileTool(), {"path": "a.txt", "content": "x"}).kind == "always"
    # 非下划线属性的**写**也落在内部门上（backend 设置档位走的就是这条路）
    gate.auto_accept_write = True
    assert inner.auto_accept_write is True
    assert gate.auto_accept_write is True
