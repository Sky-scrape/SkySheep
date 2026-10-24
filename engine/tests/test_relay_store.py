"""接力存储层：relay 三表、独立短写事务 CAS、幂等、启动中断恢复。"""
import asyncio

from skysheep.session.store import SCHEMA_VERSION, SessionStore


def _task(project_id=1, state="running", **over) -> dict:
    base = {
        "id": over.pop("id", "t1"),
        "project_id": project_id,
        "session_id": "s1",
        "state": state,
        "current_node": "n1",
        "active_slot": "A",
        "generation": 0,
        "revision": 0,
        "instruction_revision": 0,
        "step": 0,
        "owner_instance": "inst-a",
        "goal": "目标",
        "config": {"slots": {}},
        "model_snapshot": {"A": {"provider": "fake", "model": "m"}},
        "error": {},
    }
    base.update(over)
    return base


def _nodes() -> list[dict]:
    return [
        {"node_id": "n1", "slot": "A", "ordinal": 0, "status": "authorized",
         "terminal": False, "prompt": "研究"},
        {"node_id": "n2", "slot": "B", "ordinal": 1, "status": "pending",
         "terminal": True, "prompt": "复核"},
    ]


async def test_schema_version_bumped_and_tables_exist(store):
    # v5：工厂二期给 automation_runs 补 token 三列 + factory_acks
    # v6：工厂三期补 denied_count / error_kind / outcome / outcome_source + factory_alerts
    # v7：运行账本补 denied_tools（被拦工具名与次数的 JSON 快照）
    assert SCHEMA_VERSION == 7
    cur = await store._db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name IN ('relay_tasks','relay_nodes','relay_actions',"
        "'relay_pairs','relay_turns','factory_acks','factory_alerts')"
    )
    names = {r["name"] for r in await cur.fetchall()}
    assert names == {
        "relay_tasks", "relay_nodes", "relay_actions", "relay_pairs", "relay_turns",
        "factory_acks", "factory_alerts",
    }


async def test_create_get_and_cas_revision(store):
    snap = await store.relay_create(_task(), _nodes())
    assert snap["task"]["revision"] == 0
    assert [n["node_id"] for n in snap["nodes"]] == ["n1", "n2"]

    # 过期 revision 的 CAS 必须失败（不凭内存设置状态）
    assert await store.relay_mutate("t1", expected_revision=5, new_state="paused") is None
    # 正确 revision 才成功，且 revision 递增
    ok = await store.relay_mutate("t1", expected_revision=0, new_state="paused")
    assert ok is not None and ok["task"]["state"] == "paused"
    assert ok["task"]["revision"] == 1
    assert (await store.relay_get_task("t1"))["state"] == "paused"


async def test_node_report_and_step(store):
    await store.relay_create(_task(), _nodes())
    snap = await store.relay_mutate(
        "t1", expect_states=("running",), expected_revision=0, step_delta=1,
        node_id="n1", node_status="reported", node_report="报告正文",
        node_report_hash="h", node_report_id="r1", node_usage={"input_tokens": 3},
    )
    assert snap is not None
    node = snap["node"]
    assert node["status"] == "reported" and node["report"] == "报告正文"
    assert snap["task"]["step"] == 1


async def test_mutate_request_id_idempotent(store):
    await store.relay_create(_task(), _nodes())
    first = await store.relay_mutate(
        "t1", request_id="rid-1", action="pause", expect_states=("running",), new_state="paused"
    )
    assert first is not None and first["task"]["revision"] == 1
    again = await store.relay_mutate(
        "t1", request_id="rid-1", action="pause", expect_states=("running",), new_state="paused"
    )
    assert again is not None and again.get("replayed") is True
    assert (await store.relay_get_task("t1"))["revision"] == 1  # 未重复变更


async def test_action_get_put_scope(store):
    assert await store.relay_action_get("start:1", "x") is None
    await store.relay_action_put("start:1", "x", "start", {"task": {"id": "t9"}})
    assert (await store.relay_action_get("start:1", "x"))["task"]["id"] == "t9"


async def test_mark_interrupted_only_non_terminal(store):
    await store.relay_create(_task(id="live", state="running"), _nodes())
    await store.relay_create(_task(id="done", state="completed"), _nodes())
    n = await store.relay_mark_interrupted("inst-b")
    assert n == 1
    live = await store.relay_get_task("live")
    assert live["state"] == "interrupted" and live["generation"] == 1
    assert live["owner_instance"] == "inst-b"
    assert (await store.relay_get_task("done"))["state"] == "completed"


async def test_relay_get_snapshot_is_consistent_under_concurrent_writes(store):
    """relay_get 的 task 与 nodes 必须来自同一读快照（回归）。

    撕裂场景：worker 在「置节点 running + revision+1」的 CAS 提交瞬间，如果
    task 行与 node 行分两次独立 SELECT 读，就会得到「旧 revision + 新 node」
    的组合。这份快照同时是前端做 CAS 的 revision 基准——approve / pause /
    resume 拿着旧 revision 去提交必然 relay_conflict，用户看到的是「任务已
    变化，请重新读取」，机器越忙越容易命中。

    构造可判定的不变量：初始 revision=0 / status=pending，此后每次 mutate
    只改节点状态并令 revision+1，第 i 次变更后满足
    「revision 为奇数 ⇔ status=running」。
    """
    await store.relay_create(_task(), [{
        "node_id": "n1", "slot": "A", "ordinal": 0, "status": "pending",
        "terminal": False, "prompt": "研究",
    }])

    mutations = 120
    done = asyncio.Event()
    tears: list[str] = []
    reads = 0

    async def writer() -> None:
        for i in range(1, mutations + 1):
            status = "running" if i % 2 else "pending"
            changed = await store.relay_mutate("t1", node_id="n1", node_status=status)
            assert changed is not None and changed["task"]["revision"] == i
        done.set()

    async def reader() -> None:
        nonlocal reads
        while not done.is_set():
            snap = await store.relay_get("t1")
            if snap is None:
                continue
            reads += 1
            rev = snap["task"]["revision"]
            status = snap["nodes"][0]["status"]
            if (rev % 2 == 1) != (status == "running"):
                tears.append(f"revision={rev} status={status}")

    tasks = [asyncio.create_task(writer()), *(asyncio.create_task(reader()) for _ in range(3))]
    await asyncio.gather(*tasks)
    assert reads > 0
    assert tears == [], f"快照撕裂 {len(tears)} 次（共 {reads} 次读）：{tears[:5]}"


async def test_old_db_without_relay_tables_opens_empty(home):
    # 老库（无 relay 表）连接时正常建空表
    s = await SessionStore(home / "home" / "old.db").connect()
    try:
        assert await s.relay_list_tasks(1) == []
    finally:
        await s.close()
