"""从 static/app-themes.js 的 THEMES 表派生 backend_parts/remote.py 的主题 id 白名单。

主题名单的唯一事实来源是前端 `app-themes.js` 的 `const THEMES`（26 套，id → {name, dark}）。
remote.py 里三份手抄元组（THEME_PREFS / THEME_AUTO_LIGHT / THEME_AUTO_DARK）由本工具
生成，块首尾有哨兵注释；`--check` 用于契约测试防漂移（手改即红）。

用法：
    python -I engine/tools/gen_theme_prefs.py            # 重写 remote.py 的生成块
    python -I engine/tools/gen_theme_prefs.py --check    # 只校验，过期则退出码 1
"""

from __future__ import annotations

import argparse
import difflib
import pathlib
import re
import sys

ENGINE = pathlib.Path(__file__).resolve().parents[1]
THEMES_JS = ENGINE / "src" / "skysheep" / "server" / "static" / "app-themes.js"
REMOTE_PY = ENGINE / "src" / "skysheep" / "server" / "backend_parts" / "remote.py"

BEGIN = "# --- BEGIN GENERATED THEME IDS（tools/gen_theme_prefs.py 从 app-themes.js 生成，勿手改）---"
END = "# --- END GENERATED THEME IDS ---"

# 非主题的 ui.json 取值：auto=跟随系统、timed=定时切换、light/dark=旧版两档
_NON_THEME_PREFS = ("auto", "timed", "light", "dark")

_WRAP = 8  # 每行 id 数（与既有手抄版式一致：13 个 id = 8 + 5）


def load_themes() -> list[tuple[str, bool]]:
    """按表内顺序返回 (id, dark)。解析口径与 tests/test_theme_alignment.py 一致。"""
    js = THEMES_JS.read_text(encoding="utf-8")
    m = re.search(r"^const THEMES = \{(.*?)^\};", js, re.M | re.S)
    if not m:
        raise SystemExit("app-themes.js 里找不到顶层 `const THEMES = {...}` 字面量")
    themes: list[tuple[str, bool]] = []
    for name, body in re.findall(
        r"^\s{2}([A-Za-z][\w-]*):\s*\{(.*?)\},?\s*$", m.group(1), re.M
    ):
        dark = re.search(r"\bdark:\s*(true|false)", body)
        if not dark:
            raise SystemExit(f"THEMES.{name} 缺少 dark 布尔")
        themes.append((name, dark.group(1) == "true"))
    if not themes:
        raise SystemExit("THEMES 表为空")
    return themes


def _tuple_lines(name: str, groups: list[list[str]]) -> list[str]:
    """name = (...) —— 每组各自按 _WRAP 折行（保持浅/深两族的视觉分组）。"""
    lines = [f"{name} = ("]
    for ids in groups:
        for i in range(0, len(ids), _WRAP):
            chunk = ", ".join(f'"{tid}"' for tid in ids[i : i + _WRAP])
            lines.append("    " + chunk + ",")
    lines.append(")")
    return lines


def render_block(themes: list[tuple[str, bool]]) -> str:
    """生成哨兵之间的完整文本（含首尾换行，行尾 LF；写盘时按文件行尾归一）。"""
    light = [tid for tid, dark in themes if not dark]
    dark = [tid for tid, dark in themes if dark]
    lines = [
        *_tuple_lines("THEME_PREFS", [list(_NON_THEME_PREFS), light, dark]),
        "# 「跟随系统」时的落点映射（设置页主题卡下方两个下拉）：浅色/深色各落一个具体主题，",
        "# 与 wintheme.read_ui_theme、前端 themeAutoLight/themeAutoDark、首帧注入共用同一值域。",
        *_tuple_lines("THEME_AUTO_LIGHT", [light]),
        *_tuple_lines("THEME_AUTO_DARK", [dark]),
    ]
    return "\n".join(lines) + "\n"


def _split(text: str, eol: str) -> tuple[str, str, str]:
    begin = text.find(BEGIN)
    end = text.find(END)
    if begin < 0 or end < 0 or end < begin:
        raise SystemExit(f"remote.py 缺少生成块哨兵：{BEGIN} / {END}")
    head = text[: begin + len(BEGIN) + len(eol)]
    tail = text[end:]
    return head, text[begin + len(BEGIN) + len(eol) : end], tail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="只校验生成块是否最新")
    args = ap.parse_args(argv)

    raw = REMOTE_PY.read_bytes()
    text = raw.decode("utf-8")
    eol = "\r\n" if raw.count(b"\r\n") == raw.count(b"\n") else "\n"
    head, current, tail = _split(text, eol)
    expected = render_block(load_themes()).replace("\n", eol)

    if args.check:
        if current == expected:
            print("gen_theme_prefs: OK（remote.py 生成块与 app-themes.js 一致）")
            return 0
        diff = difflib.unified_diff(
            current.splitlines(), expected.splitlines(),
            fromfile="remote.py（当前）", tofile="remote.py（应由 app-themes.js 生成）",
            lineterm="",
        )
        print("\n".join(diff))
        print("\n重跑 python -I engine/tools/gen_theme_prefs.py 更新生成块", file=sys.stderr)
        return 1

    REMOTE_PY.write_bytes((head + expected + tail).encode("utf-8"))
    print(f"gen_theme_prefs: 已更新 {REMOTE_PY.relative_to(ENGINE)}（{len(load_themes())} 套主题）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
