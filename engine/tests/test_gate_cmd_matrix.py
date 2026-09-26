# 敌意命令矩阵（2026-09-25 审查专项 1 的回归固化）。
#
# 白名单的安全性依赖一个不变量：凡是 cmd.exe /d /s /c 实际会执行出「多段命令」的
# 命令串，_has_shell_chain 必须判 True（prefix/glob 规则随之失效、回退逐次确认）。
# 反方向（gate 拦了但 cmd 只跑一段）是刻意的保守误报（^、;、括号、单引号），
# 只损失便利性不损失安全性，矩阵里同样锁住当前行为，防止有人「优化」成漏判。
#
# 矩阵来源：实证脚本 审计-2026-09-25/cmd_matrix_probe.py 在真实 cmd.exe 上逐条
# 验证过「第二段是否真的执行」（第二段用 ver，其版本号输出无法被前一段的字面
# 回显伪造）。/s 的「剥首尾引号」老式行为被 list2cmdline 的内部引号转义（\"）
# 化解：残留引号仍保住配对，与 gate 的朴素引号翻转一致——这是本矩阵最重要的
# 一个负例，改动 _shell_argv 的包装方式或 _has_shell_chain 的引号逻辑时必须重跑。
import subprocess
import sys
import tempfile

import pytest

from skysheep.security.gate import (
    _has_code_exec_flag,
    _has_shell_chain,
    _prefix_match,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe 语义矩阵，仅 Windows")

# (命令, 既有前缀规则)。第二段统一用 ver（输出含 10.0.，回显伪造不出来）。
MATRIX = [
    # 基础分隔符：gate 拦截，cmd 确实执行多段 —— 核心防线
    ("echo A & ver", "echo A", True),
    ("echo A && ver", "echo A", True),
    ("echo A | ver", "echo A", True),
    ("echo A & ver & ver", "echo A", True),
    ("(echo A) & (ver)", "(echo A)", True),
    ("echo A; ver", "echo A", True),           # ; 在 cmd 里不是分隔符：保守误报（fail-closed 方向）
    # cmd 拼接面字符不在串里 / 被引号护住的形态：gate 判无拼接，cmd 也只跑一段 —— 一致
    ("(echo A) (ver)", "(echo A)", False),     # 括号分组实测不构成第二条命令
    ('echo "A & ver"', "echo", False),
    ('"echo A & ver" extra', '"echo A', False),
    ('"echo A" serve "junk & ver"', '"echo A" serve', False),
    ('"echo A" "x & ver"', '"echo A"', False),
    ('echo "A-OK & ver', "echo", False),       # 未闭合引号：cmd 按字面输出
    ('echo A-OK "%WRAP%"', "echo A-OK", True),  # %VAR% 展开面（值可带分隔符）
    # 脱字符 / for /f：gate 按原字符保守拦下
    ("echo A ^& ver", "echo A", True),         # cmd 实际单段：保守误报
    ('for /f %i in (\'ver\') do @echo FORSUB-%i', "for /f", True),   # %i 触发展开面
    ("echo A > out.txt", "echo A", True),      # 重定向可落盘，按拼接面拦
]

_VER_MARK = "10.0."


def _run_via_cmd(command: str, cwd: str) -> str:
    """与 tools/shell.py._shell_argv 完全一致的执行管线（子进程按 argv 重新引注）。"""
    proc = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command],
        cwd=cwd, capture_output=True, timeout=15,
    )
    return ((proc.stdout or b"") + (proc.stderr or b"")).decode("utf-8", errors="replace")


def test_matrix_cmd_execution_matches_gate() -> None:
    """不变量（单向）：cmd 实际执行多段 ⇒ gate 必判拼接。

    反方向不成立是刻意的保守误报（^、;、% 触发展开面等），不在此断言。
    """
    with tempfile.TemporaryDirectory() as cwd:
        for command, _rule, _gate_expected in MATRIX:
            out = _run_via_cmd(command, cwd)
            multi = _VER_MARK in out
            if multi:
                assert _has_shell_chain(command), (
                    f"cmd.exe 实际执行了多段，但 gate 判无拼接（白名单可绕过）：{command!r}\n"
                    f"输出：{out[:200]!r}"
                )


def test_matrix_gate_classifications() -> None:
    """锁定每条命令当前的 gate 判定：变严是取舍（要同步 _chain_hint 文案），变松必须先过安全评审。"""
    for command, _rule, gate_expected in MATRIX:
        assert _has_shell_chain(command) is gate_expected, f"{command!r} 的拼接判定变了"


def test_matrix_prefix_rules_never_allow_multisegment() -> None:
    """白名单放行口径：规则能放行的命令，cmd 实际必须只跑一段（即不存在绕过组合）。"""
    with tempfile.TemporaryDirectory() as cwd:
        for command, rule, _ in MATRIX:
            blocked = _has_shell_chain(command) or _has_code_exec_flag(command)
            rule_ok = (not blocked) and _prefix_match(command, rule)
            if rule_ok:
                out = _run_via_cmd(command, cwd)
                assert _VER_MARK not in out, (
                    f"白名单放行了实际多段执行的命令：{command!r}（规则 {rule!r}）\n输出：{out[:200]!r}"
                )


def test_expand_and_newline_always_chain() -> None:
    """展开/换行字符在任何上下文都算拼接面（含双引号内：cmd 双引号内 % 仍展开）。"""
    assert _has_shell_chain("echo %VAR%")
    assert _has_shell_chain('echo "%VAR%"')
    assert _has_shell_chain("echo a\necho b")
    assert _has_shell_chain("echo a\recho b")
    assert _has_shell_chain("echo `id`")
    assert _has_shell_chain("echo $(id)")


def test_code_exec_flags_block_prefix_and_glob() -> None:
    """解释器旗标：前缀/规则提炼必须退化 exact，任意代码不能借白名单进门。"""
    for cmd in ("python -c 'import os'", "py -3 -c \"1\"", "powershell -Command gci",
                "node --eval process.exit(1)", "bash -lc id"):
        assert _has_code_exec_flag(cmd), cmd
