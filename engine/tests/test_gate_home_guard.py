# 引擎主目录写入守卫（2026-09-25 审查 P2-7 的回归）。
#
# ~/.skysheep 是凭据（config.toml / mcp.json）与全局技能（注入所有项目——
# 含未信任项目——的 system prompt）的落点。白名单沉淀的整工具 always 规则
# 一旦能免确认写这里，就等于跨信任边界的自由写入面。锁定口径：
# authorize 的规则放行分支必须拦下、rule_for 只允许固化当次参数（exact）。
import pytest

from skysheep.security.gate import Decision, PermissionGate
from skysheep.tools import WriteFileTool


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
