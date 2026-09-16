"""Hooks：工具调用前后的用户钩子（对标 Claude Code hooks）。

config.toml 配置（可选）：

    [[hooks.pre_tool_use]]
    match = "write_file"        # 工具名 glob，缺省 "*" 匹配全部
    command = "python check.py" # 收到 stdin 的 JSON：{tool, input, working_dir}
    timeout_s = 10

    [[hooks.post_tool_use]]
    match = "run_command"
    command = "notify-done.exe"

约定（与 Claude Code 对齐）：
- pre：退出码 0 放行；退出码 2 阻止该工具调用（stderr 作为原因）；
  stdout 若是 {"decision":"block","reason":"..."} 也阻止；
  其余非零退出码视为钩子自身故障，不阻断（放行）。
- post：仅通知，输出/失败都只产生 Notice，不影响工具结果。

钩子命令来自用户自己的 config.toml，等价于用户手敲命令，不经过权限门；
但钩子拿到的 input JSON 是模型给的参数，pre 钩子是「写盘前最后一道人工闸门」。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_TIMEOUT_S = 10.0


class HookRule:
    def __init__(self, *, match: str = "*", command: str = "", timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.match = match or "*"
        self.command = command
        self.timeout_s = max(1.0, float(timeout_s or DEFAULT_TIMEOUT_S))

    def matches(self, tool_name: str) -> bool:
        return fnmatch(tool_name, self.match)


class HookRunner:
    """按规则执行 pre/post 钩子；无规则时所有调用都是零开销直通。"""

    def __init__(
        self,
        pre_rules: list[HookRule] | None = None,
        post_rules: list[HookRule] | None = None,
        working_dir: Path | None = None,
    ) -> None:
        self.pre_rules = pre_rules or []
        self.post_rules = post_rules or []
        self.working_dir = working_dir

    @property
    def has_pre(self) -> bool:
        return bool(self.pre_rules)

    async def run_pre(self, tool_name: str, input_dict: dict) -> str:
        """执行 pre 钩子。返回空串 = 放行；非空 = 阻止原因（工具调用失败）。"""
        reasons: list[str] = []
        for rule in self.pre_rules:
            if not rule.matches(tool_name):
                continue
            code, stdout, stderr = await self._run(rule, tool_name, input_dict)
            if code == 2:
                reasons.append((stderr or stdout or "blocked by hook").strip()[:300])
                continue
            if code == 0 and stdout.strip().startswith("{"):
                try:
                    data = json.loads(stdout)
                    if data.get("decision") == "block":
                        reasons.append(str(data.get("reason") or "blocked by hook")[:300])
                except ValueError:
                    pass
            # 其余情况（非零但非 2 / 解析失败）：钩子自身故障，不阻断
        return "; ".join(reasons)

    async def run_post(self, tool_name: str, input_dict: dict) -> str:
        """执行 post 钩子。返回空串 = 无话可说；非空 = 一条提示（Notice）。"""
        notes: list[str] = []
        for rule in self.post_rules:
            if not rule.matches(tool_name):
                continue
            code, stdout, stderr = await self._run(rule, tool_name, input_dict)
            text = (stderr if code else stdout or stderr).strip()
            if text:
                notes.append(f"[hook:{rule.match}] {text[:200]}")
        return "; ".join(notes)

    async def _run(self, rule: HookRule, tool_name: str, input_dict: dict) -> tuple[int, str, str]:
        env = dict(os.environ)
        env["SKYSHEEP_TOOL"] = tool_name
        env["SKYSHEEP_PROJECT"] = str(self.working_dir or "")
        stdin_data = json.dumps(
            {"tool": tool_name, "input": input_dict, "working_dir": str(self.working_dir or "")},
            ensure_ascii=False,
        )
        if sys.platform == "win32":
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            # 关键：用字符串形式传命令行（不走 list2cmdline）。cmd /s 会剥掉
            # 最外层引号、内层引号原样保留；若用 argv 列表，list2cmdline 会把
            # 命令里的引号转义成 \"，cmd 不认这种转义，带引号的命令直接坏掉。
            cmdline = 'cmd.exe /d /s /c "' + rule.command + '"'
            argv = None
        else:
            flags = 0
            argv = ["/bin/bash", "-c", rule.command]
            cmdline = None

        def _blocking() -> tuple[int, str, str]:
            try:
                proc = subprocess.run(  # noqa: S603 - 命令来自用户自己的配置文件
                    argv if argv is not None else cmdline,
                    input=stdin_data, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=rule.timeout_s,
                    cwd=str(self.working_dir) if self.working_dir else None,
                    env=env, creationflags=flags,
                )
                return proc.returncode, proc.stdout or "", proc.stderr or ""
            except subprocess.TimeoutExpired:
                return 124, "", f"hook timed out after {rule.timeout_s:.0f}s"
            except OSError as e:
                return 127, "", f"hook failed to start: {e}"

        return await asyncio.to_thread(_blocking)


def hooks_from_config(raw: dict) -> tuple[list[HookRule], list[HookRule]]:
    """从 config.toml 的原始 [hooks] 表解析 pre/post 规则；坏条目跳过不炸。"""
    hooks = raw.get("hooks") if isinstance(raw.get("hooks"), dict) else {}

    def _parse(key: str) -> list[HookRule]:
        rules: list[HookRule] = []
        for item in hooks.get(key) or []:
            if not isinstance(item, dict):
                continue
            command = str(item.get("command", "")).strip()
            if not command:
                continue
            try:
                rules.append(HookRule(
                    match=str(item.get("match", "*")) or "*",
                    command=command,
                    timeout_s=float(item.get("timeout_s") or DEFAULT_TIMEOUT_S),
                ))
            except (TypeError, ValueError):
                continue
        return rules

    return _parse("pre_tool_use"), _parse("post_tool_use")


def load_raw_config() -> dict:
    """读取 config.toml 原始表（hooks 解析用；拿不到就当空）。"""
    from ..config import config_path

    try:
        import tomllib

        with open(config_path(), "rb") as f:
            return tomllib.load(f)
    except Exception:  # noqa: BLE001 - 无文件/坏文件：钩子直接为空
        return {}
