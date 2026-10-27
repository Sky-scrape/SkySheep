"""SessionStore 的快捷指令（snippets）持久化方法。"""

from __future__ import annotations

import time


class SnippetsMixin:
    async def list_snippets(self) -> list[dict]:
        assert self._db
        # sort_order 升序 = 手动排序（设置页拖拽 / 新条目置顶）；同值回退
        # created_at 倒序——旧库行都是 0，行为与升级前一致
        cur = await self._db.execute(
            "SELECT * FROM snippets ORDER BY sort_order ASC, created_at DESC"
        )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def add_snippet(self, name: str, content: str) -> dict:
        assert self._db
        # 新条目排在列表最前：sort_order 取现有最小值 - 1（空库取 0），
        # 保证「刚建的提示词马上能在 ~ 候选前几位看到」
        cur = await self._db.execute("SELECT MIN(sort_order) FROM snippets")
        row = await cur.fetchone()
        base = row[0] if row and row[0] is not None else None
        sort_order = 0.0 if base is None else float(base) - 1.0
        cur = await self._db.execute(
            "INSERT INTO snippets (name, content, created_at, sort_order) VALUES (?, ?, ?, ?)",
            (name[:40], content[:8000], time.time(), sort_order),
        )
        await self._db.commit()
        row = await (await self._db.execute(
            "SELECT * FROM snippets WHERE id = ?", (cur.lastrowid,)
        )).fetchone()
        return dict(row)

    async def reorder_snippets(self, ids: list[int]) -> int:
        """按给定顺序重写 sort_order（0..n-1）；未提及的 id 保持原值。

        设置页拖拽排序提交整份顺序；写入值离散化后，后续新建条目
        仍取 min-1 置顶，两种来源不会打架。返回实际更新的行数。"""
        assert self._db
        changed = 0
        for idx, sid in enumerate(ids):
            cur = await self._db.execute(
                "UPDATE snippets SET sort_order = ? WHERE id = ?",
                (float(idx), int(sid)),
            )
            changed += cur.rowcount
        await self._db.commit()
        return changed

    async def mark_snippet_used(self, snippet_id: int) -> bool:
        """记一次插入使用：计数 +1、刷新最近使用时间（设置页展示用）。"""
        assert self._db
        cur = await self._db.execute(
            "UPDATE snippets SET use_count = use_count + 1, last_used_at = ? WHERE id = ?",
            (time.time(), snippet_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def update_snippet(
        self, snippet_id: int, name: str, content: str, enabled: bool | None = None,
    ) -> bool:
        assert self._db
        if enabled is None:
            cur = await self._db.execute(
                "UPDATE snippets SET name = ?, content = ? WHERE id = ?",
                (name[:40], content[:8000], snippet_id),
            )
        else:
            # 带启用位：编辑弹窗保存时可以顺带改停用状态
            cur = await self._db.execute(
                "UPDATE snippets SET name = ?, content = ?, enabled = ? WHERE id = ?",
                (name[:40], content[:8000], 1 if enabled else 0, snippet_id),
            )
        await self._db.commit()
        return cur.rowcount > 0

    async def set_snippet_enabled(self, snippet_id: int, enabled: bool) -> bool:
        """只切启用位（列表行上的快捷开关）：停用的提示词不再进 ~ 候选。"""
        assert self._db
        cur = await self._db.execute(
            "UPDATE snippets SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, snippet_id),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def delete_snippet(self, snippet_id: int) -> bool:
        assert self._db
        cur = await self._db.execute("DELETE FROM snippets WHERE id = ?", (snippet_id,))
        await self._db.commit()
        return cur.rowcount > 0
