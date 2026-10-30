"""WS 方法参考表自动生成——从 _WS_METHODS 注册表生成 docs/WS方法参考.md（P27，防协议漂移）。

前端 WS 协议的唯一事实来源是 ``server/app.py`` 的 ``_WS_METHODS`` 总注册表
（按域合并自各 ``_dispatch_*.py`` 的 ``METHODS``），本脚本从权威聚合点取全集，
按 dispatch 域模块分节生成 ``docs/WS方法参考.md``：每个方法一行
（方法名 / local_only / 说明）。说明取 handler 函数 docstring 首行，
无 docstring 记「—」。

新增 / 改动 WS 方法后重跑本脚本更新文档；
``tests/test_ws_reference_doc.py`` 会把仓库里已提交的文档与本脚本现算内容
逐字比对，忘更新即红——协议漂移在 CI 上被抓住。

用法（engine/ 下）::

    uv run python tools/gen_ws_reference.py

退出码：生成成功 0；注册表校验失败（域重叠 / 与总分发表不一致等）抛异常非 0。
"""

from __future__ import annotations

import importlib
import inspect
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

# 脚本位于 <repo>/engine/tools/：parents[0]=tools、parents[1]=engine、parents[2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC_PATH = _REPO_ROOT / "docs" / "WS方法参考.md"

# dispatch 域模块（分节顺序 = app.py 合并 _WS_METHODS 的顺序，逐字对应）。
# app.py 新增 / 更名域模块时这里要同步，否则 load_sections 的全集校验会红。
_DOMAIN_MODULES = (
    "_dispatch_chat",
    "_dispatch_session",
    "_dispatch_files",
    "_dispatch_automation",
    "_dispatch_prefs",
    "_dispatch_ops",
    "_dispatch_collab",
    "_dispatch_ext",
)

# 总数数量级哨兵（2026-10 生成时全集 314）：只防「聚合链路坏掉拿到空表 /
# 残表」（某域模块没导出 METHODS、import 被裁剪等），不是精确锁数——
# 协议自然增长不设上限，低于量级下限才报错。
_MIN_EXPECTED_TOTAL = 100

_REGEN_CMD = "uv run python tools/gen_ws_reference.py"


class Row(NamedTuple):
    """表格一行：方法名 / 是否本机专属 / 说明。"""

    method: str
    local_only: bool
    summary: str


class Section(NamedTuple):
    """一个 dispatch 域模块的分节。"""

    module: str  # 不带 .py 的模块名，如 "_dispatch_chat"
    title: str  # 域标题，取自模块 docstring 首行的「（…域）」
    rows: list[Row]


def _ensure_importable() -> None:
    """skysheep 不可导入时把 engine/src 兜进 sys.path。

    常规入口是 ``uv run``（项目依赖已在环境里）；用裸 python 直跑脚本时靠
    这里按脚本的固定位置（engine/tools/）找到源码目录，不依赖工作目录。
    """
    try:
        import skysheep  # noqa: F401
    except ImportError:
        src = Path(__file__).resolve().parents[1] / "src"
        if src.is_dir():
            sys.path.insert(0, str(src))


def _domain_title(module_name: str, module) -> str:
    """域标题：模块 docstring 首行的「（…域）」；解析不出回退模块名。"""
    first_line = (module.__doc__ or "").strip().splitlines()
    text = first_line[0] if first_line else ""
    m = re.search(r"（(.+?)）", text)
    return m.group(1) if m else module_name


def _summary(handler) -> str:
    """表格「说明」列：handler docstring 首行（压空白、转义竖线），无则「—」。"""
    doc = inspect.getdoc(handler)
    if not doc:
        return "—"
    first = doc.splitlines()[0] if doc.splitlines() else ""
    text = " ".join(first.split())
    return text.replace("|", "\\|") if text else "—"


def load_sections() -> list[Section]:
    """从权威聚合点取全集并按域分节；注册表不一致直接抛错（fail closed）。

    权威聚合点是 ``server/app.py`` 的 ``_WS_METHODS``（各域 ``METHODS`` 的
    ``**`` 合并结果）；分节取各 ``_dispatch_*.py`` 的 ``METHODS``，并校验
    两者键集完全一致、域间无重叠（``**`` 合并遇重名会静默覆盖，必须在这里
    炸出来而不是写进文档）。注意：导入 app 会拉起完整 backend 导入链
    （约 1-2 秒），属预期成本。
    """
    _ensure_importable()
    from skysheep.server import app as server_app

    sections: list[Section] = []
    seen: Counter[str] = Counter()
    for name in _DOMAIN_MODULES:
        mod = importlib.import_module(f"skysheep.server.{name}")
        methods = getattr(mod, "METHODS", None)
        if not isinstance(methods, dict) or not methods:
            raise RuntimeError(f"{name} 没有可用的 METHODS 注册表（导出被移除或改名了？）")
        seen.update(methods.keys())
        rows = [
            Row(method=method, local_only=entry.local_only, summary=_summary(entry.handler))
            for method, entry in methods.items()
        ]
        sections.append(Section(module=name, title=_domain_title(name, mod), rows=rows))

    dupes = sorted(m for m, n in seen.items() if n > 1)
    if dupes:
        raise RuntimeError(f"方法在多个域模块重复登记（** 合并会静默覆盖）：{dupes}")

    registry = server_app._WS_METHODS
    missing = sorted(set(seen) - set(registry))
    extra = sorted(set(registry) - set(seen))
    if missing or extra:
        raise RuntimeError(
            "域模块与 _WS_METHODS 总注册表不一致"
            f"（域里多出 {missing}；总表里多出 {extra}——app.py 加了新域没登记进"
            " _DOMAIN_MODULES？）：先改 gen_ws_reference.py 再重新生成"
        )
    if len(registry) < _MIN_EXPECTED_TOTAL:
        raise RuntimeError(
            f"WS 方法总数 {len(registry)} 低于数量级下限 {_MIN_EXPECTED_TOTAL}，"
            "聚合链路疑似坏掉（拿到空表 / 残表），拒绝生成"
        )
    return sections


def build_document(now_text: str, sections: list[Section] | None = None) -> str:
    """渲染整份 Markdown。``now_text`` 是生成时间行（测试回放已提交文档的时间戳）。"""
    if sections is None:
        sections = load_sections()
    total = sum(len(s.rows) for s in sections)
    local_n = sum(1 for s in sections for r in s.rows if r.local_only)
    lines = [
        "# WS 方法参考",
        "",
        "> 本文件由 engine/tools/gen_ws_reference.py 自动生成，改协议后重跑"
        f" `{_REGEN_CMD}`，勿手改。",
        "",
        f"- 生成时间：{now_text}",
        f"- 方法总数：{total}（本机专属 local_only {local_n} 个）",
        "- 说明：dispatch handler 的 docstring 首行；无 docstring 记「—」。",
        "- local_only：是 = 远程客户端（局域网 / Tailscale 持令牌）不可调用，仅桌面端本机可执行。",
        "",
    ]
    for s in sections:
        lines.append(f"## {s.title}（`{s.module}.py`）")
        lines.append("")
        lines.append("| 方法 | local_only | 说明 |")
        lines.append("| --- | --- | --- |")
        for r in s.rows:
            flag = "是" if r.local_only else ""
            lines.append(f"| {r.method} | {flag} | {r.summary} |")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _force_utf8_stdio() -> None:
    """标准流重配为 UTF-8，防 windows runner 按 locale 编码 stdout 时报行崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def main() -> int:
    _force_utf8_stdio()
    sections = load_sections()
    doc = build_document(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), sections)
    _DOC_PATH.write_text(doc, encoding="utf-8", newline="\n")
    total = sum(len(s.rows) for s in sections)
    local_n = sum(1 for s in sections for r in s.rows if r.local_only)
    print(
        f"✓ 已生成 docs/WS方法参考.md：{total} 个方法 / {len(sections)} 个分节"
        f" / local_only {local_n} 个"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
