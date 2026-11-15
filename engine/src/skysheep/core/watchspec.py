"""文件夹变化触发器（A2）的规格解析与扫描签名。

定时任务新增 schedule_type="watch"：不再按时间到点，而是盯一个目录
（glob 过滤），发现新文件或内容变化才触发一次运行。规格存在
``cron_tasks.time_of_day`` 文本列里（JSON），与 cron 表达式复用同一列的
先例一致——纯文本列，无需加列迁移。

本模块只做纯函数（不碰 store / asyncio），扫描结果是一份
「相对路径 → (mtime_ns, size)」的签名表；两次扫描的差异由 ``diff`` 给出。
后台轮询器（backend_parts/automation.py 的 watch 循环）每轮拿 diff 判定
是否该触发，触发后把 next_run_at 顶到当前时刻，交给既有扫描循环去跑——
执行、门控、通知、运行账本全部复用定时任务的现成链路。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

#: 一次触发最多回报的变化条数（防「目录里一下多了几万文件」把提示撑爆）
MAX_CHANGES = 20
#: 防抖上限（秒）：太短会把「保存一次文件写两次」算两轮，太长用户觉得迟钝
MAX_DEBOUNCE_S = 600
#: 单次扫描的文件数上限：超过就截断（签名表只用于 diff，不需要全量精度）
MAX_FILES = 5000


class WatchSpecError(ValueError):
    """规格不合法（入口校验用；错误文案直接可展示给用户）。"""


@dataclass(frozen=True)
class WatchSpec:
    directory: str
    pattern: str = "*"
    debounce_s: int = 5


@dataclass
class ScanResult:
    files: dict[str, tuple[int, int]] = field(default_factory=dict)
    truncated: bool = False


def parse_spec(text: str) -> WatchSpec:
    """把 time_of_day 里的 JSON 规格解析成 WatchSpec。

    兼容「裸目录路径」写法（最省事的输入）：没有 JSON 花括号就整段当目录、
    glob 默认 *。解析失败一律抛 WatchSpecError，调用方转成可读错误。
    """
    raw = str(text or "").strip()
    if not raw:
        raise WatchSpecError("文件夹触发器缺少目录配置")
    if not raw.startswith("{"):
        return WatchSpec(directory=raw)
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise WatchSpecError(f"文件夹触发器配置不是合法 JSON：{e}") from None
    if not isinstance(data, dict):
        raise WatchSpecError("文件夹触发器配置必须是对象")
    directory = str(data.get("dir") or "").strip()
    if not directory:
        raise WatchSpecError("文件夹触发器缺少 dir（要盯的目录）")
    pattern = str(data.get("glob") or "*").strip() or "*"
    try:
        debounce = int(data.get("debounce_s") or 5)
    except (TypeError, ValueError):
        debounce = 5
    return WatchSpec(
        directory=directory,
        pattern=pattern,
        debounce_s=max(0, min(debounce, MAX_DEBOUNCE_S)),
    )


def dump_spec(spec: WatchSpec) -> str:
    """WatchSpec → 存进 time_of_day 的 JSON 文本（键名与 parse 对称）。"""
    return json.dumps(
        {"dir": spec.directory, "glob": spec.pattern, "debounce_s": spec.debounce_s},
        ensure_ascii=False,
    )


def validate_spec(spec: WatchSpec) -> None:
    """入口侧校验：目录必须存在且是目录；glob 不许带路径分隔符（只匹配目录内）。

    目录不存在直接拒绝而不是「创建时先建」：目录是用户的既有资产（下载夹、
    发票夹），创建任务时写错路径，静默建一个空目录会让任务永远不触发还看不
    出哪里错。glob 带分隔符会把语义扩成递归/跨目录，超出「盯一个目录」的承诺。
    """
    p = Path(spec.directory).expanduser()
    if not p.exists():
        raise WatchSpecError("目录不存在：" + spec.directory)
    if not p.is_dir():
        raise WatchSpecError("不是目录：" + spec.directory)
    if "/" in spec.pattern or "\\" in spec.pattern:
        raise WatchSpecError("glob 只匹配目录内的文件名，不能带路径分隔符")


def scan(spec: WatchSpec) -> ScanResult:
    """扫描目录，返回「相对路径 → (mtime_ns, size)」签名表。

    隐藏文件与子目录一律跳过：监控一个订阅文件被下载进夹子这类场景不需要
    递归，递归在深目录上代价大且容易把 .git 之类的内部变化算成触发。
    扫描本身失败（权限/被删）返回空表而不是抛错——调用方把它当作「这一轮
    看不到任何文件」，下一轮目录回来会重新建立基线。
    """
    root = Path(spec.directory).expanduser()
    out = ScanResult()
    try:
        entries = sorted(root.glob(spec.pattern))
    except (OSError, ValueError):
        return out
    for p in entries:
        if len(out.files) >= MAX_FILES:
            out.truncated = True
            break
        try:
            if not p.is_file() or p.name.startswith("."):
                continue
            st = p.stat()
        except OSError:
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            rel = p.name
        out.files[rel] = (int(st.st_mtime_ns), int(st.st_size))
    return out


def diff(prev: dict[str, tuple[int, int]], cur: dict[str, tuple[int, int]]) -> list[str]:
    """两次扫描的差异：新增 / 内容变化（mtime 或 size 变了）的文件，按名排序。

    删除不算触发（「文件没了」通常不是要处理的活；真要处理请写进任务指令里
    由模型自己检查）。上限 MAX_CHANGES，超出部分只计数不列出。
    """
    changed = [
        name for name, sig in cur.items()
        if name not in prev or prev[name] != sig
    ]
    return sorted(changed)[:MAX_CHANGES]
