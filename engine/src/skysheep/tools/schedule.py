"""schedule_write 工具：Agent 的日程管理（对标 todo_write 的会话工具模式）。

日程存在用户级全局库（~/.skysheep/skysheep.db 的 schedules 表），切项目不丢；
到点提醒由服务层后台循环推送（应用内横幅）。仅记录/查询，不做任何危险操作，
READONLY 免确认。
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field

from .base import Safety, Tool, ToolContext, ToolError

ScheduleAction = Literal["add", "list", "update", "delete"]

SCHEDULE_HINT = (
    "start_at 为 Unix 时间戳（秒，float）。"
    "对话里说「明天下午3点」这类相对时间时，先推算成具体时间戳再调用；"
    "推算的当前时间以本条对话最近一次提及的时间为准，拿不准就先向用户确认。"
    "只说「明天下午3点开会」时给 start_at 即可（按点事件）；"
    "说「下午2点到4点」「午休 12:00~13:30」这类区间时再同时给 end_at。"
)


class ScheduleWriteArgs(BaseModel):
    action: ScheduleAction = Field(description="add 添加 / list 查询 / update 修改 / delete 删除")
    id: int | None = Field(default=None, description="update/delete 必填：日程 id")
    title: str | None = Field(default=None, description="日程标题")
    start_at: float | None = Field(default=None, description="开始时间，Unix 秒级时间戳")
    end_at: float | None = Field(
        default=None,
        description="结束时间（可选），Unix 秒级时间戳且必须晚于 start_at；"
        "填了才是一个时间段，不填就是按点事件；update 时传 0 可清除结束时间",
    )
    notes: str | None = Field(default="", description="备注（可选）")
    remind: bool | None = Field(default=None, description="是否到点在应用内提醒")
    remind_before: int | None = Field(
        default=None, description="提前提醒的分钟数，0 = 到点才提醒（默认）"
    )
    done: bool | None = Field(default=None, description="update 可用：标记完成")
    include_done: bool = Field(default=False, description="list 时是否包含已完成")


class ScheduleWriteTool(Tool):
    name = "schedule_write"
    description = (
        "管理用户的跨会话日程（安排、会议、截止提醒等）。"
        "用户说「帮我记一下/安排一下/别忘了我下周三要…」时用 add 记录，"
        "「我都有什么安排」用 list 查询，改期/完成/取消用 update 或 delete。"
        + SCHEDULE_HINT
    )
    safety = Safety.READONLY
    # 只写 SkySheep 自有日程库（safety=READONLY 免确认），但对环境有写动作
    read_only_hint = False
    destructive_hint = False
    idempotent_hint = False
    open_world_hint = False
    args_model = ScheduleWriteArgs

    def __init__(self, store) -> None:
        self.store = store  # SessionStore（接口 duck-type，便于测试注入）

    async def run(self, args: ScheduleWriteArgs, ctx: ToolContext) -> str:
        if args.action == "add":
            if not args.title or args.start_at is None:
                raise ToolError("add 需要 title 和 start_at")
            end = self._check_end(args.end_at, args.start_at)
            row = await self.store.add_schedule(
                args.title,
                args.start_at,
                notes=args.notes or "",
                remind=args.remind is not False,
                remind_before=args.remind_before or 0,
                end_at=end,
            )
            return "scheduled: " + self._fmt(row)
        if args.action == "list":
            rows = await self.store.list_schedules(include_done=args.include_done)
            if not rows:
                return "no schedules"
            now = time.time()
            return "schedules:\n" + "\n".join(
                self._fmt(r, past=r["start_at"] <= now) for r in rows
            )
        if args.action == "update":
            if not args.id:
                raise ToolError("update 需要 id")
            # end_at 的先后关系要拿「改后」的 start_at 比：只改结束时间时必须回读
            # 现有开始时间，否则会把合法区间误判成倒置
            if args.end_at:
                base = args.start_at
                if base is None:
                    cur = await self.store.get_schedule(args.id)
                    if not cur:
                        raise ToolError(f"schedule {args.id} not found")
                    base = cur["start_at"]
                self._check_end(args.end_at, base)
            kw = {
                k: v
                for k, v in {
                    "title": args.title,
                    "notes": args.notes if args.notes else None,
                    "start_at": args.start_at,
                    "end_at": args.end_at,
                    "remind": args.remind,
                    "remind_before": args.remind_before,
                    "done": args.done,
                }.items()
                if v is not None
            }
            if not kw:
                raise ToolError("update 没有给出任何要修改的字段")
            row = await self.store.update_schedule(args.id, **kw)
            if not row:
                raise ToolError(f"schedule {args.id} not found")
            return "updated: " + self._fmt(row)
        # delete
        if not args.id:
            raise ToolError("delete 需要 id")
        ok = await self.store.delete_schedule(args.id)
        return "deleted" if ok else f"schedule {args.id} not found"

    @staticmethod
    def _check_end(end_at: float | None, start_at: float) -> float:
        """校验并归一化结束时间：None/0 = 无结束时间（按点事件）；否则必须晚于开始。"""
        if not end_at:
            return 0.0
        if float(end_at) <= float(start_at):
            raise ToolError("end_at 必须晚于 start_at（不填就是按点事件）")
        return float(end_at)

    @staticmethod
    def _fmt(row: dict, past: bool = False) -> str:
        stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["start_at"]))
        if row.get("end_at"):
            stamp += "-" + time.strftime("%H:%M", time.localtime(row["end_at"]))
        tags = []
        if row["remind"]:
            if row.get("remind_before"):
                tags.append(f"提前{row['remind_before']}分钟提醒")
            else:
                tags.append("到点提醒")
        if row["done"]:
            tags.append("已完成")
        elif past:
            tags.append("已过期")
        return "#{} [{}] {}{}".format(
            row["id"], stamp, row["title"], ("（" + "，".join(tags) + "）") if tags else ""
        )
