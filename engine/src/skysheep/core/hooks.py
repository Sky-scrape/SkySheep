"""Hooks：工具调用前后的用户钩子（对标 Claude Code hooks）。

config.toml 配置（可选）：

    [[hooks.pre_tool_use]]
    match = "write_file"        # 工具名 glob，缺省 "*" 匹配全部
    command = "python check.py" # 收到 stdin 的 JSON：{tool, input, working_dir, session_id}
    timeout_s = 10
    enabled = true              # 缺省 true；false = 暂停这条钩子（不删配置）

    [[hooks.post_tool_use]]
    match = "run_command"
    command = "notify-done.exe"

    [[hooks.stop]]              # 任务完成钩子：一轮以最终回答结束（不再调工具）时触发
    command = "notify-done.exe"

约定（与 Claude Code 对齐）：
- pre：退出码 0 放行；退出码 2 阻止该工具调用（stderr 作为原因）；
  stdout 若是 {"decision":"block","reason":"..."} 也阻止；
  其余非零退出码视为钩子自身故障，不阻断（放行）——但会记进最近执行记录，
  设置页可见，不会「配了钩子却像没配一样」静默失效。
- post / stop：仅通知，输出/失败都只产生 Notice，不影响工具结果。

钩子命令来自用户自己的 config.toml，等价于用户手敲命令，不经过权限门；
但钩子拿到的 input JSON 是模型给的参数，pre 钩子是「写盘前最后一道人工闸门」。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from collections import deque
from fnmatch import fnmatch
from pathlib import Path

DEFAULT_TIMEOUT_S = 10.0

# 最近执行记录（设置页「最近执行」面板）：进程内环形缓冲，不落盘——
# 钩子执行情况是诊断信息，重启后清空可接受。
_RECENT_RUNS: deque[dict] = deque(maxlen=24)

# 输出摘要截断：记录里放不下整段 stdout/stderr
_SUMMARY_CHARS = 200


def recent_hook_runs() -> list[dict]:
    """最近执行的钩子记录（旧→新）。"""
    return list(_RECENT_RUNS)


class HookRule:
    def __init__(
        self, *, match: str = "*", command: str = "",
        timeout_s: float = DEFAULT_TIMEOUT_S, enabled: bool = True,
    ) -> None:
        self.match = match or "*"
        self.command = command
        self.timeout_s = max(1.0, float(timeout_s or DEFAULT_TIMEOUT_S))
        self.enabled = bool(enabled)

    def matches(self, tool_name: str) -> bool:
        return fnmatch(tool_name, self.match)


class HookRunner:
    """按规则执行 pre/post/stop 钩子；无规则时所有调用都是零开销直通。"""

    def __init__(
        self,
        pre_rules: list[HookRule] | None = None,
        post_rules: list[HookRule] | None = None,
        working_dir: Path | None = None,
        stop_rules: list[HookRule] | None = None,
    ) -> None:
        self.pre_rules = pre_rules or []
        self.post_rules = post_rules or []
        self.stop_rules = stop_rules or []
        self.working_dir = working_dir

    @property
    def has_pre(self) -> bool:
        # 只数启用中的规则：全部停用时不应再阻止只读工具并发（语义同「没配钩子」）
        return any(r.enabled for r in self.pre_rules)

    @property
    def has_stop(self) -> bool:
        return any(r.enabled for r in self.stop_rules)

    async def run_pre(self, tool_name: str, input_dict: dict, session_id: str = "") -> str:
        """执行 pre 钩子。返回空串 = 放行；非空 = 阻止原因（工具调用失败）。"""
        reasons: list[str] = []
        for rule in self.pre_rules:
            if not rule.enabled or not rule.matches(tool_name):
                continue
            code, stdout, stderr, dur = await self._exec(rule, "pre", tool_name, input_dict, session_id)
            if code == 2:
                reason = (stderr or stdout or "blocked by hook").strip()
                reasons.append(reason[:300])
                self._record("pre", rule, tool_name, code, dur, reason, status="blocked")
                continue
            blocked = False
            block_reason = ""
            if code == 0 and stdout.strip().startswith("{"):
                try:
                    data = json.loads(stdout)
                    if data.get("decision") == "block":
                        block_reason = str(data.get("reason") or "blocked by hook")
                        reasons.append(block_reason[:300])
                        blocked = True
                except ValueError:
                    pass
            # 其余情况（非零但非 2 / 解析失败）：钩子自身故障，不阻断
            out = (block_reason or (stderr if code else stdout or stderr)).strip()
            self._record(
                "pre", rule, tool_name, code, dur, out,
                status="blocked" if blocked else ("ok" if code == 0 else "error"),
            )
        return "; ".join(reasons)

    async def run_post(self, tool_name: str, input_dict: dict, session_id: str = "") -> str:
        """执行 post 钩子。返回空串 = 无话可说；非空 = 一条提示（Notice）。"""
        notes: list[str] = []
        for rule in self.post_rules:
            if not rule.enabled or not rule.matches(tool_name):
                continue
            code, stdout, stderr, dur = await self._exec(rule, "post", tool_name, input_dict, session_id)
            text = (stderr if code else stdout or stderr).strip()
            if text:
                notes.append(f"[hook:{rule.match}] {text[:200]}")
            self._record("post", rule, tool_name, code, dur, text,
                         status="ok" if code == 0 else "error")
        return "; ".join(notes)

    async def run_stop(self, session_id: str = "") -> str:
        """任务完成钩子（一轮以最终回答结束）。返回空串 = 无话可说；非空 = Notice。"""
        notes: list[str] = []
        for rule in self.stop_rules:
            if not rule.enabled:
                continue
            code, stdout, stderr, dur = await self._exec(rule, "stop", "", {}, session_id)
            text = (stderr if code else stdout or stderr).strip()
            if text:
                notes.append(f"[hook:stop] {text[:200]}")
            self._record("stop", rule, "", code, dur, text,
                         status="ok" if code == 0 else "error")
        return "; ".join(notes)

    async def test_run(
        self, kind: str, rule: HookRule, tool_name: str = "", input_dict: dict | None = None,
    ) -> dict:
        """设置页「钩子测试器」：拿示例参数实跑一遍，回完整结果（不记执行记录）。"""
        code, stdout, stderr, dur = await self._exec(
            rule, kind, tool_name, input_dict or {}, "", record=False,
        )
        blocked = False
        if kind == "pre":
            if code == 2:
                blocked = True
            elif code == 0 and stdout.strip().startswith("{"):
                try:
                    blocked = json.loads(stdout).get("decision") == "block"
                except ValueError:
                    pass
        return {
            "code": code,
            "stdout": stdout[:2000],
            "stderr": stderr[:2000],
            "duration_ms": dur,
            "blocked": blocked,
        }

    async def _exec(
        self, rule: HookRule, kind: str, tool_name: str,
        input_dict: dict, session_id: str, record: bool = True,
    ) -> tuple[int, str, str, int]:
        """跑一条钩子命令，返回 (退出码, stdout, stderr, 耗时ms)。"""
        env = dict(os.environ)
        env["SKYSHEEP_TOOL"] = tool_name
        env["SKYSHEEP_EVENT"] = kind
        env["SKYSHEEP_PROJECT"] = str(self.working_dir or "")
        payload: dict = {"working_dir": str(self.working_dir or ""), "session_id": session_id}
        if kind == "stop":
            payload["event"] = "stop"
        else:
            payload.update({"tool": tool_name, "input": input_dict})
        stdin_data = json.dumps(payload, ensure_ascii=False)
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

        t0 = time.monotonic()
        code, stdout, stderr = await asyncio.to_thread(_blocking)
        dur = int((time.monotonic() - t0) * 1000)
        if record:
            self._record(kind, rule, tool_name, code, dur,
                         (stderr if code else stdout or stderr).strip())
        return code, stdout, stderr, dur

    def _record(
        self, kind: str, rule: HookRule, tool_name: str, code: int,
        duration_ms: int, output: str = "", status: str = "",
    ) -> None:
        if not status:
            status = "ok" if code == 0 else "error"
        _RECENT_RUNS.append({
            "time": time.time(),
            "kind": kind,
            "match": rule.match,
            "tool": tool_name,
            "code": code,
            "status": status,
            "duration_ms": duration_ms,
            "output": output[:_SUMMARY_CHARS],
        })


def hooks_from_config(raw: dict) -> tuple[list[HookRule], list[HookRule], list[HookRule]]:
    """从 config.toml 的原始 [hooks] 表解析 pre/post/stop 规则；坏条目跳过不炸。"""
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
                    enabled=bool(item.get("enabled", True)),
                ))
            except (TypeError, ValueError):
                continue

        return rules

    return _parse("pre_tool_use"), _parse("post_tool_use"), _parse("stop")


def load_raw_config() -> dict:
    """读取 config.toml 原始表（hooks 解析用；拿不到就当空）。"""
    from ..config import config_path

    try:
        import tomllib

        with open(config_path(), "rb") as f:
            return tomllib.load(f)
    except Exception:  # noqa: BLE001 - 无文件/坏文件：钩子直接为空
        return {}
