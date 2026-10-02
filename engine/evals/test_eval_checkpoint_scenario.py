"""评测基线 · 检查点回滚行为场景（fake provider 驱动真实后端管线）。

场景 16：一轮改文件 → 轮末自动存检查点 → 「撤销本轮改动」还原文件后果；
叠加快照之后又被改过时的冲突脏检查（conflict → force 才恢复）。

走真实的 ServerBackend.send 管线：检查点在 _run_turn_pipeline 的轮末收尾
里由 runtime.recorder 的改前快照落库（backend.py 轮末 checkpoints.save），
还原走 restore_checkpoint（get/restore 在线程里跑、冲突先于一切写盘）。
这是「每轮改动可撤销」承诺的组装行为：模型写两轮、回滚到第一轮，文件的
存在性与内容必须精确还原。
"""

from __future__ import annotations

import asyncio

from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.server.backend import ServerBackend


async def _send_turn_allowing_writes(be: ServerBackend, text: str) -> dict:
    """跑一轮并自动同意写确认（用户在确认卡上点「允许一次」的同款决策）。"""
    evs: list = []

    async def emit(ev: dict) -> None:
        evs.append(ev)

    task = asyncio.create_task(be.send(text, emit))
    seen: set[str] = set()
    try:
        while True:
            for e in evs:
                if e.get("kind") == "permission_request" and e["request_id"] not in seen:
                    seen.add(e["request_id"])
                    assert be.respond_permission(e["request_id"], "allow_once")
            if task.done():
                break
            await asyncio.sleep(0.01)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_eval_checkpoint_restore_reverts_file_consequences(home):
    """场景 16（「每轮改动可撤销」回归）：回滚还原文件，冲突必须先问人。

    两轮各改一次同一文件，产生两条检查点，然后：
    - 快照保存后文件又被改过（第二轮改了第一轮的落点）→ 不带 force 的还原
      不动磁盘，返回 conflict 并列出冲突文件——直接覆盖会抹掉并行改动，
      必须由用户确认后再 force；
    - force 还原第一轮检查点：文件改前不存在 → 回滚后**删除**（新建的文件
      撤销 = 删掉）；
    - force 还原第二轮检查点：文件回到第一轮的内容 v1；
    - 还原后 Agent 历史追加「撤销本轮改动」系统提示（模型不再引用已回滚内容）。
    """
    proj = home / "proj"
    abs_a = str((proj / "a.txt").resolve())  # 检查点按绝对路径记录（resolve_path 口径）
    provider = FakeProvider([
        [ToolUseBlock(id="w1", name="write_file",
                      input={"path": "a.txt", "content": "v1"})],
        [TextBlock(text="第一轮写好了")],
        [ToolUseBlock(id="w2", name="write_file",
                      input={"path": "a.txt", "content": "v2"})],
        [TextBlock(text="第二轮改成 v2")],
    ])
    be = ServerBackend(working_dir=proj, provider_factory=lambda: provider)
    await be.setup()
    try:
        r1 = await _send_turn_allowing_writes(be, "建一个 a.txt")
        cp1 = r1.get("checkpoint")
        assert cp1 and cp1["paths"] == [abs_a], "改文件的轮末必须落一条检查点"
        assert (proj / "a.txt").read_text(encoding="utf-8") == "v1"

        r2 = await _send_turn_allowing_writes(be, "把 a.txt 改成 v2")
        cp2 = r2.get("checkpoint")
        assert cp2 and cp2["id"] != cp1["id"]
        assert (proj / "a.txt").read_text(encoding="utf-8") == "v2"

        # 脏检查：cp1 保存之后文件又被第二轮改过 → 不带 force 不动磁盘
        conflict = await be.restore_checkpoint(cp1["id"])
        assert conflict == {
            "conflict": True, "checkpoint_id": cp1["id"], "files": [abs_a],
        }, "快照之后又被改过的文件必须先报冲突"
        assert (proj / "a.txt").read_text(encoding="utf-8") == "v2", "冲突时磁盘原样"

        # force 还原第一轮：改前不存在 → 回滚即删除
        restored1 = await be.restore_checkpoint(cp1["id"], force=True)
        assert restored1["restored"] == cp1["id"] and restored1["files"] == [abs_a]
        assert not (proj / "a.txt").exists(), "新建文件的撤销 = 删除"

        # force 还原第二轮：文件回到该轮改前（= 第一轮）的内容
        restored2 = await be.restore_checkpoint(cp2["id"], force=True)
        assert restored2["files"] == [abs_a]
        assert (proj / "a.txt").read_text(encoding="utf-8") == "v1", "回滚还原改前内容"

        # 还原要告诉模型：历史里追加「撤销本轮改动」系统提示
        assert "撤销本轮改动" in be.agent.history[-1].text, (
            "回滚后给模型一条系统提示，不再引用已回滚的内容"
        )
    finally:
        await be.shutdown()
