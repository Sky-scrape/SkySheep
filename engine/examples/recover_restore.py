"""从 SQLite 镜像的空闲页残留中抢救已删除的消息，恢复为"（抢救）"会话。

背景：会话被删除后 SQLite 只回收页、不清零，消息 JSON 仍留在文件里。
本工具扫描这些残留、按时间归组，补写回数据库（只新增，不删除任何现有数据），
并额外导出一份 Markdown 到桌面便于阅读。

用法：uv run python examples/recover_restore.py [--dry-run]
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, "src")

from skysheep.config import db_path  # noqa: E402

GAP_SECONDS = 15 * 60  # 超过 15 分钟视为另一段会话
TITLE_PREFIX = "（抢救）"


def find_message_json(data: bytes) -> list[dict]:
    """扫描字节流中所有可解析的消息 JSON（含空闲页残留），去重后按时间排序。"""
    found: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(rb'\{"role":', data):
        start = m.start()
        depth = 0
        in_str = False
        esc = False
        for i in range(start, min(len(data), start + 200_000)):
            b = data[i]
            if in_str:
                if esc:
                    esc = False
                elif b == 0x5C:
                    esc = True
                elif b == 0x22:
                    in_str = False
                continue
            if b == 0x22:
                in_str = True
            elif b == 0x7B:
                depth += 1
            elif b == 0x7D:
                depth -= 1
                if depth == 0:
                    chunk = data[start : i + 1]
                    try:
                        obj = json.loads(chunk.decode("utf-8"))
                    except Exception:
                        break
                    if isinstance(obj, dict) and "role" in obj and "content" in obj:
                        key = json.dumps(obj, ensure_ascii=False, sort_keys=True)
                        if key not in seen:
                            seen.add(key)
                            found.append(obj)
                    break
    found.sort(key=lambda o: o.get("created_at", 0))
    return found


def group_by_time(messages: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for m in messages:
        ts = m.get("created_at", 0)
        if not groups or ts - groups[-1][-1].get("created_at", 0) > GAP_SECONDS:
            groups.append([m])
        else:
            groups[-1].append(m)
    return groups


def main() -> None:
    dry = "--dry-run" in sys.argv
    path = db_path()
    data = path.read_bytes()
    messages = find_message_json(data)
    groups = group_by_time(messages)
    print("数据库:", path)
    print("扫到消息:", len(messages), "条 → 归为", len(groups), "段会话")
    if dry:
        for i, g in enumerate(groups, 1):
            first = time.strftime("%m-%d %H:%M", time.localtime(g[0].get("created_at", 0)))
            print(f"  段{i}: {len(g)} 条，起始 {first}")
        return

    con = sqlite3.connect(path)
    try:
        # 幂等保护：已抢救过就退出
        exists = con.execute(
            "SELECT COUNT(*) FROM sessions WHERE title LIKE ?", (TITLE_PREFIX + "%",)
        ).fetchone()[0]
        if exists:
            print("已存在抢救会话", exists, "个，跳过（避免重复导入）")
            return

        now = time.time()
        project = con.execute(
            "SELECT id FROM projects WHERE root_path LIKE ?",
            ("%SkySheep\\engine",),
        ).fetchone()
        project_id = project[0] if project else None

        total = 0
        for i, g in enumerate(groups, 1):
            ts = g[0].get("created_at", now)
            stamp = time.strftime("%m-%d %H:%M", time.localtime(ts))
            sid = f"rec{int(ts) ^ (i * 0x9E3779B1) & 0xFFFFFFFF:08x}"
            con.execute(
                "INSERT OR IGNORE INTO sessions (id, project_id, title, pinned, created_at, updated_at)"
                " VALUES (?, ?, ?, 0, ?, ?)",
                (sid, project_id, f"{TITLE_PREFIX} {stamp} 会话", ts, ts),
            )
            for seq, m in enumerate(g):
                con.execute(
                    "INSERT INTO messages (session_id, seq, role, content, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (sid, seq, m.get("role", "user"),
                     json.dumps(m, ensure_ascii=False), m.get("created_at", ts)),
                )
                total += 1
        con.commit()
        print("已恢复", total, "条消息，共", len(groups), "个会话（标题以「抢救」开头）")

        # 导出 Markdown 到桌面便于阅读
        lines = ["# 抢救恢复的会话记录", ""]
        for g in groups:
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(g[0].get("created_at", now)))
            lines.append("## " + stamp)
            lines.append("")
            for m in g:
                text = "".join(
                    b.get("text", "") for b in (m.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "text"
                )
                tool = "".join(
                    "  [工具 {}] {}".format(b.get("name"), b.get("input"))
                    for b in (m.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                )
                if text.strip():
                    lines.append("**{}**: {}".format(m.get("role"), text.strip()))
                    lines.append("")
                if tool:
                    lines.append(f"```\n{tool}\n```")
                    lines.append("")
        out = Path.home() / "Desktop" / "SkySheep-抢救恢复的会话.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        print("已导出可读记录:", out)
    finally:
        con.close()


if __name__ == "__main__":
    main()
