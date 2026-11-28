"""命令前缀白名单与安全词法判定（从 security/gate.py 拆分，提炼/匹配共用同一源）。"""

from __future__ import annotations

import os
import re
import shlex
import sys

# 以「一条简单命令」为单位的前缀白名单只对 run_command 有意义；命令里出现 shell 元字符
# （见 _has_shell_chain）就意味着可能存在后续命令或参数替换，必须退回逐次确认。
_RUN_COMMAND = "run_command"

# 白名单规则的合法类型（设置页手动添加 / 导入时校验；与 WhitelistRule.matches 支持的 kind 对齐）。
RULE_KINDS = ("always", "prefix", "exact", "glob")

# glob 规则的大小写语义：Windows 文件系统大小写不敏感，规则也按不敏感匹配（与文件查找一致）；
# 其它平台严格区分。fnmatch.fnmatch 会隐式做这层归一（normcase），行为随平台悄悄变化，
# 这里显式判断，让规则语义可读、可预期。
_CASE_INSENSITIVE_FS = os.name == "nt"

# 命令实际交给哪个 shell 执行（见 tools/shell.py：Windows 是 cmd.exe，其它平台是 bash）。
# 两者的引号与展开语义不同，白名单的「是否有命令拼接」判定必须按平台取，见 _has_shell_chain。
_IS_WINDOWS = os.name == "nt"


def _is_windows_now() -> bool:
    """实时读取平台标志（兼容测试对 gate._IS_WINDOWS 或本模块 _IS_WINDOWS 的打桩）。"""
    gate_mod = sys.modules.get("skysheep.security.gate")
    if gate_mod is not None and "_IS_WINDOWS" in gate_mod.__dict__:
        return bool(gate_mod.__dict__["_IS_WINDOWS"])
    return bool(_IS_WINDOWS)


def _case_insensitive_fs_now() -> bool:
    gate_mod = sys.modules.get("skysheep.security.gate")
    if gate_mod is not None and "_CASE_INSENSITIVE_FS" in gate_mod.__dict__:
        return bool(gate_mod.__dict__["_CASE_INSENSITIVE_FS"])
    return bool(_CASE_INSENSITIVE_FS)


# 两平台都危险的展开/替换字符：POSIX 的变量替换（$VAR）、命令替换（$(...)）与反引号，
# 以及换行。Windows 的 cmd 不展开 $ 与反引号，但保守拦下没有代价（只是回到逐次确认），
# 而一旦漏判就是白名单被绕过。
_CHAIN_EXPAND_CHARS = "`$\r\n"

# 命令分隔/重定向字符。`;` 在 cmd 中并不是分隔符，但它在本表里两平台一并拦下——
# 纵深防御：命令字符串的解析方未必永远是我们这里指定的 shell，宁可多问一次。
_CHAIN_SEP_CHARS = ";|&<>"

# 「下一参数就是任意代码文本」的解释器/shell 旗标（小写）。规则提炼（rule_for）
# 与匹配（WhitelistRule._matches_core）共用：这类调用提炼不出有边界的前缀，
# "python -c" 前缀规则等于放行任意 Python 代码。
_CODE_EXEC_FLAGS = frozenset({
    "-c", "-e", "-p", "-r", "-lc", "-ic", "/c", "/k",
    "-command", "-encodedcommand", "--eval", "--command",
})

# 解释器调用**全文扫描**时额外识别的「下一参数即任意代码/任意脚本」旗标。
# 与 _CODE_EXEC_FLAGS 分开：这些词在普通工具里是常见参数（tar -f / curl -f），
# 只对解释器（_INTERPRETER_COMMANDS）的调用启用，避免把正常命令误伤成 exact。
_INTERPRETER_EXTRA_FLAGS = frozenset({
    "-m", "-file", "--file", "-f",
    # deno eval / bun eval：第二词就是子命令，后接任意 JS
    "eval",
    # node --require / --import / --import / --loader：脚本名之后仍可加载任意模块执行代码
    "--require", "--import", "--loader",
})

# 解释器/shell 可执行名（小写）：旗标全文扫描只对以它们开头的命令放开。
_INTERPRETER_COMMANDS = frozenset({
    "python", "python3", "py", "powershell", "pwsh",
    "node", "deno", "bun", "ruby", "perl",
    "bash", "sh", "zsh", "fish", "cmd",
})

# 「第二词之后仍是任意执行/任意文件面」的入口命令（小写）：两词前缀提炼不出
# 有边界的前缀，改成 exact 只放行用户当时批准的这一条。
_PREFIX_UNSAFE_COMMANDS = frozenset({
    "docker", "ssh", "scp",
    "wsl", "npx", "uvx", "bunx",
    "go", "make",
    "regsvr32", "certutil", "mshta", "rundll32",
    "sudo", "doas",
})


def _norm_exe_name(word: str) -> str:
    """可执行名归一：小写、去配对引号、去路径前缀、去 .exe 后缀（两平台写法都认）。"""
    exe = word.lower()
    if len(exe) >= 2 and exe[0] == exe[-1] and exe[0] in "\"'":
        exe = exe[1:-1]
    exe = exe.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if exe.endswith(".exe"):
        exe = exe[: -len(".exe")]
    return exe


def _tokenize(text: str) -> list[str]:
    r"""命令分词：优先 shlex（正确切分带引号/空格的路径，`"C:\Program Files\...\python.exe" -c`
    不会被空格拆碎），失败退回 str.split。Windows 用 posix=False 保留引号交由
    _norm_exe_name 配对剥离；POSIX 用 posix=True 由 shlex 去引号。"""
    try:
        return shlex.split(text, posix=(not _is_windows_now()))
    except ValueError:
        return text.split()


# 「后面紧跟真实命令」的包装器命令：解释器可被这些前缀挤出首词
_WRAPPER_COMMANDS = frozenset({
    "env", "nohup", "nice", "time", "sudo", "doas", "setsid", "stdbuf",
    "xargs", "command", "exec", "timeout", "ionice", "chrt",
})

# 解释器/真实命令词识别的总扫描词数上限
_INTERPRETER_SCAN_MAX_TOKENS = 64

# 提权包装器：前缀提炼取三词（`sudo apt install` / `sudo apt remove` 各成一类）
_SUDO_WRAPPERS = frozenset({"sudo", "doas"})

# 包装器命令的「下一参数是值」旗标（小写，含长短形态）
_WRAPPER_VALUE_OPTS: dict[str, frozenset[str]] = {
    "sudo": frozenset({
        "-u", "-g", "-h", "-p", "-c", "-t", "-r",
        "--user", "--group", "--host", "--prompt", "--close-from",
        "--role", "--type", "--other-user",
    }),
    "doas": frozenset({"-u", "-c"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "ionice": frozenset({"-c", "-n", "-p", "-u", "--class", "--classdata"}),
    "chrt": frozenset({"-p", "-t", "--pid", "--sched-runtime"}),
    "stdbuf": frozenset({"-i", "-o", "-e", "--input", "--output", "--error"}),
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "time": frozenset({"-f", "-o", "--format", "--output"}),
    "nohup": frozenset(),
    "setsid": frozenset(),
    "command": frozenset(),
    "exec": frozenset(),
    "env": frozenset({"-u", "--unset", "-c", "--chdir", "-s", "--split-string"}),
    "xargs": frozenset({"-n", "-i", "-l", "-p", "-s", "-a", "-d", "-e"}),
}


def _is_var_assignment(word: str) -> bool:
    """POSIX 前置赋值 `NAME=value`（NAME 是合法标识符）。"""
    name, sep, _ = word.partition("=")
    return bool(sep) and name.isidentifier()


def _is_wrapper_duration_token(wrapper: str, word: str) -> bool:
    """包装器的位置数值/时长参数（整数或 timeout 的 `5s`/`0.5m`/`1h`/`2d`）。"""
    if word.isdigit():
        return True
    if wrapper == "timeout":
        return bool(re.fullmatch(r"\d+(?:\.\d+)?[smhdSMHD]?", word))
    return False


def _first_real_command_word(words: list[str]) -> str | None:
    """跳过前置赋值与包装器（含其带值旗标的值、无值旗标、数值），取第一个
    真实命令词（经 _norm_exe_name 归一）；找不到返回 None。"""
    words = words[:_INTERPRETER_SCAN_MAX_TOKENS]
    wrapper: str | None = None
    i = 0
    while i < len(words):
        w = words[i]
        exe = _norm_exe_name(w)
        if _is_var_assignment(w):
            i += 1
            continue
        if exe in _WRAPPER_COMMANDS:
            wrapper = exe
            i += 1
            continue
        if wrapper is not None:
            if w.lower() in _WRAPPER_VALUE_OPTS.get(wrapper, frozenset()):
                i += 2
                continue
            if w.startswith("-") or _is_wrapper_duration_token(wrapper, w):
                i += 1
                continue
            return exe
        if w.isdigit() or w.startswith("-"):
            i += 1
            continue
        return exe
    return None


def _sudo_anchor_index(words: list[str]) -> int | None:
    """首个真实命令词是 sudo/doas 时返回它的下标；不是提权包装器返回 None。"""
    words = words[:_INTERPRETER_SCAN_MAX_TOKENS]
    wrapper: str | None = None
    i = 0
    while i < len(words):
        w = words[i]
        exe = _norm_exe_name(w)
        if _is_var_assignment(w):
            i += 1
            continue
        if exe in _SUDO_WRAPPERS:
            return i
        if exe in _WRAPPER_COMMANDS:
            wrapper = exe
            i += 1
            continue
        if wrapper is not None:
            if w.lower() in _WRAPPER_VALUE_OPTS.get(wrapper, frozenset()):
                i += 2
                continue
            if w.startswith("-") or _is_wrapper_duration_token(wrapper, w):
                i += 1
                continue
        elif w.isdigit() or w.startswith("-"):
            i += 1
            continue
        return None
    return None


def _interpreter_invocation(text: str) -> bool:
    """命令是否以解释器/shell 调用（带引号、路径前缀、.exe 后缀、前置赋值/包装器均可）。"""
    return _first_real_command_word(_tokenize(text)) in _INTERPRETER_COMMANDS


_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "fish"})


def _token_hits_code_exec(low: str, exe: str | None = None) -> bool:
    """判定单个旗标词是否命中任意代码执行形态（含 = 传值、连写短旗标与 PowerShell 缩写）。"""
    head = low.split("=", 1)[0].split(":", 1)[0]
    if head in _CODE_EXEC_FLAGS:
        return True
    if exe is None:
        return False
    if head in _INTERPRETER_EXTRA_FLAGS:
        return True
    if exe in _POSIX_SHELLS and low.startswith("-") and not low.startswith("--") and len(low) <= 6:
        return any(ch in ("c", "e") for ch in low[1:])
    if exe in ("python", "python3", "py"):
        return low.startswith(("-c", "-m"))
    if exe in ("node", "deno", "bun"):
        return low.startswith(("-e", "-p", "-r"))
    if exe in ("ruby", "perl"):
        return low.startswith(("-e",))
    if exe in ("powershell", "pwsh"):
        return (
            len(head) >= 3
            and ("-encodedcommand".startswith(head) or "-command".startswith(head))
        )
    return False


def _has_code_exec_flag(text: str, head: int = 4) -> bool:
    """命令里是否出现「下一参数就是任意代码文本」的解释器旗标。"""
    words = _tokenize(text)
    exe = _first_real_command_word(words)
    if exe in _INTERPRETER_COMMANDS:
        return any(_token_hits_code_exec(w.lower(), exe) for w in words)
    return any(_token_hits_code_exec(w.lower(), None) for w in words[:head])


def _has_shell_chain(text: str) -> bool:
    r"""命令里是否出现 shell 拼接/替换元字符（Windows 走 cmd.exe、其余走 bash）。"""
    single = False
    double = False
    is_win = _is_windows_now()
    escape = "^" if is_win else "\\"
    pct_active = is_win and text.count("%") >= 2
    for ch in text:
        if ch == '"' and not single:
            double = not double
            continue
        if ch == "'" and not double:
            if not is_win:
                single = not single
            continue
        if single:
            continue
        if ch in _CHAIN_EXPAND_CHARS or (pct_active and ch == "%") or ch == escape:
            return True
        if double:
            continue
        if ch in _CHAIN_SEP_CHARS:
            return True
    return False


def _chain_hint() -> str:
    """确认弹窗/测试器里解释「为什么这条命令没命中前缀规则」时用的字符集说明。"""
    base = "; | & < > ` $ 或换行"
    if _is_windows_now():
        return base + "，以及单引号 '、%VAR% 变量展开、^ 转义"
    return base + "，以及反斜杠 \\ 转义"


def _has_extra_flag_after_script(text: str) -> bool:
    """解释器调用是否在"脚本名"之后又带了额外 - 旗标。"""
    if not _interpreter_invocation(text):
        return False
    words = _tokenize(text)
    if len(words) < 3:
        return False
    return any(w.startswith("-") and not w.isdigit() for w in words[2:])


def _prefix_match(text: str, pattern: str) -> bool:
    """前缀命中，且前缀必须是**完整的词**。"""
    if not pattern or not text.startswith(pattern):
        return False
    rest = text[len(pattern) :]
    return rest[:1] in ("", " ", "\t")


_PREFIX_DELETE_HEADS = frozenset({
    "rm", "rmdir", "del", "erase", "rd", "unlink", "shred",
    "deltree", "sdelete", "srm", "wipe", "truncate",
    "taskkill", "find", "xargs", "forfiles", "unzip", "tar",
})
_PREFIX_EXEC_PAIRS = frozenset({
    ("git", "clean"), ("git", "rm"),
    ("npm", "run"), ("npm", "exec"), ("npm", "test"),
    ("npm", "start"), ("npm", "install"), ("npm", "ci"),
    ("yarn", "run"), ("yarn", "install"),
    ("pnpm", "run"), ("pnpm", "install"),
    ("bun", "run"), ("cargo", "run"), ("dotnet", "run"),
})


def _prefix_deletes_or_runs_arbitrary(pattern: str) -> bool:
    """前缀是否落在删除 / 任意子命令执行族（模板前缀通道专用）。"""
    words = [w for w in _tokenize(pattern) if w]
    if not words:
        return False
    real = _first_real_command_word(words)
    if real is None:
        return True
    idx = next(
        (i for i, w in enumerate(words) if _norm_exe_name(w) == real), None
    )
    if idx is None:
        return True
    rest = words[idx + 1 :]
    if real in _PREFIX_DELETE_HEADS:
        return True
    second = str(rest[0]).lower() if rest else ""
    return (real, second) in _PREFIX_EXEC_PAIRS


def _all_assignment_words(pattern: str) -> bool:
    """前缀是否为纯前置赋值序列（``action=read`` 这类动作语义文本，审查 P-9）。"""
    words = [w for w in pattern.split() if w]
    return bool(words) and all(_is_var_assignment(w) for w in words)


def _sudo_prefix_shape_unsafe(words: list[str]) -> bool:
    """锚在提权包装器（sudo/doas）上的词序列钉不住可信三词 → True（F6.1）。"""
    lead = _sudo_anchor_index(words)
    if lead is None:
        return False
    if lead + 2 >= len(words):
        return True
    if words[lead + 1].startswith("-") or _is_var_assignment(words[lead + 1]):
        return True
    if words[lead + 2].startswith("-") or _is_var_assignment(words[lead + 2]):
        return True
    return False


def _is_arbitrary_exec_words(words: list[str]) -> bool:
    """词序列是否落在任意执行面：首个真实命令词在入口清单
    （_PREFIX_UNSAFE_COMMANDS）内，或提权锚（sudo/doas）钉不住三词形状。"""
    real = _first_real_command_word(words)
    if real is not None:
        if real in _PREFIX_UNSAFE_COMMANDS:
            return True
        return _sudo_prefix_shape_unsafe(words)
    if any(_norm_exe_name(w) in _WRAPPER_COMMANDS for w in words):
        return True
    if _sudo_prefix_shape_unsafe(words):
        return True
    return len(words) > _INTERPRETER_SCAN_MAX_TOKENS


def _is_arbitrary_exec_prefix(pattern: str) -> bool:
    """前缀规则钉住的**首个真实命令词**是否属于「之后即任意执行面」的入口命令。"""
    return _is_arbitrary_exec_words(pattern.split())


def _hits_unsafe_entry_command(text: str) -> bool:
    """本次命令是否落在任意执行面（匹配侧，SEC-M01 / F6.1）。"""
    return _is_arbitrary_exec_words(_tokenize(text))
