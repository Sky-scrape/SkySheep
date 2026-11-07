"""子代理状态文件的编码容错 + 子代理直播事件契约（events.py 收编）。

编码容错（审查 M1）：subagents.json / subagent_tasks.json / team_resident.json
三份状态册共四处读取此前固定 ``read_text(encoding="utf-8")``，手改存成 GBK 的
文件抛 UnicodeDecodeError 直穿 backend.setup() 启动；统一改走
textio.read_text_file（编码探测，探测不出也不抛），坏文件按空册降级。

事件契约（审查 M2）：subagent_spawned / subagent_event / task_finished 三种
直播事件收进 events.py 的 AgentEvent 联合——TaskManager 以模型实例发出，
无头协议（events.py 是唯一事实来源）从此有子代理过程事件；本文件断言
事件以 AgentEvent 形态发出且每个 dump 都是无头流可消费的 NDJSON 行。
"""

from __future__ import annotations

import asyncio
import json

from skysheep.core.subagent import TaskManager
from skysheep.core.subagent_store import SubagentStore
from skysheep.core.team_resident import ResidentTeamStore
from skysheep.events import (
    AgentEvent,
    SubagentEvent,
    SubagentSpawned,
    TaskFinished,
)
from skysheep.messages import TextBlock
from skysheep.models.fake import FakeProvider

# utf-8 与 gb18030 都解不出的字节（0xFF 不在任何合法序列里），
# textio 探测失败也不抛——交给 JSON 解析兜底降级
_GARBAGE = b"\xff\xff\xff\xff"


async def _wait_status(tasks, task_id, status, timeout=5.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        rec = tasks.status(task_id)
        if rec.status == status:
            return rec
        await asyncio.sleep(0.01)
    raise AssertionError(f"任务未在 {timeout}s 内变为 {status}（当前 {rec.status}）")


# ---- 编码容错：GBK 状态文件不崩、坏文件按空册降级 ----


def test_subagent_store_gbk_file_loads(tmp_path):
    """GBK 编码的 subagents.json 不再炸启动：中文内容原样读回。"""
    path = tmp_path / "subagents.json"
    payload = {
        "builtin": {"task": {"model": "旗舰模型"}},
        "custom": [
            {"name": "writer-zh", "description": "中文写手", "prompt": "用中文写作"}
        ],
    }
    path.write_bytes(json.dumps(payload, ensure_ascii=False).encode("gbk"))
    store = SubagentStore(path)
    store.load()  # 旧实现在这里抛 UnicodeDecodeError，backend.setup() 启动崩
    assert store.builtin["task"].model == "旗舰模型"
    d = store.get_custom("writer-zh")
    assert d is not None and d.description == "中文写手"


def test_subagent_store_undecodable_file_degrades_to_empty(tmp_path):
    """探测不出编码的坏文件不致命：按空册降级，下次保存覆盖。"""
    path = tmp_path / "subagents.json"
    path.write_bytes(_GARBAGE)
    store = SubagentStore(path)
    store.load()
    assert store.custom == []
    assert store.builtin["task"].provider == "" and store.builtin["task"].model == ""


def test_task_manager_state_gbk_file_loads(tmp_path):
    """GBK 编码的任务簿不崩：重启后中文 prompt / result 原样读回。"""
    state = tmp_path / "subagent_tasks.json"
    rec = {
        "id": "aabbccddee", "agent_type": "explore", "prompt": "调研中文资料",
        "session_id": "sess-zh", "status": "done", "result": "结论：一切正常",
        "created_at": 1.0, "finished_at": 2.0,
    }
    state.write_bytes(json.dumps({"tasks": [rec]}, ensure_ascii=False).encode("gbk"))
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([]),
        working_dir=tmp_path, state_path=state,
    )
    got = tasks.status("aabbccddee")
    assert got.status == "done" and got.result == "结论：一切正常"
    assert got.prompt == "调研中文资料" and got.session_id == "sess-zh"


def test_task_manager_state_garbage_degrades_to_empty(tmp_path):
    """坏任务簿按空簿降级：不崩、不丢本实例写入路径（_read_state_file 回空）。"""
    state = tmp_path / "subagent_tasks.json"
    state.write_bytes(_GARBAGE)
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([]),
        working_dir=tmp_path, state_path=state,
    )
    assert tasks._tasks == {}
    assert tasks._read_state_file() == []


def test_team_resident_gbk_file_loads(tmp_path):
    """GBK 编码的 team_resident.json 不再炸启动：登记条目原样读回。"""
    path = tmp_path / "team_resident.json"
    entry = {
        "team_id": "team-1", "name": "常驻小队", "template": "巡逻模板",
        "project_id": 3, "board": [], "last_seq": 4,
    }
    path.write_bytes(json.dumps({"teams": [entry]}, ensure_ascii=False).encode("gbk"))
    store = ResidentTeamStore(path)
    store.load()
    assert len(store.teams) == 1
    assert store.teams[0].name == "常驻小队" and store.teams[0].last_seq == 4


def test_team_resident_garbage_degrades_to_empty(tmp_path):
    """坏注册表按空册降级，下次保存整体覆盖。"""
    path = tmp_path / "team_resident.json"
    path.write_bytes(_GARBAGE)
    store = ResidentTeamStore(path)
    store.load()
    assert store.teams == []


# ---- 事件契约：三种直播事件收进 AgentEvent 联合 ----


async def test_task_manager_emits_agentevent_contract(tmp_path):
    """构造 TaskManager 传假 emitter：三种事件以 AgentEvent 模型实例发出。

    - 全部 isinstance(ev, AgentEvent)：events.py 联合成员，无头协议唯一
      事实来源（旧实现裸发 dict，无头流里子代理过程零事件）；
    - spawned 首发且字段与旧 dict 契约一致（前端 app.js 按 kind 与字段名
      消费，不受影响）；
    - subagent_event 内层子代理事件收在 event 字段；
    - 终态 task_finished 与任务簿一致；
    - 每个 model_dump(mode="json") 都能 json.dumps 成一行 NDJSON——宿主把
      emitter 接进无头输出流时事件行即可见（docs/无头运行协议.md 事件行
      索引已收子代理族）。
    """
    seen: list[AgentEvent] = []
    tasks = TaskManager(
        provider_factory=lambda: FakeProvider([[TextBlock(text="REPORT: 契约")]]),
        working_dir=tmp_path,
        event_emitter=seen.append,  # 不 dump：直接收原始对象验形态
    )
    tid = tasks.start_background("explore", "契约用例")
    await _wait_status(tasks, tid, "done")

    assert seen, "没有发出任何直播事件"
    assert all(isinstance(ev, AgentEvent) for ev in seen), [
        type(ev).__name__ for ev in seen
    ]
    kinds = [ev.kind for ev in seen]
    assert kinds[0] == "subagent_spawned"
    assert "subagent_event" in kinds
    assert kinds[-1] == "task_finished"

    spawned = seen[0]
    assert isinstance(spawned, SubagentSpawned)
    assert spawned.task_id == tid and spawned.background is True
    assert spawned.queued is False and spawned.agent_type == "explore"
    assert spawned.prompt == "契约用例"

    sub_events = [ev for ev in seen if isinstance(ev, SubagentEvent)]
    assert sub_events and all(ev.task_id == tid for ev in sub_events)
    inner_kinds = {ev.event["kind"] for ev in sub_events}
    assert inner_kinds, "内层事件字典为空"
    assert "permission_request" not in inner_kinds  # 子代理内已预拒绝，不直播

    finished = next(ev for ev in seen if isinstance(ev, TaskFinished))
    assert finished.task_id == tid and finished.status == "done"
    assert finished.agent_type == "explore" and finished.prompt == "契约用例"

    # 无头流里可见：dump 后逐行 NDJSON（kind 判别字段在行上）
    rows = [json.dumps(ev.model_dump(mode="json"), ensure_ascii=False) for ev in seen]
    assert all(json.loads(row)["kind"] == ev.kind for row, ev in zip(rows, seen, strict=True))
