# 审计 SEC-M01（全面测试与灰度评估报告 2026-10-25）回归：run_command 前缀
# 白名单的「任意执行面」守卫与解释器判定必须落在**首个真实命令词**上。
#
# 两个已复核的缺口：
#   1) 以 NAME=value 前置赋值（或 env/sudo/nohup 等包装器）开头的命令把
#      docker/make/go/npx… 挤出首词——旧 rule_for 沉淀出 "TAG=latest docker"
#      这类宽前缀，之后任意 `docker run` / `make -f` 免确认；
#   2) 旧 _INTERPRETER_SCAN_WINDOW=8 硬截 8 个 token：8 个前置赋值即可把
#      解释器推出窗口，`python -c` 逃过 code-exec 判定、沉淀 "A=1 B=2" 前缀。
#
# 修复口径：提炼（rule_for）与匹配（WhitelistRule._matches_core）共用
# 「取首个真实命令词」扫描（_first_real_command_word）；提炼不出可信真实
# 命令词一律 exact（fail closed，绝不沉淀宽前缀）；历史已沉淀的这类前缀
# 规则在匹配侧 fail-closed（含扫描上限截断的形态）。
#
# 审查 F6.1 补充：前缀提炼锚定在「首个非前置赋值词」上——sudo/doas 三词
# 前缀与通用两词前缀都不再按裸 words[:2]/words[:3] 提炼（旧实现对
# `FOO=1 sudo apt install` 沉淀 "FOO=1 sudo"，把 `FOO=1 sudo rm -rf /`
# 合并进同一放行面）；钉不住三词形状的提权命令（旗标/词数/赋值收尾）
# 一律 exact，匹配侧把这类形状的前缀规则（含历史沉淀与手工创建）判死
# （_sudo_prefix_shape_unsafe，两侧同源）。
#
# 所有用例只断言规则判定（rule_for / matches / authorize），不真实执行任何
# docker/make/python 载荷。
from __future__ import annotations

import pytest

from skysheep.security.gate import (
    PermissionGate,
    WhitelistRule,
    _has_code_exec_flag,
    _interpreter_invocation,
    _is_arbitrary_exec_prefix,
)
from skysheep.tools.shell import RunCommandTool


def _rule_for(command: str) -> WhitelistRule:
    return PermissionGate.rule_for(RunCommandTool(), {"command": command})


# ---- 缺口 1（提炼侧）：前置赋值/包装器挤出首词的任意执行面入口 → exact ----


@pytest.mark.parametrize("command", [
    "TAG=latest docker build .",
    "DOCKER_BUILDKIT=1 docker run -v /:/host evil",
    "PIP_INDEX_URL=https://x make -f /tmp/evil.mk",
    "GOFLAGS=-mod=mod go run /tmp/evil.go",
    "KUBECONFIG=/tmp/k npx evil-pkg",
    "env docker build .",  # 包装器挤出首词，同类形态
    "sudo docker build .",  # 提权包装器的三词前缀同样受任意执行面守卫约束
    "nohup make -f /tmp/evil.mk",
])
def test_assignment_prefixed_unsafe_entry_never_sinks_prefix(command: str) -> None:
    """首词是赋值/包装器、真实命令词落在任意执行面入口清单的命令：
    「总是允许」只固化当次参数，绝不沉淀宽前缀。"""
    rule = _rule_for(command)
    assert rule.kind == "exact" and rule.pattern == command, (command, rule)


@pytest.mark.parametrize("command", ["FOO=bar", "A=1 B=2 C=3", "sudo", "env", "action=run"])
def test_no_trusted_real_command_word_yields_exact(command: str) -> None:
    """提炼不出可信真实命令词（全是前置赋值/包装器收尾）→ 一律 exact：
    宽前缀等于放行「该前缀后接任意命令」（含裸 `sudo`/`env`）。"""
    rule = _rule_for(command)
    assert rule.kind == "exact" and rule.pattern == command, (command, rule)


# ---- 缺口 2：解释器扫描不再被 8 个 token 硬截 ----

_TWELVE = "A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 I=9 J=10 K=11 L=12"


def test_exactly_eight_assignments_no_longer_hide_interpreter() -> None:
    """报告复现原样：8 个前置赋值正好填满旧窗口，python -c 逃过判定。"""
    command = "A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 python -c 'print(1)'"
    assert _interpreter_invocation(command)
    assert _has_code_exec_flag(command)
    rule = _rule_for(command)
    assert rule.kind == "exact" and rule.pattern == command


def test_interpreter_scan_survives_more_than_eight_leading_assignments() -> None:
    command = f"{_TWELVE} python -c 'print(1)'"
    assert _interpreter_invocation(command)
    assert _has_code_exec_flag(command)
    assert _rule_for(command).kind == "exact"


def test_interpreter_scan_survives_wrapper_then_assignments() -> None:
    command = f"sudo -u root {_TWELVE} python -c 'print(1)'"
    assert _interpreter_invocation(command)
    assert _has_code_exec_flag(command)


def test_scan_cap_is_fail_closed_for_generation() -> None:
    """超长前置赋值链越过总扫描上限：解释器扫描不误报（找不到即找不到），
    但提炼侧拿不到可信真实命令词 → 一律 exact，绝不沉淀宽前缀。"""
    assignments = " ".join(f"V{i}=1" for i in range(70))
    command = f"{assignments} python -c 'print(1)'"
    assert not _interpreter_invocation(command)
    rule = _rule_for(command)
    assert rule.kind == "exact" and rule.pattern == command


# ---- 普通命令不受影响（既有口径回归锁定） ----


@pytest.mark.parametrize("command,pattern", [
    ("git status --short", "git status"),
    ("uv run pytest -q", "uv run"),
    ("kubectl get pods", "kubectl get"),
    ("sudo apt install htop", "sudo apt install"),
    ("env git status", "env git"),
    # F6.1 改此断言：旧值 "FOO=bar git" 是按裸 words[:2] 提炼的合并前缀——
    # 只钉住一个真实命令词 git，放行面覆盖任意 git 子命令。新口径锚定在
    # 首个非前置赋值词上，两词前缀 = 锚词 + 下一词（git status），
    # 前置赋值原样垫前。
    ("FOO=bar git status", "FOO=bar git status"),
    ("nohup python x.py", "nohup python"),
])
def test_normal_commands_keep_two_word_prefix(command: str, pattern: str) -> None:
    """安全真实命令词的普通命令照旧沉淀两词前缀（修复不得一刀切 exact）。"""
    rule = _rule_for(command)
    assert rule.kind == "prefix" and rule.pattern == pattern, (command, rule)


def test_action_semantics_keep_action_scoped_prefix() -> None:
    """action=read/kill/list 的动作语义文本不是 shell 命令行，照旧按动作
    粒度沉淀前缀（test_gate.py 的动作级白名单口径不变）。"""
    rule = PermissionGate.rule_for(
        RunCommandTool(), {"command": "", "action": "read", "id": 1})
    assert rule.kind == "prefix" and rule.pattern == "action=read"
    kill_rule = PermissionGate.rule_for(
        RunCommandTool(), {"action": "kill", "id": 3, "command": "git status"})
    assert kill_rule.kind == "prefix" and kill_rule.pattern == "action=kill id=3"


# ---- 匹配侧：历史已沉淀的宽前缀规则 fail-closed ----


@pytest.mark.parametrize("pattern,evil", [
    ("TAG=latest docker", "TAG=latest docker run -v /:/host -v ~/.ssh:/k evil"),
    ("TAG=latest docker", "TAG=latest docker build ."),
    ("env docker", "env docker run -v /:/host evil"),
    ("A=1 B=2", "A=1 B=2 docker run -v /:/host evil"),
    ("A=1 B=2", "A=1 B=2 make -f /tmp/evil.mk"),
    ("A=1 B=2", "A=1 B=2 python -c 'print(1)'"),
    ("A=1 B=2", "A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 python -c 'print(1)'"),
])
def test_legacy_prefix_rules_do_not_unlock_real_command(pattern: str, evil: str) -> None:
    """历史沉淀的 "TAG=latest docker" 类前缀、全赋值前缀 "A=1 B=2"：
    其后的任意执行面命令 / 任意代码不得免确认。"""
    rule = WhitelistRule(tool="run_command", kind="prefix", pattern=pattern)
    assert not rule.matches("run_command", evil), (pattern, evil)


def test_cap_truncated_command_fails_closed_on_match() -> None:
    """超长前置赋值链把真实命令推到扫描上限外：按任意执行面 fail-closed，
    不得借历史前缀规则免确认（解释器与入口命令两种形态都拦）。"""
    assignments = " ".join(f"V{i}=1" for i in range(70))
    rule = WhitelistRule(tool="run_command", kind="prefix", pattern="A=1 B=2")
    for evil in (
        f"{assignments} docker run -v /:/host evil",
        f"{assignments} python -c 'print(1)'",
    ):
        assert not rule.matches("run_command", evil), evil[:80]


@pytest.mark.parametrize("pattern,text", [
    ("git status", "git status --short"),
    ("git status", "git status"),
    ("sudo apt install", "sudo apt install htop"),
    ("kubectl get", "kubectl get pods"),
    ("python manage.py", "python manage.py runserver"),
    ("env git", "env git status"),
    ("action=read", "action=read"),
])
def test_benign_legacy_prefix_rules_still_match(pattern: str, text: str) -> None:
    """安全真实命令词的历史前缀规则照常命中（修复不得把正常命令变成
    永远无法放行）。"""
    rule = WhitelistRule(tool="run_command", kind="prefix", pattern=pattern)
    assert rule.matches("run_command", text), (pattern, text)


# ---- _is_arbitrary_exec_prefix：按首个真实命令词判定（设置页失效标记同源） ----


@pytest.mark.parametrize("pattern", [
    "docker run",
    "TAG=latest docker",
    "env docker",
    "sudo docker build",
    "make -f",
    "wsl -d",
    "ssh host",
])
def test_arbitrary_exec_prefix_judged_on_real_command_word(pattern: str) -> None:
    assert _is_arbitrary_exec_prefix(pattern), pattern


@pytest.mark.parametrize("pattern", [
    "git status",
    "uv run",
    "kubectl get pods",
    "sudo apt install",
    "FOO=1 sudo apt install",  # F6.1：赋值垫前的三词形状照常有效
    "TAG=latest git",
    "env git",
    "python script.py",
    "action=read",  # 动作语义文本：不算任意执行面（动作级前缀是既有口径）
    "",  # 空 pattern 本就匹配不了任何命令（backend 的 stale 判定不标它）
])
def test_benign_prefix_not_marked_arbitrary(pattern: str) -> None:
    assert not _is_arbitrary_exec_prefix(pattern), pattern


# ---- 审查 F6.1：提权包装器锚定与两词前缀的赋值垫前 ----
#
# 旧实现按裸 words 提炼：sudo 三词分支只认 words[0] 是 sudo/doas，`FOO=1
# sudo apt install` 落进通用两词分支沉淀 "FOO=1 sudo"，与 `FOO=1 sudo
# rm -rf /` 合并成同一放行面；`sudo rm -rf /` / `sudo apt` 也沉淀出
# "sudo rm" / "sudo apt" 两词残前缀。修复后与 SEC-M01 同口径：锚「首个
# 非前置赋值词」，钉不住三词形状的提权命令一律 exact，匹配侧把这类形状
# 的前缀规则（历史沉淀 + 手工创建）判死（_sudo_prefix_shape_unsafe，
# 两侧同源）。Windows / POSIX 两形状都测：_tokenize 在 Windows 走
# posix=False、POSIX 走 posix=True，分词路径不同判定必须一致。
#
# 所有用例只断言规则判定（rule_for / matches / authorize），命令串从不
# 进入任何 shell。


@pytest.mark.parametrize("command,pattern", [
    # 既有口径回归：裸 sudo 三词前缀各成一类
    ("sudo apt install htop", "sudo apt install"),
    ("sudo apt remove bar", "sudo apt remove"),
    ("doas apt install x", "doas apt install"),
    # F6.1 核心：赋值垫前时锚在 sudo 上，三词前缀钉住子命令，不再合并放行面
    ("FOO=1 sudo apt install htop", "FOO=1 sudo apt install"),
    ("FOO=1 sudo apt remove bar", "FOO=1 sudo apt remove"),
    ("FOO=1 doas apt install x", "FOO=1 doas apt install"),
    # 命令词（rm）被钉进前缀、参数原样保留：放行面只覆盖「同前缀 + 任意
    # 追加参数」，与 sudo apt install 的既有取舍同档（旧实现对这两条分别
    # 沉淀 "FOO=1 sudo" / "sudo doas rm"——前者更宽，后者同宽）。
    ("FOO=1 sudo rm x", "FOO=1 sudo rm x"),
    ("sudo doas rm -rf x", "sudo doas rm"),
])
def test_sudo_anchor_keeps_three_word_prefix(command: str, pattern: str) -> None:
    """提权包装器锚在首个非前置赋值词上：三词形状钉得住子命令。"""
    rule = _rule_for(command)
    assert rule.kind == "prefix" and rule.pattern == pattern, (command, rule)


@pytest.mark.parametrize("command", [
    "FOO=1 sudo rm -rf /",
    "FOO=1 doas rm -rf /",
    "sudo rm -rf /",           # 旧实现沉淀 "sudo rm"，任意 root 删除免确认
    "sudo apt",                # 词数不足：旧实现沉淀 "sudo apt"，合并子命令
    "FOO=1 sudo apt",
    "FOO=1 sudo",              # 裁判点名的两词残形状
    "sudo",                    # 裸 sudo（既有口径回归）
    "sudo -u root rm -rf /",   # 旗标收尾（对抗审查第二轮既有口径）
    "FOO=1 sudo -u root python x",  # 赋值垫前的旗标收尾：旧实现同样漏
    "sudo env FOO=1 rm -rf /",  # 第三词收在赋值上：真实命令未出现
])
def test_unpinned_sudo_shapes_yield_exact(command: str) -> None:
    """sudo/doas 锚定但钉不住三词形状（旗标/词数/赋值收尾）→ 一律 exact，
    绝不沉淀两词残前缀。"""
    rule = _rule_for(command)
    assert rule.kind == "exact" and rule.pattern == command, (command, rule)


@pytest.mark.parametrize("pattern,evil", [
    # 裁判点名的复现：'FOO=1 sudo' 规则不得放行 'FOO=1 sudo rm -rf /'
    ("FOO=1 sudo", "FOO=1 sudo rm -rf /"),
    ("FOO=1 sudo", "FOO=1 sudo apt install htop"),
    ("FOO=1 doas", "FOO=1 doas rm -rf /"),
    ("sudo", "sudo rm -rf /"),
    ("sudo", "sudo apt install htop"),
    ("sudo apt", "sudo apt remove docker"),   # 两词残前缀不再合并子命令
    ("sudo rm", "sudo rm -rf /tmp/x"),
    ("sudo -u root", "sudo -u root rm -rf /"),
])
def test_legacy_sudo_prefix_rules_fail_closed(pattern: str, evil: str) -> None:
    """历史沉淀 / 手工创建的提权两词残前缀规则：匹配侧 fail-closed，
    不再命中任何命令，回落逐次确认。"""
    rule = WhitelistRule(tool="run_command", kind="prefix", pattern=pattern)
    assert not rule.matches("run_command", evil), (pattern, evil)


@pytest.mark.parametrize("pattern,text", [
    ("sudo apt install", "sudo apt install htop"),
    ("FOO=1 sudo apt install", "FOO=1 sudo apt install htop"),
    ("doas apt install", "doas apt install htop"),
])
def test_pinned_sudo_prefix_rules_still_match(pattern: str, text: str) -> None:
    """三词形状的提权前缀照常命中（保留免确认便利，修复不得一刀切死）。"""
    rule = WhitelistRule(tool="run_command", kind="prefix", pattern=pattern)
    assert rule.matches("run_command", text), (pattern, text)


@pytest.mark.parametrize("is_windows", [False, True])
def test_sudo_anchor_shapes_on_both_platforms(monkeypatch, is_windows: bool) -> None:
    """Windows 与 POSIX 两形状：分词路径不同（shlex posix=False/True），
    提炼与匹配的判定结果必须一致（裁判点名的两个形状不得合并）。"""
    from skysheep.security import gate as gate_mod

    monkeypatch.setattr(gate_mod, "_IS_WINDOWS", is_windows)
    install = _rule_for("FOO=1 sudo apt install htop")
    assert install.kind == "prefix"
    assert install.pattern == "FOO=1 sudo apt install"
    rm = _rule_for("FOO=1 sudo rm -rf /")
    assert rm.kind == "exact" and rm.pattern == "FOO=1 sudo rm -rf /"
    # 历史残前缀两平台都判死
    legacy = WhitelistRule(tool="run_command", kind="prefix", pattern="FOO=1 sudo")
    assert not legacy.matches("run_command", "FOO=1 sudo rm -rf /")
    assert not legacy.matches("run_command", "FOO=1 sudo apt install htop")
    # 新沉淀的三词前缀两平台都照常命中自己的形状
    assert install.matches("run_command", "FOO=1 sudo apt install htop")


@pytest.mark.parametrize("is_windows", [False, True])
def test_windows_shape_sudo_exe_path(monkeypatch, is_windows: bool) -> None:
    """Windows 惯用形状：路径前缀 + .exe 后缀的 sudo 经 _norm_exe_name
    归一后同样锚定提权包装器（两平台桩都跑，判定不依赖运行平台）。"""
    from skysheep.security import gate as gate_mod

    monkeypatch.setattr(gate_mod, "_IS_WINDOWS", is_windows)
    rule = _rule_for(r"C:\Windows\System32\sudo.exe rm -rf /")
    assert rule.kind == "exact", rule
    assert _is_arbitrary_exec_prefix(r"C:\Windows\System32\sudo.exe")
    assert not _is_arbitrary_exec_prefix(
        r"C:\Windows\System32\sudo.exe apt install")


async def test_gate_does_not_auto_allow_assignment_prefixed_sudo() -> None:
    """F6.1 端到端：批准 `FOO=1 sudo apt install htop`（三词前缀）后，
    `FOO=1 sudo rm -rf /` 仍需确认；历史 "FOO=1 sudo" 残前缀规则同样
    放行不了它。"""
    gate = PermissionGate()
    cmd = RunCommandTool()
    rule = _rule_for("FOO=1 sudo apt install htop")
    assert rule.kind == "prefix" and rule.pattern == "FOO=1 sudo apt install"
    gate.add_session_rule(rule)
    evil = "FOO=1 sudo rm -rf /"
    assert await gate.authorize(cmd, {"command": evil}) is not None

    legacy = PermissionGate()
    legacy.add_session_rule(
        WhitelistRule(tool="run_command", kind="prefix", pattern="FOO=1 sudo"))
    assert await legacy.authorize(cmd, {"command": evil}) is not None


# ---- 门级端到端：批准无害形态后，危险变体必须仍走确认 ----


async def test_gate_does_not_auto_allow_assignment_prefixed_docker() -> None:
    """报告复现链（提炼面）：批准 `TAG=latest docker build .`（exact 规则）
    后，同前缀的任意 `docker run` 仍需确认；历史宽前缀规则同样放行不了。"""
    gate = PermissionGate()
    cmd = RunCommandTool()
    rule = _rule_for("TAG=latest docker build .")
    assert rule.kind == "exact"
    gate.add_session_rule(rule)
    evil = "TAG=latest docker run -v /:/host -v ~/.ssh:/k evil"
    assert await gate.authorize(cmd, {"command": evil}) is not None

    legacy = PermissionGate()
    legacy.add_session_rule(
        WhitelistRule(tool="run_command", kind="prefix", pattern="TAG=latest docker"))
    assert await legacy.authorize(cmd, {"command": evil}) is not None


async def test_gate_blocks_legacy_assignment_prefix_interpreter_repro() -> None:
    """报告复现链（匹配面）：历史 "A=1 B=2" 前缀规则放行不了其后被 8 个
    赋值挤出窗口的 `python -c`（旧实现免确认）。"""
    gate = PermissionGate()
    cmd = RunCommandTool()
    gate.add_session_rule(
        WhitelistRule(tool="run_command", kind="prefix", pattern="A=1 B=2"))
    evil = 'A=1 B=2 C=3 D=4 E=5 F=6 G=7 H=8 python -c "import os;os.system(\'calc\')"'
    assert await gate.authorize(cmd, {"command": evil}) is not None
