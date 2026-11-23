"""前端事件 kind 常量表自动生成——从 events.py 反射生成 static/app-events.js（防前端裸字面量漂移）。

前端 app.js 消费引擎事件靠 ``kind`` 字符串比较分发，此前全是裸字面量，与引擎
``src/skysheep/events.py`` 之间只有 tests/test_event_kind_contract.py 的正则扫描
契约。本脚本把引擎侧升级为单一事实来源：反射 events.py 拿全部事件子类的
``kind: Literal["…"]`` 字面值，生成 ``static/app-events.js`` 的 ``AP_EVENTS``
冻结常量表（NAME = kind.upper()），app.js 的 48 处比较改为查表引用。

新增 / 改动事件类后重跑本脚本更新产物；``tests/test_event_kind_contract.py``
会把仓库里已提交的产物与本脚本现算内容逐字比对（EOL 归一化），忘更新即红；
``app.js`` 里的裸字面量由同一测试的残余扫描禁掉（引擎 kind 一律经 AP_EVENTS）。

注意：后端 WS 广播的非引擎 kind（update_progress / relay_pair_* /
session_updated / cron_updated 等）由 server/backend.py 直接 emit、不经
events.py 事件类，**不进本表**，前端分支仍是字面量。

用法（engine/ 下）::

    uv run python tools/gen_frontend_events.py            # 生成 / 覆盖产物
    uv run python tools/gen_frontend_events.py --check    # 只校验，不落盘

退出码：生成 / 校验通过 0；产物过期或不一致 1；反射链路坏掉（拿不到事件类 /
kind 重复 / 数量低于下限）抛异常非 0。
"""

from __future__ import annotations

import argparse
import re
import sys
from difflib import unified_diff
from pathlib import Path

# 产物路径按脚本位置推导：<repo>/engine/tools/ → parents[1]=engine → src/skysheep/server/static
_OUTPUT_PATH = (
    Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static" / "app-events.js"
)

_REGEN_CMD = "uv run python tools/gen_frontend_events.py"

# 数量级哨兵（生成时 events.py 有 34 个事件类）：只防「反射链路坏掉拿到空表 /
# 残表」（import 失败被吞、字段解析断裂等），不是精确锁数——事件自然增长不设上限。
_MIN_EXPECTED_KINDS = 30

# 文件模板。不带时间戳：产物与 build() 现算内容永远逐字节可比（仅 EOL 归一化），
# --check 与契约测试都不需要回放任何一行。
_HEADER = """\
/* SkySheep 引擎事件 kind 常量表——由 engine/tools/gen_frontend_events.py 从
 * src/skysheep/events.py 反射生成，勿手改；手改会被 tests/test_event_kind_contract.py
 * 逐字比对拦下。重新生成：{regen_cmd}
 * 只校验不落盘：{regen_cmd} --check
 * 加载顺序：必须先于 app.js（index.html 尾部 script 块的第一个标签）。
 */
"use strict";

// 引擎 AgentEvent 全部 {count} 个事件 kind（kind 字典序）。后端 WS 广播的非引擎 kind
// （update_progress / relay_pair_* / session_updated / cron_updated 等）不在此表：
// 它们由 server/backend.py 直接 emit，不经 events.py 事件类，handleEvent 分支里
// 仍是字面量（广播 kind 出仓属后续批次，暂不实施）。
const AP_EVENTS = Object.freeze({{
"""

_FOOTER = "});\n"

# 生成形态的表行：两格缩进 + NAME: "kind", + 行尾逗号。parse_table 锁定这条
# 形态——名字限大写蛇形、kind 限小写蛇形，任何手改（注释行 / 缩进漂移 / 少逗号）
# 都不再匹配，宁可报「解析不到」也不带病放行。
_ROW_RE = re.compile(r'^  ([A-Z_0-9]+): "([a-z_0-9]+)",$')

# 表块整体：const AP_EVENTS = Object.freeze({ … });（build() 渲染的唯一形态）
_TABLE_RE = re.compile(r"^const AP_EVENTS = Object\.freeze\(\{\n(.*?)^\}\);$", re.S | re.M)


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


def engine_kinds() -> dict[str, str]:
    """反射 skysheep.events：kind 字面值 → 事件类名（单一事实来源是 events.py 本体）。

    守卫与 tests/test_event_kind_contract.py 的反射同款：子类缺
    ``kind: Literal["…"]`` 字面默认值、kind 重复声明、数量低于下限，一律抛错
    拒绝生成——残表落盘会让前端的查表分发静默漏接事件。
    """
    _ensure_importable()
    import skysheep.events as events_mod

    kinds: dict[str, str] = {}
    for name in dir(events_mod):
        obj = getattr(events_mod, name)
        if not isinstance(obj, type) or not issubclass(obj, events_mod.Event):
            continue
        if obj is events_mod.Event:
            continue  # 基类 kind 是普通 str 且无默认值，不是可分发的事件形态
        default = obj.model_fields["kind"].default
        if not isinstance(default, str):
            raise RuntimeError(
                f"{obj.__name__} 的 kind 字段缺字面默认值——事件类必须声明"
                ' kind: Literal["…"] = "…"，否则前端契约无法机械对照')
        if default in kinds:
            raise RuntimeError(
                f"kind={default!r} 被 {kinds[default]} 与 {obj.__name__} 重复声明，"
                "前端按 kind 分发会串")
        kinds[default] = obj.__name__
    if len(kinds) < _MIN_EXPECTED_KINDS:
        raise RuntimeError(
            f"反射只拿到 {len(kinds)} 个事件 kind，低于数量级下限 {_MIN_EXPECTED_KINDS}，"
            "提取链路疑似坏掉（拿到空表 / 残表），拒绝生成")
    return kinds


def table_items() -> list[tuple[str, str]]:
    """常量表条目：按 kind 字典序的 ``(NAME, kind)``，命名规则 NAME = kind.upper()。

    命名规则在这里锁死并校验（可反解是契约测试双射断言的前提）；
    upper 后冲突（两个 kind 只差大小写）会让查表串 kind，同样拒绝生成。
    """
    kinds = engine_kinds()
    items: list[tuple[str, str]] = []
    for kind in sorted(kinds):
        name = kind.upper()
        if not re.fullmatch(r"[A-Z_0-9]+", name):
            raise RuntimeError(
                f"kind={kind!r} 的 NAME={name!r} 不是合法常量名（[A-Z_0-9]+），"
                "命名规则无法反解")
        items.append((name, kind))
    names = [n for n, _ in items]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise RuntimeError(f"NAME 上重冲突（两个 kind 只差大小写）：{dupes}")
    return items


def parse_table(text: str) -> dict[str, str]:
    """从 app-events.js 文本解析生成形态的 ``NAME → kind`` 映射（单一实现）。

    供 ``--check``、契约测试与 AP_EVENTS 引用反解共用：正则锁定
    ``Object.freeze({`` 块内、两格缩进、行尾逗号的生成形态；块内出现任何
    不匹配该形态的非空行（手加注释 / 改缩进 / 少逗号）即抛错，不带病解析。
    """
    # 仓库 core.autocrlf=true 会把 checkout 出的 LF 产物转成 CRLF，而
    # _TABLE_RE 的块锚（`{\n` 与 `});$`）只认裸 LF——先归一 EOL 再锁形态，
    # 与 --check / 契约测试的逐字比对同款防假红（形态判定不受行尾影响）。
    text = _normalize_eol(text)
    m = _TABLE_RE.search(text)
    if not m:
        raise ValueError(
            "app-events.js 里找不到 `const AP_EVENTS = Object.freeze({…});` 表块"
            "（生成形态变了？同步改生成器与消费方）")
    rows: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        row = _ROW_RE.match(line)
        if not row:
            raise ValueError(
                f"AP_EVENTS 表块内出现非生成形态的行：{line!r}\n"
                f"重跑 {_REGEN_CMD} 重新生成，勿手改")
        name, kind = row.groups()
        if name in rows:
            raise ValueError(f"AP_EVENTS 表内 NAME 重复：{name}")
        rows[name] = kind
    if not rows:
        raise ValueError("AP_EVENTS 表块解析不到任何条目，生成链路疑似坏掉")
    return rows


def build(items: list[tuple[str, str]] | None = None) -> str:
    """渲染完整产物文件。输出不含时间戳，--check 与测试可做纯逐字比对。

    ``items`` 可复用在调用方已经反射过的结果上，避免重复反射 events.py
    （main 的 --check 与生成路径都要拿条目数，曾因此反射两遍）。
    """
    if items is None:
        items = table_items()
    rows = "\n".join(f'  {name}: "{kind}",' for name, kind in items)
    return _HEADER.format(regen_cmd=_REGEN_CMD, count=len(items)) + rows + "\n" + _FOOTER


def _normalize_eol(text: str) -> str:
    """EOL 归一化：仓库 core.autocrlf=true 会把 checkout 出的 LF 转成 CRLF，
    逐字比对先归一，防行尾转换制造假红。"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


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
    parser = argparse.ArgumentParser(
        description="从 src/skysheep/events.py 反射生成 static/app-events.js 的 AP_EVENTS 常量表")
    parser.add_argument(
        "--check", action="store_true",
        help="不落盘，只校验盘上产物与现算内容一致（EOL 归一化逐字比对，不一致退出 1）")
    args = parser.parse_args()

    items = table_items()
    content = build(items)
    rel = _OUTPUT_PATH.relative_to(Path(__file__).resolve().parents[2])

    if args.check:
        if not _OUTPUT_PATH.exists():
            print(f"✗ {rel} 不存在。先跑 {_REGEN_CMD} 生成产物", file=sys.stderr)
            return 1
        on_disk = _OUTPUT_PATH.read_text(encoding="utf-8")
        if _normalize_eol(on_disk) != _normalize_eol(content):
            diff = "\n".join(list(unified_diff(
                on_disk.splitlines(),
                content.splitlines(),
                fromfile=f"{rel}（盘上）",
                tofile="生成器现算（重新生成）",
                lineterm="",
            ))[:60])
            print(
                f"✗ {rel} 与生成器现算内容不一致（产物过期或被手改）。\n"
                f"重跑 {_REGEN_CMD} 更新。\n\n{diff}",
                file=sys.stderr)
            return 1
        print(f"✓ {rel} 与生成器现算内容一致（{len(items)} 个事件 kind）")
        return 0

    _OUTPUT_PATH.write_text(content, encoding="utf-8", newline="\n")
    print(f"✓ 已生成 {rel}：{len(items)} 个事件 kind")
    return 0


if __name__ == "__main__":
    sys.exit(main())
