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
    ("FOO=bar git status", "FOO=bar git"),
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
    "TAG=latest git",
    "env git",
    "python script.py",
    "action=read",  # 动作语义文本：不算任意执行面（动作级前缀是既有口径）
    "",  # 空 pattern 本就匹配不了任何命令（backend 的 stale 判定不标它）
])
def test_benign_prefix_not_marked_arbitrary(pattern: str) -> None:
    assert not _is_arbitrary_exec_prefix(pattern), pattern


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
