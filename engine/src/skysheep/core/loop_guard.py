"""一轮内的重复调用检测与工具熔断。

模型打转的典型形态不是「反复调同一个工具」（调试、轮询时正常），而是
**同样的参数、同样的结果，一遍遍重来**：每轮都烧上下文与 token，直到
max_iterations 才被动停下，用户看到的却是一个没进展的长回答。

这里的判据刻意保守，只拦「确定没有新信息」的调用：
- 指纹 = 工具名 + 规范化参数 JSON；只有指纹完全相同才累计；
- 结果哈希连续相同才算重复：模型改了参数（尝试新写法）一律放行；
- 第 2 次相同结果只在结果尾部加提示，第 4 次才拦下一次执行；
- 另设工具级熔断：同一工具连续多次返回**同一句错误**（例如权限被拒、
  服务不可用）时，本轮暂停该工具——错误文案不同（编译报错每次不同）
  不触发，避免正常调试被误伤。

所有状态都是「本轮」的：`reset()` 在每个 turn 开头调用，不跨轮记忆。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

# 同一指纹下，结果第 N 次完全相同时给模型加提示（不拦）
REPEAT_WARN_AT = 2
# 同一指纹下，结果连续相同达到 N 次时拦下一次执行
REPEAT_BLOCK_AT = 4
# 同一工具连续返回同一句错误达到 N 次时，本轮暂停该工具
FAILURE_BLOCK_AT = 5
# 单轮指纹表上限：超长轮次（几十次工具调用）不无限占内存
MAX_ENTRIES = 200
# 计算指纹时参数 JSON 的硬上限（防御性：极端大参数不参与完整哈希）
MAX_FP_CHARS = 200_000


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def fingerprint(name: str, args: dict) -> str:
    """工具调用指纹：名字 + 键序无关的规范 JSON。"""
    try:
        canonical = json.dumps(
            {"name": name, "args": args}, sort_keys=True, ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):
        canonical = f"{name}:{args!r}"
    if len(canonical) > MAX_FP_CHARS:
        canonical = canonical[:MAX_FP_CHARS]
    return _digest(canonical)


@dataclass
class _Entry:
    count: int = 0
    last_hash: str = ""
    # 连续「结果与上一次完全相同」的次数：第 1 次执行后为 0
    same_streak: int = 0


@dataclass
class LoopGuard:
    """单轮重复调用守卫。线程内使用（Agent 循环串行访问），不加锁。"""

    repeat_warn_at: int = REPEAT_WARN_AT
    repeat_block_at: int = REPEAT_BLOCK_AT
    failure_block_at: int = FAILURE_BLOCK_AT
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _err_streak: dict[tuple[str, str], int] = field(default_factory=dict)
    _disabled: dict[str, str] = field(default_factory=dict)

    def reset(self) -> None:
        self._entries.clear()
        self._err_streak.clear()
        self._disabled.clear()

    def before(self, name: str, args: dict) -> str | None:
        """执行前判定。返回非空字符串表示拦下，内容直接回灌给模型。"""
        disabled = self._disabled.get(name)
        if disabled:
            return disabled
        entry = self._entries.get(fingerprint(name, args))
        if entry is not None and entry.same_streak >= self.repeat_block_at - 1:
            return (
                f"已拦下这次重复调用：本轮以完全相同的参数调用 `{name}` 共 {entry.count} 次，"
                f"最近 {entry.same_streak + 1} 次返回的内容完全相同。"
                "请换一种做法（修改参数、换一个工具，或先获取新的信息），不要原样重试。"
            )
        return None

    def after(self, name: str, args: dict, result: str, is_error: bool) -> str:
        """执行后登记。返回要追加到工具结果尾部的提示（可为空串）。"""
        fp = fingerprint(name, args)
        entry = self._entries.get(fp)
        if entry is None:
            entry = _Entry()
            self._entries[fp] = entry
            # 简单 LRU：字典保序，超限丢最早插入的指纹（只影响检测精度，
            # 不影响正确性——被挤掉的最多退化为「不拦」）
            while len(self._entries) > MAX_ENTRIES:
                self._entries.pop(next(iter(self._entries)))
        entry.count += 1
        digest = _digest(result)
        if entry.last_hash and digest == entry.last_hash:
            entry.same_streak += 1
        else:
            entry.same_streak = 0
        entry.last_hash = digest

        if is_error:
            key = (name, digest)
            self._err_streak[key] = self._err_streak.get(key, 0) + 1
            if self._err_streak[key] >= self.failure_block_at:
                preview = " ".join(result.split())[:80]
                self._disabled[name] = (
                    f"`{name}` 本轮已连续 {self._err_streak[key]} 次返回同一种错误"
                    f"（{preview}），已暂停调用该工具。"
                    "请改变方案（换工具/换参数/先排查环境），或向用户说明卡在哪里。"
                )
        else:
            # 成功一次即清零该工具的错误连击：偶发失败不该累计成熔断
            for key in [k for k in self._err_streak if k[0] == name]:
                del self._err_streak[key]

        if entry.same_streak >= self.repeat_warn_at - 1 and entry.same_streak > 0:
            return (
                f"（提示：本轮第 {entry.count} 次以相同参数调用 `{name}`，"
                "返回结果与前次完全相同。若没有获得新信息，请换一种做法。）"
            )
        return ""
