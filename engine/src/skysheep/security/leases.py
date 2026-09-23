"""写租约枢纽：同一项目里并行任务的写路径协调。

背景：多会话、流水线节点、定时任务共享同一个项目工作目录，此前的并发写
没有任何协调——两个任务同时写一个文件时后写者静默覆盖前写者，谁都不知道。
本模块在进程内维护一张「活跃写租约」表：写操作执行前领走目标路径的租约，
执行完归还；目标正被别的任务持有（并行冲突）时等对方用完（有超时上限），
超时也放行，但租约带上冲突注记——agent 循环把它追加进工具结果，让模型与
用户都看得见这次并行写入。

设计边界（故意保持克制，避免伤效率）：

- **粒度是「文件路径」**，不是目录或整个工作区：只让真正撞路径的调用错开，
  并行任务各写各的文件时零等待、零开销。目录级删除/移动按其路径本身参与
  匹配，另做祖先关系判定（往正被删除的目录里写文件也算冲突）。
- **租约生命周期是「一次写调用」**（agent 循环在 finally 里归还），不是
  任务全程——任务 A 开局圈占一片路径的用法不成立。
- **等待有上限（WRITE_LEASE_WAIT_S），超时按原计划写入（fail open）**：
  既是效率护栏也是防泄漏护栏——租约只应在一次调用内存活，异常泄漏时后来者
  最多多等一个超时周期；另有 TTL 兜底清理（LEASE_TTL_S）。
- **只覆盖走权限门的工具写入**（声明了 write_path_arg 的工具）与文件面板
  保存；``run_command`` 里的命令改文件看不见，与检查点是同一条边界。
- **无会话归属（owner 为空）不构成冲突**：引擎是单进程，所有执行体都有
  会话 id；空 owner 只出现在 CLI 等独立进程或历史兼容场景，按放行处理。

按项目各一个枢纽（hub_for 以工作目录为键）：主会话门、定时任务门、流水线
headless 门、渠道门是不同实例，但同项目目录取到同一个枢纽，跨门可见。
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

# 冲突等待上限：一次工具写入是毫秒级，正常冲突等待远小于此；到点按原计划写入。
WRITE_LEASE_WAIT_S = 10.0
# 租约兜底 TTL：正常租约只存活一次调用；超过这么久的记录按泄漏清理（fail open）。
LEASE_TTL_S = 600.0


def _key(path: Path) -> str:
    """路径归一（resolve + 平台大小写/分隔符归一）后的比较键。"""
    try:
        s = str(Path(path).resolve())
    except OSError:
        s = str(path)
    return os.path.normcase(s)


def _overlaps(key_a: str, key_b: str) -> bool:
    """两键相同，或一个是另一个的路径祖先（目录删除/移动 vs 目录内写文件）。"""
    if key_a == key_b:
        return True
    sep = os.sep
    return key_a.startswith(key_b + sep) or key_b.startswith(key_a + sep)


class WriteLease:
    """一次写调用持有的租约句柄。

    ``release()`` 归还并返回冲突注记（无冲突为空串）：agent 循环把它追加进
    工具结果。可重复调用（幂等），只有第一次生效。
    """

    def __init__(self, hub: WriteLeaseHub, held: list[tuple[str, object]], note: str) -> None:
        self._hub = hub
        self._held = held  # [(键, 登记时的 entry)]：按身份归还，接管/覆盖场景不会误删别人的
        self.note = note
        self._released = False

    def release(self) -> str:
        if self._released:
            return self.note
        self._released = True
        self._hub._release(self._held)
        return self.note


class WriteLeaseHub:
    """单个项目的活跃写租约表：路径键 -> {owner, event, at}。

    只在 asyncio 单线程事件循环里操作：登记与冲突判定是纯同步代码，检查与
    占用之间没有让出点，不需要加锁；冲突等待才 await（醒来后重新检查）。
    """

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}

    def _purge_expired(self) -> None:
        """TTL 兜底：清掉存活超过 LEASE_TTL_S 的租约（泄漏时后来者不被永久卡住）。"""
        now = time.monotonic()
        expired = [k for k, e in self._entries.items() if now - e["at"] > LEASE_TTL_S]
        for k in expired:
            self._entries.pop(k, None)

    async def claim(
        self,
        paths: list[tuple[Path, str]],
        owner: str,
        wait_s: float | None = None,
    ) -> WriteLease:
        """领租约。paths 是 [(路径, 展示名)]；任一路径被**其他**会话持有时等待，
        等到后登记、超时则接管并带上冲突注记。多路径（如 move_file 的源+目标）
        逐个登记，全部拿到才算持有。"""
        if wait_s is None:
            wait_s = WRITE_LEASE_WAIT_S
        self._purge_expired()
        held: list[tuple[str, object]] = []
        timed_out: list[str] = []
        waited: list[str] = []
        deadline = time.monotonic() + wait_s
        for path, label in paths:
            key = _key(path)
            while True:
                # 命中判定：同键，或与既有租约呈祖先关系（目录租约 vs 目录内文件）
                entry = self._entries.get(key)
                if entry is None:
                    for k2, e2 in self._entries.items():
                        if _overlaps(key, k2):
                            entry = e2
                            break
                if entry is None:
                    fresh = {"owner": owner, "event": asyncio.Event(), "at": time.monotonic()}
                    self._entries[key] = fresh
                    held.append((key, fresh))
                    break
                holder = entry["owner"]
                if not holder or not owner or holder == owner:
                    # 同会话重入 / 空 owner：不构成冲突。同会话内写本就串行，
                    # 这里只会出现在面板保存与自家 Agent 撞车的边角，直接接管
                    # （登记在自己的键上，身份归还不会误删别人的租约）。
                    fresh = {"owner": owner, "event": asyncio.Event(), "at": time.monotonic()}
                    self._entries[key] = fresh
                    held.append((key, fresh))
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out.append(label)
                    fresh = {"owner": owner, "event": asyncio.Event(), "at": time.monotonic()}
                    self._entries[key] = fresh
                    held.append((key, fresh))
                    break
                waited.append(label)
                try:
                    await asyncio.wait_for(entry["event"].wait(), timeout=remaining)
                except TimeoutError:
                    continue  # 超时醒来重新检查（对方可能还没真正释放）
        note = ""
        if timed_out:
            note = (
                f"检测到并行写入：「{timed_out[0]}」正被其他会话/任务写入，且在 "
                f"{wait_s:.0f} 秒内未结束。本次写入已按原计划执行，可能覆盖对方的改动；"
                "如该文件仍需继续操作，请先重新读取确认最新内容。"
            )
        elif waited:
            note = (
                f"检测到并行写入：「{waited[0]}」刚由其他会话/任务写完，已等待后继续。"
                "写入基于的是更早读到的内容；如需基于最新内容继续，请先重新读取该文件。"
            )
        return WriteLease(self, held, note)

    def _release(self, held: list[tuple[str, object]]) -> None:
        for key, entry in held:
            if self._entries.get(key) is entry:
                entry["event"].set()  # 唤醒等待者（若 entry 已被接管，set 的是旧对象，无副作用）
                self._entries.pop(key, None)


# 按项目目录各一个枢纽；进程内全局表（与 tools/shell.py 的后台进程表同一模式）。
_HUBS: dict[str, WriteLeaseHub] = {}


def hub_for(working_dir: Path | None) -> WriteLeaseHub | None:
    """取（或创建）某个工作目录对应的枢纽；无项目态返回 None。"""
    if not working_dir:
        return None
    try:
        key = os.path.normcase(str(Path(working_dir).resolve()))
    except OSError:
        key = os.path.normcase(str(working_dir))
    hub = _HUBS.get(key)
    if hub is None:
        hub = _HUBS[key] = WriteLeaseHub()
    return hub
