"""进程被强制杀死后的恢复（真机硬杀 + WAL 恢复）。

模拟两类崩溃现场，全部用**真子进程 + 硬杀**（os._exit），而不是进程内
模拟——WAL 的崩溃恢复语义只有在进程真正不告而别时才被触发：

1. store 写路径中途被杀：已 commit 的消息必须全部可读，库可继续追加写；
2. 未提交事务被杀：恢复时整体回滚，不出现半条消息；
3. 被杀留下的临时残留（textio 原子写 .tmp）在下次启动被清扫。

只用标准库与项目代码；子进程用当前解释器（sys.executable），确保与被测
代码同一份安装。
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from skysheep.messages import Message, TextBlock
from skysheep.session.store import SessionStore

_WRITE_AND_DIE = textwrap.dedent("""
    import asyncio, os, sys
    from pathlib import Path
    from skysheep.session.store import SessionStore
    from skysheep.messages import Message

    async def main():
        store = await SessionStore(Path(sys.argv[1])).connect()
        sess = await store.create_session(1, title="崩溃现场")
        for i in range(60):
            await store.append_message(sess.id, Message.user(f"消息 {i}"))
            if i == 30:
                # 前半完成、后半进行中：不清理、不 close，硬退（模拟任务管理器杀进程）
                os._exit(9)
        os._exit(0)  # 到不了（i==30 已退）；防御性保留

    asyncio.run(main())
""")

_UNCOMMITTED_AND_DIE = textwrap.dedent("""
    import asyncio, os, sqlite3, sys, time
    from pathlib import Path
    from skysheep.session.store import SessionStore
    from skysheep.messages import Message

    async def setup():
        store = await SessionStore(Path(sys.argv[1])).connect()
        sess = await store.create_session(1, title="半写")
        await store.append_message(sess.id, Message.user("已提交的一条"))
        with open(sys.argv[1] + ".sid", "w") as f:
            f.write(sess.id)
        await store.close()

    asyncio.run(setup())

    con = sqlite3.connect(sys.argv[1])
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "INSERT INTO messages (session_id, seq, role, content, created_at)"
        " VALUES (?, 2, 'user', ?, ?)", (open(sys.argv[1] + ".sid").read(), "{}", time.time()))
    # 不 commit、不 close：WAL 里留着未提交事务，硬退
    os._exit(9)
""")

_SWEEP_TMP = textwrap.dedent("""
    import os, sys, time
    from pathlib import Path
    # 命名必须符合 textio 原子写 mkstemp 规则（尾段恰 8 个 [a-z0-9_] 字符）
    root = Path(sys.argv[1])
    stale = root / "mcp.abcdef12.tmp"
    stale.write_bytes(b"crash residue")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    os._exit(0)
""")


def _run_and_die(script: str, *args: str) -> None:
    """跑一个注定硬退的子进程；返回码必须是非零（9 = os._exit(9)）。"""
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode != 0, (
        f"子进程应被硬杀/退出，实际 returncode={proc.returncode}\n"
        f"stderr={proc.stderr[-800:]}"
    )


def _run_ok(script: str, *args: str) -> None:
    """跑一个应正常结束的制造器脚本（如造残留文件）。"""
    proc = subprocess.run(
        [sys.executable, "-c", script, *args],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"制造器脚本失败：{proc.stderr[-800:]}"


async def test_killed_writer_loses_nothing_committed(tmp_path):
    """写路径中途硬杀：已 commit 的消息全在、顺序完整、库可续写。"""
    db = tmp_path / "crash.db"
    _run_and_die(_WRITE_AND_DIE, str(db))

    store = await SessionStore(db).connect()
    try:
        sessions = await store.list_sessions(1, limit=10)
        assert sessions, "崩溃前创建的会话必须在"
        sess = sessions[0]
        msgs = await store.load_messages(sess.id)
        # 硬杀点在 i==30 之前追加的 30 条（0..29 已完成 commit），之后的最多
        # 写到 30 号；关键约束：每一行都必须是完整 JSON、seq 连续无空洞
        assert 30 <= len(msgs) <= 31, f"已提交消息数异常：{len(msgs)}"
        assert msgs[0].text.startswith("消息 0")
        seqs = [m.seq for m in msgs]
        assert seqs == list(range(1, len(msgs) + 1)), "恢复后 seq 必须连续"
        # 继续写不受影响（锁定恢复后可正常使用）
        await store.append_message(sess.id, Message.assistant([TextBlock(text="崩溃后补写")]))
        again = await store.load_messages(sess.id)
        assert len(again) == len(msgs) + 1
        assert "崩溃后补写" in again[-1].text
    finally:
        await store.close()


async def test_killed_mid_transaction_rolls_back_fully(tmp_path):
    """未提交事务被杀：恢复时整体回滚，只留已提交的一条。"""
    db = tmp_path / "mid.db"
    _run_and_die(_UNCOMMITTED_AND_DIE, str(db))

    store = await SessionStore(db).connect()
    try:
        sessions = await store.list_sessions(1, limit=10)
        assert len(sessions) == 1
        msgs = await store.load_messages(sessions[0].id)
        assert len(msgs) == 1 and "已提交的一条" in msgs[0].text, \
            f"未提交事务必须整体回滚，实际 {len(msgs)} 条"
    finally:
        await store.close()


async def test_connect_sweeps_stale_tmp_residue(tmp_path):
    """崩溃留下的久远 .tmp 残留在下次 connect 时被清扫。"""
    home_dir = tmp_path / "sweep"
    home_dir.mkdir()
    _run_ok(_SWEEP_TMP, str(home_dir))
    residue = home_dir / "mcp.abcdef12.tmp"
    assert residue.exists(), "前置条件：残留文件已造出"

    store = await SessionStore(home_dir / "sweep.db").connect()
    try:
        assert not residue.exists(), "启动清扫应回收陈旧 .tmp"
    finally:
        await store.close()
