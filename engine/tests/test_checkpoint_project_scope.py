"""审计 SEC-M08 的跨项目检查点作用域回归（2026-10-25 报告）。

M08：跨项目分屏会话的检查点保存在会话所属项目的私有 store（runtime ctx 的
checkpoints，backend.py 轮末保存同款判定），而读取侧（list/restore/diff）旧实现
无条件用引擎当前项目的 self.checkpoints——这些会话的「撤销本轮改动」永远查不到
自己的快照，回滚安全网静默失效。

修复口径：读取侧按该会话对应 runtime 的 ctx 取 store（runtime 在时直接用
ctx.checkpoints；被淘汰/未建时按会话所属项目重新解析，同一份磁盘库）。同名
检查点 id（各项目 store 的 seq 各自从 0 起，cp1 重名是常态）按 store 实例隔离
消歧：授权会话所属的库决定查到的是哪一个。

M04 的远程授权面在 tests/test_audit_remote_scope.py；本文件只锁「会话 ↔ 项目库」
的解析（本机语义，session_id 缺省=活动会话）。
"""

from __future__ import annotations

import pytest
from conftest import FakeProvider
from test_server import make_client, recv_until

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.server.backend import ServerBackend


def _make_backend(home) -> ServerBackend:
    return ServerBackend(working_dir=home / "proj", provider_factory=lambda: FakeProvider([]))


async def _cross_project_fixture(home) -> tuple[ServerBackend, str, str]:
    """项目 A（引擎当前项目）+ 项目 B 的会话；返回 (backend, sid_a, sid_b)。"""
    be = _make_backend(home)
    await be.setup()
    proj_b = home / "projB"
    proj_b.mkdir(exist_ok=True)
    proj = await be.store.get_or_create_project(str(proj_b), "B")
    sess_b = await be.store.create_session(proj.id, "B会话")
    sid_a = (await be.new_session())["id"]
    return be, sid_a, sess_b.id


async def test_cross_project_session_reads_own_store(home):
    """M08 主张：跨项目会话的 list/diff/restore 都按会话所属项目的库解析。"""
    be, sid_a, sid_b = await _cross_project_fixture(home)
    try:
        rt_b = await be._get_runtime(sid_b)
        assert rt_b.ctx is not None and rt_b.ctx.own, "前置：B 会话绑定私有项目上下文"

        proj_b = home / "projB"
        target = proj_b / "cp.txt"
        target.write_text("v1", encoding="utf-8")
        # 保存侧口径：快照落 B 的库（rt.ctx.checkpoints）——引擎当前项目仍是 A
        cp = rt_b.ctx.checkpoints.save(sid_b, {str(target): None})
        assert cp is not None
        # 修复前的病灶：A 的引擎库里查不到该会话的快照
        assert be.checkpoints.list_for(sid_b) == []

        # 读取侧（M08 修复点）：list 按会话取 B 的库
        rows = (await be.list_checkpoints(sid_b))["checkpoints"]
        assert [r["id"] for r in rows] == [cp["id"]]

        # diff 同库：能看到 B 项目文件的改前/现状对比
        diff = await be.checkpoint_diff(cp["id"], session_id=sid_b)
        assert any("cp.txt" in f.get("path", "") for f in diff["files"])

        # restore 同库：新建文件的回滚是删除（pre 记 None）
        out = await be.restore_checkpoint(cp["id"], session_id=sid_b)
        assert out["restored"] == cp["id"] and out["files"]
        assert not target.exists(), "B 项目的新建文件应被回滚删除"

        # A 会话不受波及：A 的库里没有 B 会话的条目
        assert (await be.list_checkpoints(sid_a))["checkpoints"] == []
    finally:
        await be.shutdown()


async def test_runtime_evicted_still_resolves_own_store(home):
    """runtime 被 LRU 淘汰/未建时：按会话所属项目重新解析，同一份磁盘库。"""
    be, sid_a, sid_b = await _cross_project_fixture(home)
    try:
        rt_b = await be._get_runtime(sid_b)
        target = home / "projB" / "cp.txt"
        target.write_text("v1", encoding="utf-8")
        cp = rt_b.ctx.checkpoints.save(sid_b, {str(target): None})

        # 淘汰 runtime：读取侧必须退到 _session_project 解析出同一个库
        be.runtimes.pop(sid_b, None)
        rows = (await be.list_checkpoints(sid_b))["checkpoints"]
        assert [r["id"] for r in rows] == [cp["id"]]

        out = await be.restore_checkpoint(cp["id"], session_id=sid_b)
        assert out["restored"] == cp["id"]
        assert not target.exists()
    finally:
        await be.shutdown()


async def test_same_checkpoint_id_resolves_within_authorized_store(home):
    """同名 cp1 跨项目冲突：按授权会话所属的库消歧，互不串库。"""
    be, sid_a, sid_b = await _cross_project_fixture(home)
    try:
        rt_b = await be._get_runtime(sid_b)
        fa = home / "proj" / "cp-a.txt"
        fb = home / "projB" / "cp-b.txt"
        fa.write_text("new-a", encoding="utf-8")
        fb.write_text("new-b", encoding="utf-8")

        cp_a = be.checkpoints.save(sid_a, {str(fa): b"old-a"})
        cp_b = rt_b.ctx.checkpoints.save(sid_b, {str(fb): b"old-b"})
        assert cp_a["id"] == cp_b["id"], "前置：两库序号各自从 0 起，同名是常态"

        # A 的授权：恢复 A 库里的 cp1 → A 的文件回滚，B 的文件不动
        out = await be.restore_checkpoint(cp_a["id"], session_id=sid_a)
        assert out["restored"] == cp_a["id"]
        assert fa.read_text(encoding="utf-8") == "old-a"
        assert fb.read_text(encoding="utf-8") == "new-b"

        # B 的授权：同名 id 恢复的是 B 库里的那份
        out = await be.restore_checkpoint(cp_b["id"], session_id=sid_b)
        assert out["restored"] == cp_b["id"]
        assert fb.read_text(encoding="utf-8") == "old-b"

        # 只在 B 库存在的 id（B 库第二轮 cp2），以 A 的授权查不到 → 拒绝
        cp_b2 = rt_b.ctx.checkpoints.save(sid_b, {str(fb): b"old-b2"})
        assert cp_b2["id"] != cp_b["id"]
        with pytest.raises(RuntimeError, match="不存在或已过期"):
            await be.restore_checkpoint(cp_b2["id"], session_id=sid_a)
        assert fb.read_text(encoding="utf-8") == "old-b", "B 的文件不被 A 授权动到"

        # 同库里他人会话的检查点：归属校验拒绝（B12 口径在显式授权下仍生效）
        sid_a2 = (await be.new_session())["id"]
        f2 = home / "proj" / "cp-a2.txt"
        f2.write_text("v", encoding="utf-8")
        cp_a2 = be.checkpoints.save(sid_a2, {str(f2): b"old"})
        with pytest.raises(RuntimeError, match="归属"):
            await be.restore_checkpoint(cp_a2["id"], session_id=sid_a)
        assert f2.read_text(encoding="utf-8") == "v"
    finally:
        await be.shutdown()


async def test_unknown_session_falls_back_and_fail_closed(home):
    """未知会话：store 回落引擎当前项目库，list 回空、diff/restore 拒绝。"""
    be, sid_a, sid_b = await _cross_project_fixture(home)
    try:
        store = await be._checkpoint_store_for_session("nonexistent-session")
        assert store is be.checkpoints, "解析失败/未知会话回落当前项目库（fail closed）"
        assert (await be.list_checkpoints("nonexistent-session"))["checkpoints"] == []
        with pytest.raises(RuntimeError):
            await be.checkpoint_diff("cp1", session_id="nonexistent-session")
        with pytest.raises(RuntimeError):
            await be.restore_checkpoint("cp1", session_id="nonexistent-session")
    finally:
        await be.shutdown()


async def test_active_session_default_contract_unchanged(home):
    """本机既有契约不变：session_id 缺省取活动会话（同项目会话场景）。"""
    be, sid_a, sid_b = await _cross_project_fixture(home)
    try:
        fa = home / "proj" / "cp.txt"
        fa.write_text("v1", encoding="utf-8")
        cp = be.checkpoints.save(sid_a, {str(fa): None})

        await be.activate_session(sid_a)  # 活动指针在 A 项目会话上
        assert be.session.id == sid_a
        # 不带 session_id：按活动会话（sid_a）取引擎当前项目库——既有语义
        rows = (await be.list_checkpoints())["checkpoints"]
        assert [r["id"] for r in rows] == [cp["id"]]
        out = await be.restore_checkpoint(cp["id"])
        assert out["restored"] == cp["id"]
        assert not fa.exists()
    finally:
        await be.shutdown()


def test_cross_project_split_session_checkpoint_e2e(home, monkeypatch):
    """WS 端到端：项目 A 里建会话移入项目 B，定向发送改 B 的文件——
    快照落 B 的库；按列传 sid（跨项目分屏列的审查面板契约）可查可回滚，
    不带 sid 的裸调用只看活动会话，B 的快照不外溢。"""
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "cp-b.txt", "content": "B的改动"})],
        [TextBlock(text="written")],
    ])
    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        backend = client.app.state.backend
        proj_b = home / "projB"
        proj_b.mkdir(exist_ok=True)
        pid = client.portal.call(
            backend.store.get_or_create_project, str(proj_b.resolve()), "B"
        ).id

        ws.send_json({"id": "n1", "method": "session.new", "params": {}})
        sid = recv_until(ws, "n1")["result"]["id"]
        ws.send_json({"id": "mv", "method": "session.move",
                      "params": {"id": sid, "project_id": pid}})
        assert recv_until(ws, "mv")["ok"], "会话移入项目 B"

        # 跨项目发送（分屏列）：runtime 绑定 B 项目，快照落 B 的库
        ws.send_json({"id": "c1", "method": "chat.send",
                      "params": {"text": "改 B 项目的文件", "session_id": sid}})
        cp = None
        while cp is None:
            fr = ws.receive_json()
            if "event" in fr:
                if fr["event"] == "permission_request":
                    ws.send_json({"id": "pa", "method": "permission.respond",
                                  "params": {"request_id": fr["data"]["request_id"],
                                             "decision": "allow_once"}})
                continue
            if fr.get("id") == "c1":
                assert fr["ok"], fr.get("error")
                cp = fr["result"]["checkpoint"]
        assert cp, "轮末应产出检查点"
        target = proj_b / "cp-b.txt"
        assert target.exists()
        rt = backend.runtimes.get(sid)
        assert rt is not None and rt.ctx is not None and rt.ctx.own
        # 保存侧已落 B 的库；引擎当前项目（A）的库里没有
        assert backend.checkpoints.list_for(sid) == []

        # 分屏列聚焦口径：按列传 sid（跨项目会话不能成为活动会话——
        # activate/resume 对远程归属一律按当前项目校验，分屏列定向发送不劫持指针）
        ws.send_json({"id": "act", "method": "session.activate", "params": {"id": sid}})
        assert recv_until(ws, "act")["ok"] is False

        # 不带 sid 的裸调用只看活动会话：B 的快照不在其中（不外溢）
        ws.send_json({"id": "l0", "method": "checkpoint.list", "params": {}})
        bare = recv_until(ws, "l0")["result"]["checkpoints"]
        assert cp["id"] not in [c["id"] for c in bare]

        # 读取侧（M08 修复点）：按列显式传 sid → 解析到 B 的库
        ws.send_json({"id": "l1", "method": "checkpoint.list",
                      "params": {"session_id": sid}})
        rows = recv_until(ws, "l1")["result"]["checkpoints"]
        assert cp["id"] in [c["id"] for c in rows], "跨项目会话必须能看到自己的快照"

        ws.send_json({"id": "d1", "method": "checkpoint.diff",
                      "params": {"id": cp["id"], "session_id": sid}})
        diff = recv_until(ws, "d1")
        assert diff["ok"] and diff["result"]["files"]

        ws.send_json({"id": "r1", "method": "checkpoint.restore",
                      "params": {"id": cp["id"], "session_id": sid}})
        frame = recv_until(ws, "r1")
        assert frame["ok"] and frame["result"]["files"]
        assert not target.exists(), "新建文件回滚即删"
