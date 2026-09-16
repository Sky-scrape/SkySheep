"""从 SQLite 文件镜像（含空闲页残留）中扫描可恢复的消息 JSON。

用法：uv run python examples/recover_scan.py [db路径]
输出：找到的消息条数、所属会话、内容预览（不改动原库，只读）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, "src")


def find_json_objects(data: bytes) -> list[dict]:
    """在字节流里找出所有可解析的 JSON 对象（含空闲页残留）。"""
    found: list[dict] = []
    seen: set[str] = set()
    # 消息体以 {"role": 开头（Message.model_dump 的键序：role 在前）
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
                elif b == 0x5C:  # backslash
                    esc = True
                elif b == 0x22:  # quote
                    in_str = False
                continue
            if b == 0x22:
                in_str = True
            elif b == 0x7B:  # {
                depth += 1
            elif b == 0x7D:  # }
                depth -= 1
                if depth == 0:
                    chunk = data[start : i + 1]
                    try:
                        obj = json.loads(chunk.decode("utf-8"))
                    except Exception:
                        break
                    key = json.dumps(obj, ensure_ascii=False, sort_keys=True)[:500]
                    if key not in seen and isinstance(obj, dict) and "role" in obj:
                        seen.add(key)
                        found.append(obj)
                    break
    return found


def main() -> None:
    db = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".skysheep" / "skysheep.db"
    data = db.read_bytes()
    msgs = find_json_objects(data)
    print("扫描文件:", db, "大小", len(data), "bytes")
    print("扫到消息条数:", len(msgs))
    by_session: dict[str, int] = {}
    for m in msgs:
        by_session[m.get("id", "?")[:8]] = by_session.get(m.get("id", "?")[:8], 0)
    roles = {}
    for m in msgs:
        roles[m.get("role")] = roles.get(m.get("role"), 0) + 1
    print("按角色:", roles)
    for m in msgs[:12]:
        blocks = m.get("content") or []
        text = ""
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "text":
                text += b.get("text", "")
        print("  [{}] {}".format(m.get("role"), text[:70].replace("\n", " ")))


if __name__ == "__main__":
    main()
