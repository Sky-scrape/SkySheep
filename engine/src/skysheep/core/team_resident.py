"""常驻团队注册表（T3.2）：跨会话存活的团队登记与工单板快照。

路径由宿主注入，坏文件按空册处理，写入经 textio 原子替换（与团队模板册
team_store.TeamTemplateStore 同姿态）；模块本身不访问真实 ~/.skysheep，
单测可使用临时路径。team_id 是唯一键——常驻的是「这支队伍」（频道历史按
team_id 落 team_messages 表、工单板快照随条目落盘），定时团队触发时按
（模板名，项目）匹配复用，而不是每次重建。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from ..textio import write_text_atomic
from .team_store import TeamError

# 条目数上限（最旧更新时间的先淘汰）：常驻是显式声明，正常不会堆积；
# 上限只为「永不收敛的写盘」设一道卫生闸（同 _TEAM_INTERRUPTED_KEEP 精神）
RESIDENT_TEAMS_KEEP = 100


class ResidentTeamEntry(BaseModel):
    """一条常驻登记：哪支队伍常驻 + 复用所需的元数据。

    board 是工单板快照（TeamTask.to_dict 列表，含原工单 id 与依赖引用——
    复用时按原 id 回放，频道消息里的 task_ref 才指得回来）；last_seq 是
    频道 seq 水位（复用时从此续号，team_messages 的 (team_id, seq) 不冲突）。
    template 是建队来源的模板名（未知为空串——不匹配任何定时任务，只登记）；
    project_id 是登记时所属项目（定时任务按同模板且同项目匹配）。
    """

    team_id: str
    name: str = ""
    template: str = ""
    project_id: int | None = None
    session_id: str = ""  # 登记时（或最近一次复用）的会话，审计线索
    director_mode: str = "user"
    roster: list[str] = Field(default_factory=list)  # 成员名列表（展示与比对）
    board: list[dict] = Field(default_factory=list)  # 工单板快照（原样回放）
    last_seq: int = 0  # 频道 seq 水位
    wake_count: int = 0  # 定时唤醒次数
    created_at: float = 0.0
    updated_at: float = 0.0


class ResidentTeamStore:
    """~/.skysheep/team_resident.json 的读写与内存态（T3.2 常驻团队）。

    文件结构 {"teams": [ResidentTeamEntry…]}；坏文件（不存在/非 JSON/条目
    形状不对）不致命：回空列表或跳过坏条目，下次保存整体覆盖（沿
    team_store 的容错姿态）。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.teams: list[ResidentTeamEntry] = []

    # ---- 读写 ----

    def load(self) -> None:
        self.teams = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return  # 坏文件不致命：回空列表，下次保存覆盖
        items = data.get("teams") if isinstance(data, dict) else None
        for item in items or []:
            if isinstance(item, dict):
                try:
                    self.teams.append(ResidentTeamEntry(**item))
                except ValidationError:
                    continue  # 单条形状不对：跳过，不拖垮整份注册表

    def save(self) -> None:
        # 超上限时按 updated_at 从旧到新淘汰（常驻是显式声明，正常触不到）
        if len(self.teams) > RESIDENT_TEAMS_KEEP:
            self.teams.sort(key=lambda t: t.updated_at)
            self.teams = self.teams[-RESIDENT_TEAMS_KEEP:]
        data = {"teams": [t.model_dump() for t in self.teams]}
        # 原子写（沿 teams.json 同款）：写一半被中断不留半截 JSON
        write_text_atomic(
            self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        )

    # ---- 增删查（team_id 唯一键）----

    def list(self) -> list[dict]:
        return [t.model_dump() for t in self.teams]

    def get(self, team_id: str) -> ResidentTeamEntry | None:
        return next((t for t in self.teams if t.team_id == team_id), None)

    def is_resident(self, team_id: str) -> bool:
        return bool(team_id) and self.get(team_id) is not None

    def upsert(self, entry: ResidentTeamEntry) -> ResidentTeamEntry:
        """登记/刷新一支常驻队伍：team_id 已存在则原位覆盖，新 id 追加。

        team_id 为空按参数错拒绝（常驻的锚就是 team_id，频道落库与回放
        都凭它归组）。
        """
        team_id = str(entry.team_id or "").strip()
        if not team_id:
            raise TeamError("常驻团队登记缺少 team_id")
        entry.team_id = team_id
        now = time.time()
        if not entry.created_at:
            entry.created_at = now
        entry.updated_at = now
        for i, old in enumerate(self.teams):
            if old.team_id == team_id:
                # 保留原登记时刻（created_at 是首次声明常驻的时间）
                entry.created_at = old.created_at or now
                self.teams[i] = entry
                self.save()
                return entry
        self.teams.append(entry)
        self.save()
        return entry

    def remove(self, team_id: str) -> ResidentTeamEntry:
        """解除登记；不存在时抛 TeamError（与模板删除同姿态，不静默）。"""
        entry = self.get(team_id)
        if entry is None:
            raise TeamError("该团队没有登记为常驻")
        self.teams = [t for t in self.teams if t.team_id != team_id]
        self.save()
        return entry

    def find_for_template(
        self, template: str, project_id: int | None,
    ) -> ResidentTeamEntry | None:
        """定时团队触发的匹配：同模板 + 同项目里最近更新的一条。

        template 须非空精确匹配（空串条目是「未绑模板」的登记，不参与定时
        匹配）；project_id 须相等（常驻队伍与定时任务同厂区，工单与频道
        语境才连续）。多条命中取 updated_at 最新。
        """
        name = str(template or "").strip()
        if not name:
            return None
        hits = [
            t for t in self.teams
            if t.template == name and t.project_id == project_id
        ]
        if not hits:
            return None
        return max(hits, key=lambda t: t.updated_at)
