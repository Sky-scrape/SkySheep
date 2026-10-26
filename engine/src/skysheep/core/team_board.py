"""团队工单模型与状态机。

TeamTask 与 TeamBoard 只负责工单数据和状态转换；团队编排、频道消息与
WS 适配留在 team.py。TeamError 由 team_store 提供，保证团队各域共享同一
异常类型。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .team_store import TeamError

_DEFAULT_REDO_LIMIT = 2


def _status_label(status: str) -> str:
    return {
        "pending": "待办", "in_progress": "进行中", "review": "待验收",
        "done": "完成", "error": "失败",
    }.get(status, status)


@dataclass
class TeamTask:
    """工单（叫「工单」不叫「任务」——「任务」是右面板容器词，设计 §5）。"""

    id: str
    title: str
    assignee: str
    type: str = "exec"  # exec（执行型，带工具）/ advisor（顾问型，只出意见）——工单级属性
    deps: list[str] = field(default_factory=list)
    accept: str = ""  # 验收标准
    status: str = "pending"  # pending / in_progress / review / done / error
    redo: int = 0  # 被打回次数
    created_at: float = field(default_factory=time.time)
    detail: str = ""
    source: dict = field(default_factory=dict)
    # 是否允许编排器自动派发：对抗发现转来的顾问工单要求用户显式派工，
    # 置 False 后只停在待办（自动循环的 _dispatch_ready 不会捡它）
    auto_dispatch: bool = True

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "assignee": self.assignee,
            "type": self.type, "deps": list(self.deps), "accept": self.accept,
            "status": self.status, "redo": self.redo, "created_at": self.created_at,
            "detail": self.detail, "source": dict(self.source),
            "auto_dispatch": self.auto_dispatch,
        }


class TeamBoard:
    """工单板与状态机。板由三方触达，成员仍不改板（频道消息不可执行）：

    - 用户：WS 方法（team.task_add / task_update，一期）；
    - AI 总管：内部团队工具（team_assign / accept / reject / reassign / drop，
      二期，全部只能落在状态机允许的边上）；
    - 自动循环：派发（待办→进行中）与「成员交报告→待验收」（二期）。

    状态机（设计 §5）：待办 ──派工──▶ 进行中 ──交报告──▶ 待验收
    ──验收通过──▶ 完成；待验收 ──打回(redo+1)──▶ 进行中。
    另有 待办/进行中/待验收 ──▶ 失败（放弃；待办的失败出口用于依赖链卡死
    工单的强制裁定「砍掉」）、失败 ──▶ 进行中（重新派工/改派）；完成是终态。
    """

    _TRANSITIONS: dict[str, set[str]] = {
        "pending": {"in_progress", "error"},
        "in_progress": {"review", "error"},
        "review": {"done", "in_progress", "error"},
        "error": {"in_progress"},
        "done": set(),
    }

    def __init__(
        self, redo_limit: int = _DEFAULT_REDO_LIMIT, members: list[str] | None = None,
    ) -> None:
        self._tasks: dict[str, TeamTask] = {}
        self._counter = 0
        self.redo_limit = max(0, int(redo_limit))
        self.members = list(members or [])  # 指派校验用（建队后由编排器填）

    # ---- 查询 ----

    def get(self, task_id: str) -> TeamTask | None:
        return self._tasks.get(task_id)

    def tasks(self) -> list[TeamTask]:
        return list(self._tasks.values())

    def by_assignee(self, member: str) -> list[TeamTask]:
        return [t for t in self._tasks.values() if t.assignee == member]

    def active_exec_tasks(self, member: str) -> list[TeamTask]:
        """名下进行中的执行型工单：成员本轮挂工具表（执行型）还是空表（顾问型）。"""
        return [
            t for t in self._tasks.values()
            if t.assignee == member and t.type == "exec" and t.status == "in_progress"
        ]

    def dispatchable(self) -> list[TeamTask]:
        """可自动派发的待办工单（依赖全部完成且未标记「只能手动派工」）。"""
        return [
            t for t in self._tasks.values()
            if t.status == "pending" and t.auto_dispatch and self.deps_satisfied(t)
        ]

    def deps_satisfied(self, task: TeamTask) -> bool:
        return all(
            (dep := self._tasks.get(task_id)) is not None and dep.status == "done"
            for task_id in task.deps
        )

    def incomplete(self) -> list[TeamTask]:
        return [t for t in self._tasks.values() if t.status != "done"]

    def all_done(self) -> bool:
        return bool(self._tasks) and not self.incomplete()

    # ---- 变更（全部只应由用户的 WS 方法触达）----

    def add(
        self, title: str, assignee: str, type: str = "exec",
        accept: str = "", deps: list[str] | None = None,
        detail: str = "", source: dict | None = None,
        auto_dispatch: bool = True,
    ) -> TeamTask:
        title = (title or "").strip()
        if not title:
            raise TeamError("工单标题不能为空")
        if assignee not in self.members:
            raise TeamError(f"指派对象「{assignee}」不在团队名册中")
        if type not in ("exec", "advisor"):
            raise TeamError(f"工单类型只能是 exec（执行型）或 advisor（顾问型），收到：{type}")
        dep_ids = [d for d in dict.fromkeys(deps or []) if d]
        for dep in dep_ids:
            if dep not in self._tasks:
                raise TeamError(f"依赖的工单 {dep} 不存在")
        self._counter += 1
        task = TeamTask(
            id=f"T{self._counter}", title=title, assignee=assignee,
            type=type, deps=dep_ids, accept=(accept or "").strip(),
            detail=(detail or "")[:8_000], source=dict(source or {}),
            auto_dispatch=bool(auto_dispatch),
        )
        self._tasks[task.id] = task
        return task

    def set_status(self, task_id: str, status: str) -> TeamTask:
        """状态流转。review → in_progress 视为打回（redo+1，受 redo_limit 约束）。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise TeamError(f"工单 {task_id} 不存在")
        if status == task.status:
            return task
        if status not in self._TRANSITIONS.get(task.status, set()):
            raise TeamError(
                f"工单 {task_id} 不能从「{_status_label(task.status)}」"
                f"流转到「{_status_label(status)}」"
            )
        if status == "in_progress":
            # 派工/打回/重派都要过依赖关：依赖未完成不派发（设计 §5）
            waiting = [
                dep for dep in task.deps
                if (t := self._tasks.get(dep)) is None or t.status != "done"
            ]
            if waiting:
                raise TeamError(
                    f"工单 {task_id} 的依赖工单未完成（{'、'.join(waiting)}），不能派发"
                )
            if task.status == "review":
                if task.redo >= self.redo_limit:
                    raise TeamError(
                        f"工单 {task_id} 已被打回 {task.redo} 次，达到上限（{self.redo_limit}）："
                        "请改派他人、换顾问型工单替代，或标记 error 放弃"
                    )
                task.redo += 1
        task.status = status
        return task

    def mark_error(self, task_id: str, reason: str = "") -> TeamTask:
        """失败收尾（放弃；用户的 WS 方法或 AI 总管的 team_drop 工具触达）。

        待办也可放弃：依赖链卡死的待办工单落「砍掉」裁定（设计 §9），否则
        强制裁定指令里的 team_drop 对这类工单无单可落。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TeamError(f"工单 {task_id} 不存在")
        if task.status not in ("pending", "in_progress", "review"):
            raise TeamError(f"工单 {task_id} 当前不在待办/进行中/待验收，不能标记失败")
        task.status = "error"
        if reason:
            task.accept = (task.accept + "\n" if task.accept else "") + f"失败原因：{reason}"
        return task

    def reassign(
        self, task_id: str, new_assignee: str, type: str | None = None, note: str = "",
    ) -> TeamTask:
        """改派（AI 总管的 team_reassign 工具触达；用户改派走 task_update + 重派）。

        只接受 待验收/失败 两种在板状态：失败是重派（error → 进行中的既有边），
        待验收是验收不合格的换人。待验收改派的 redo 口径：
        - 未超打回上限：视同打回（redo+1）；
        - 已超上限（强制裁定出口）：redo 清零——新队员拿全新的打回预算，
          「换人再换人」的滥用由全局轮次上限兜底（设计 §9 的兜底层）。
        new_assignee 必须不同于当前指派（改派他人，设计 §5）；想给原队员新的
        机会 = 放弃后用新工单重来。type 传入时同时改工单类型
        （降级为顾问型的强制裁定出口）。依赖必然已满足（派发时已过关，
        完成是终态不会回退），不再重复校验。
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise TeamError(f"工单 {task_id} 不存在")
        if task.status not in ("review", "error"):
            raise TeamError(f"工单 {task_id} 当前不在待验收/失败，不能改派")
        if new_assignee not in self.members:
            raise TeamError(f"改派对象「{new_assignee}」不在团队名册中")
        if new_assignee == task.assignee:
            raise TeamError(
                f"改派对象必须不同于当前指派队员「{task.assignee}」"
                "（给原队员新的机会：先放弃该工单，再建新工单指派给他）"
            )
        if type is not None and type not in ("exec", "advisor"):
            raise TeamError(f"工单类型只能是 exec（执行型）或 advisor（顾问型），收到：{type}")
        over_limit = task.status == "review" and task.redo >= self.redo_limit
        task.assignee = new_assignee
        if type is not None:
            task.type = type
        if task.status == "review":
            task.redo = 0 if over_limit else task.redo + 1
        task.status = "in_progress"
        if note:
            task.accept = (task.accept + "\n" if task.accept else "") + f"改派注记：{note}"
        return task
