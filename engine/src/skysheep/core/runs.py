"""运行中心：全产品所有「Agent 在跑的事」的统一登记与状态视图。

一期范围（数据面先行）：
- 统一状态枚举：queued / thinking / tool_running / waiting_permission /
  retrying / cancelled / failed / completed / partial / interrupted
- 主对话轮（ServerBackend 的 run_task）与后台子代理任务都登记为 Run
- 全局列表 runs.list、单条 runs.get、取消 runs.cancel、失败重试重跑轮的
  信息（run.retryable 带原 session 与文本）
- 启动时崩溃恢复：上次退出时在跑的 Run 记为 interrupted，界面上可见、
  可一键重发（runs.retry 只做「把原文再发一次」，不做自动恢复）

持久化：~/.skysheep/runs.json（原子写），只存终态近 200 条 + 在跑的少量
在途项；崩溃恢复从「上次退出时仍是 running 态」的记录推断。

不引入新表：会话轮的细节（消息、用量）在 session store 里，运行中心只存
「哪件事、什么状态、卡在哪、怎么恢复」的最小字段。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..textio import write_text_atomic

# 状态机。合法迁移不做硬校验（登记方都是引擎自身代码，乱序说明有 bug，
# 记日志即可）；前端按 level 分组展示。
QUEUED = "queued"                  # 排队中（消息排队 / 后台并发满员排队）
THINKING = "thinking"              # 模型生成中（含流式）
TOOL_RUNNING = "tool_running"      # 工具执行中
WAITING_PERMISSION = "waiting_permission"  # 等待用户权限确认
RETRYING = "retrying"              # 瞬态错误自动重试中
CANCELLED = "cancelled"            # 用户取消
FAILED = "failed"                  # 失败（可重试时 retryable=True）
COMPLETED = "completed"            # 正常完成
PARTIAL = "partial"                # 部分完成（多段任务有失败段）
INTERRUPTED = "interrupted"        # 应用崩溃/重启导致的中断（可一键重发）

RUNNING_STATES = {QUEUED, THINKING, TOOL_RUNNING, WAITING_PERMISSION, RETRYING}
TERMINAL_STATES = {CANCELLED, FAILED, COMPLETED, PARTIAL, INTERRUPTED}

# 状态的中文标签与展示组（前端直接用，不各自翻译）
STATE_LABELS = {
    QUEUED: "排队中",
    THINKING: "正在思考",
    TOOL_RUNNING: "正在执行工具",
    WAITING_PERMISSION: "等待确认",
    RETRYING: "重试中",
    CANCELLED: "已取消",
    FAILED: "失败",
    COMPLETED: "已完成",
    PARTIAL: "部分完成",
    INTERRUPTED: "已中断（应用重启）",
}

KEEP_TERMINAL = 200  # 终态记录保留条数（超出裁剪最旧的）

# 允许一键重发/重跑的状态：只有「真的没跑完」的运行才该被恢复。
# completed / partial 不在此列——旧实现只看 retryable 布尔与原文，
# 已成功的运行也能被重发接口接受，写入类任务会重复产生副作用。
RETRYABLE_STATES = frozenset({FAILED, INTERRUPTED})


@dataclass
class Run:
    """一次可观测的 Agent 运行：主轮或后台子代理任务。"""

    id: str
    kind: str  # turn | subagent
    session_id: str
    title: str = ""
    state: str = QUEUED
    detail: str = ""            # 当前动作的可读描述（「正在执行 write_file」等）
    error: str = ""             # 失败/中断时的错误摘要
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    # 恢复所需的最小信息：原文与模式（team/roundtable/adversarial 一期不进重发，
    # 只重发纯文本）；subagent 运行额外记 agent_type / isolated，供失败/中断后
    # 一键重跑（重新派生同型任务）使用。
    retry_text: str = ""
    retryable: bool = False
    agent_type: str = ""  # 子代理型（task/explore/...）；主轮为空
    isolated: bool = False  # 隔离 worktree 派生；主轮为 False
    tools_used: int = 0
    retries: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["state_label"] = STATE_LABELS.get(self.state, self.state)
        d["running"] = self.state in RUNNING_STATES
        return d


class RunCenter:
    """进程内运行中心 + 磁盘快照（崩溃恢复用）。线程模型：引擎事件循环内使用。"""

    def __init__(self, path: Path | None = None) -> None:
        self._runs: dict[str, Run] = {}
        self._order: list[str] = []  # 创建序，旧→新
        self._id_seq = 0  # 同一毫秒内的并发自增（见 allocate_id）
        self.path = path
        if path is not None:
            self._load()

    def allocate_id(self, kind: str) -> str:
        """跨重启唯一的运行 id。

        旧实现用进程内自增 `turn-<n>`：每次启动都从 `turn-1` 重来，重启后
        新运行会把磁盘上同名旧记录覆盖掉（历史丢失、列表出现重复 id）。
        这里带上毫秒时间戳，进程重启、快照丢失都不会撞上旧 id。
        """
        self._id_seq += 1
        return f"{kind}-{int(time.time() * 1000)}-{self._id_seq}"

    # ---- 持久化 ----

    def _load(self) -> None:
        """启动装载：上次退出时仍在 RUNNING 态的记录改判 interrupted。"""
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            runs = raw.get("runs") or []
        except Exception:  # noqa: BLE001 - 坏快照不阻塞启动
            return
        now = time.time()
        for item in runs:
            try:
                r = Run(
                    id=str(item.get("id", "")),
                    kind=str(item.get("kind", "turn")),
                    session_id=str(item.get("session_id", "")),
                    title=str(item.get("title", "")),
                    state=str(item.get("state", COMPLETED)),
                    detail=str(item.get("detail", "")),
                    error=str(item.get("error", "")),
                    created_at=float(item.get("created_at", now)),
                    updated_at=float(item.get("updated_at", now)),
                    finished_at=item.get("finished_at"),
                    retry_text=str(item.get("retry_text", "")),
                    retryable=bool(item.get("retryable", False)),
                    agent_type=str(item.get("agent_type", "")),
                    isolated=bool(item.get("isolated", False)),
                    tools_used=int(item.get("tools_used", 0)),
                    retries=int(item.get("retries", 0)),
                )
            except Exception:  # noqa: BLE001
                continue
            if not r.id or r.id in self._runs:
                continue  # 快照里本不该有重复 id；有也只认第一条，避免列表重复
            if r.state in RUNNING_STATES:
                # 上次没退出干净：标记为「应用重启导致的中断」，可一键重发
                r.state = INTERRUPTED
                r.detail = "应用退出时该任务仍在运行"
                r.retryable = bool(r.retry_text)
                r.updated_at = now
            self._runs[r.id] = r
            self._order.append(r.id)

    def _trim_memory(self) -> None:
        """内存裁剪：终态超过 KEEP_TERMINAL 时从最旧开始丢（在跑的永不丢）。

        与 save 的落盘裁剪同一配额；不裁的话长会话进程内存缓慢上涨，
        list_runs 也永远回全量历史。"""
        terminal_ids = [rid for rid in self._order
                        if rid in self._runs and self._runs[rid].state in TERMINAL_STATES]
        overflow = len(terminal_ids) - KEEP_TERMINAL
        for rid in terminal_ids[:max(0, overflow)]:
            self._runs.pop(rid, None)
            try:
                self._order.remove(rid)
            except ValueError:
                pass

    def save(self) -> None:
        if self.path is None:
            return
        # 只落终态 + 在跑的（在跑的下次启动会被改判 interrupted）
        self._trim_memory()
        runs = [self._runs[rid] for rid in self._order if rid in self._runs]
        terminal = [r for r in runs if r.state in TERMINAL_STATES][-KEEP_TERMINAL:]
        active = [r for r in runs if r.state in RUNNING_STATES]
        payload = {"version": 1, "saved_at": time.time(),
                   "runs": [asdict(r) for r in (terminal + active)]}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self.path, json.dumps(payload, ensure_ascii=False))
        except Exception:  # noqa: BLE001 - 快照失败不影响运行
            pass

    # ---- 登记 ----

    def register(self, run_id: str, kind: str, session_id: str, title: str = "",
                 state: str = QUEUED, retry_text: str = "",
                 agent_type: str = "", isolated: bool = False) -> Run:
        run = Run(
            id=run_id, kind=kind, session_id=session_id, title=title,
            state=state, retry_text=retry_text,
            retryable=bool(retry_text),
            agent_type=agent_type, isolated=isolated,
        )
        # 同 id 重复登记：保留原创建位置，只替换内容并清掉 _order 里的重复项。
        # 旧实现直接覆盖字典又追加 _order，列表里会出现两个相同 id（一个是
        # 幽灵条目），裁剪与展示都会错位。
        self._runs[run_id] = run
        if self._order.count(run_id) > 1:
            while self._order.count(run_id) > 1:
                self._order.remove(run_id)
        if run_id not in self._order:
            self._order.append(run_id)
        self.save()
        return run

    def get(self, run_id: str) -> Run | None:
        return self._runs.get(run_id)

    def update(
        self, run_id: str, state: str | None = None, detail: str | None = None,
        error: str | None = None, tools_used: int | None = None,
        retries: int | None = None,
    ) -> Run | None:
        run = self._runs.get(run_id)
        if run is None:
            return None
        if state is not None:
            run.state = state
        if detail is not None:
            run.detail = detail[:300]
        if error is not None:
            run.error = error[:500]
        if tools_used is not None:
            run.tools_used = max(run.tools_used, tools_used)
        if retries is not None:
            run.retries = retries
        run.updated_at = time.time()
        if run.state in TERMINAL_STATES:
            run.finished_at = run.updated_at
        self.save()
        return run

    def cancel(self, run_id: str) -> bool:
        """用户取消：标记终态。真正的取消动作（停 task/停轮）由调用方执行。"""
        run = self._runs.get(run_id)
        if run is None or run.state not in RUNNING_STATES:
            return False
        run.state = CANCELLED
        run.updated_at = time.time()
        run.finished_at = run.updated_at
        self.save()
        return True

    # ---- 查询 ----

    def list_runs(
        self, *, session_id: str | None = None, include_terminal: bool = True,
        limit: int = 100,
    ) -> list[dict]:
        out: list[Run] = []
        for rid in reversed(self._order):  # 新→旧
            r = self._runs.get(rid)
            if r is None:
                continue
            if session_id and r.session_id != session_id:
                continue
            if not include_terminal and r.state in TERMINAL_STATES:
                continue
            out.append(r)
            if len(out) >= limit:
                break
        return [r.to_dict() for r in out]

    def running_runs(self) -> list[Run]:
        return [self._runs[rid] for rid in self._order
                if rid in self._runs and self._runs[rid].state in RUNNING_STATES]

    def interrupted_runs(self) -> list[Run]:
        return [self._runs[rid] for rid in self._order
                if rid in self._runs and self._runs[rid].state == INTERRUPTED]

    def retryable_run(self, run_id: str) -> Run | None:
        """可重发/重跑的运行：必须是失败或中断，且带原文。

        状态约束由后端强制，不能只靠前端隐藏按钮——否则已完成的运行也能被
        runs.retry 接受并重放，写入类任务会重复产生副作用。
        """
        r = self._runs.get(run_id)
        if r is None or not r.retry_text or not r.retryable:
            return None
        if r.state not in RETRYABLE_STATES:
            return None
        return r
