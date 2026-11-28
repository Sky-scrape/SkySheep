from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .base import (
    ChangeRecorder,
    Safety,
    Tool,
    ToolContext,
    ToolError,
    ToolRegistry,
    truncate_output,
)
from .browser import BrowserTool
from .browser_cdp import WebPageTool
from .computer import (
    ClipboardReadTool,
    ClipboardWriteTool,
    KeyboardTool,
    MouseTool,
    ScreenshotTool,
    WindowListTool,
    WindowTool,
)
from .doc_search import IndexDocumentsTool, SearchDocumentsTool
from .docs import ReadDocumentTool, WriteDocumentTool
from .fs import (
    DeleteFileTool,
    EditFileTool,
    GlobTool,
    ListDirTool,
    MakeDirTool,
    MoveFileTool,
    ReadFileTool,
    WriteFileTool,
)
from .history import SearchHistoryTool
from .image import ReadImageTool
from .imagegen import GenerateImageTool
from .memory import MemoryWriteTool
from .schedule import ScheduleWriteTool
from .search import GrepTool
from .shell import RunCommandTool
from .todo import TodoWriteTool
from .web import WebFetchTool, WebSearchTool

if TYPE_CHECKING:
    # 仅注解用：network 模块很轻，但工具层不该在导入期把它拉进 CLI 冷启动路径
    from ..network import NetworkConfig

# ── 内置工具的单一清单 ────────────────────────────────────────────────────
# 新增内置工具只在这里加一条 _ToolSpec：default_tools() 的注册顺序、
# COMPUTER_TOOL_NAMES 与包 __all__ 全部由这份清单机械派生，不存在第二份
# 平行清单可漏改（改前是 import / default_tools / __all__ 三处手工同步，
# 新工具漏一处就静默缺席）。
#
# gate 档位（与 default_tools 形参一一对应，默认都站在安全的一边）：
# - "always"   恒注册；
# - "computer" computer_control=True 才注册（电脑控制七件套）；
# - "browser"  browser_control=True 才注册（浏览器 + 页内自动化）；
# - "store"    传入 store（SessionStore）才注册，构造时自动注入 store=store。
# 构造注入标记（False 即不传，走工具自己的默认值）：
# - recorder=True  注入 recorder=recorder：写工具落盘前把状态记进检查点；
# - network=True   注入 network=network：全局出网设置（代理 / no_proxy / CA）；
# - websearch=True 展开 **websearch（web_search 的 provider/api_key/base_url）；
# - imagegen=True  展开 **imagegen（generate_image 的 provider/api_key/...）。
# dict 展开在前、显式注入在后——与旧调用形态一致（显式参数优先）；
# resolve_websearch / resolve_imagegen 只返回 provider/api_key 一类键，
# 与 recorder / network 实际不会撞。

_Gate = Literal["always", "computer", "browser", "store"]

_GATES = ("always", "computer", "browser", "store")


@dataclass(frozen=True)
class _ToolSpec:
    """单一清单条目：工具类 + 注册档位 + 构造注入标记。"""

    cls: type[Tool]
    gate: _Gate = "always"
    recorder: bool = False
    network: bool = False
    websearch: bool = False
    imagegen: bool = False

    def __post_init__(self) -> None:
        # 档位写错（如 "computr"）不能静默变成恒注册：截屏/键鼠这类隐私
        # 敏感工具一旦走错档就是安全回归，这里直接拒绝构造。
        if self.gate not in _GATES:
            raise ValueError(f"未知工具注册档位 {self.gate!r}（{self.cls.__name__}）")


_TOOL_SPECS: tuple[_ToolSpec, ...] = (
    # ── 恒注册：文件/命令/检索 + 任务清单 + 联网 + 文档 + 记忆 + 画图
    _ToolSpec(ReadFileTool),
    _ToolSpec(ReadImageTool),
    _ToolSpec(WriteFileTool, recorder=True),
    _ToolSpec(EditFileTool, recorder=True),
    _ToolSpec(MoveFileTool, recorder=True),
    _ToolSpec(DeleteFileTool, recorder=True),
    _ToolSpec(MakeDirTool),
    _ToolSpec(ListDirTool),
    _ToolSpec(GlobTool),
    _ToolSpec(GrepTool),
    _ToolSpec(RunCommandTool),
    _ToolSpec(TodoWriteTool),
    _ToolSpec(WebFetchTool, network=True),
    _ToolSpec(WebSearchTool, websearch=True, network=True),
    _ToolSpec(ReadDocumentTool),
    _ToolSpec(WriteDocumentTool, recorder=True),
    _ToolSpec(IndexDocumentsTool),
    _ToolSpec(SearchDocumentsTool),
    _ToolSpec(MemoryWriteTool),
    _ToolSpec(GenerateImageTool, recorder=True, imagegen=True, network=True),
    # ── computer_control：电脑控制七件套（默认关，安全审查 M16）
    _ToolSpec(ScreenshotTool, gate="computer"),
    _ToolSpec(WindowListTool, gate="computer"),
    _ToolSpec(ClipboardReadTool, gate="computer"),
    _ToolSpec(ClipboardWriteTool, gate="computer"),
    _ToolSpec(MouseTool, gate="computer"),
    _ToolSpec(KeyboardTool, gate="computer"),
    _ToolSpec(WindowTool, gate="computer"),
    # ── browser_control：浏览器 + 页内自动化（默认关）
    _ToolSpec(BrowserTool, gate="browser"),
    _ToolSpec(WebPageTool, gate="browser"),
    # ── store：跨会话日程 + 本会话历史召回（不传 store 则缺席）
    _ToolSpec(ScheduleWriteTool, gate="store"),
    _ToolSpec(SearchHistoryTool, gate="store"),
)

# 电脑控制七件套的工具名（子代理 tools="all" 排除清单、审计口径共用这份清单：
# 截屏/窗口列表属于隐私敏感只读，不该因为 "all" 就顺手进子代理工具集）。
# 由清单 gate="computer" 条目派生（name 是工具类的类属性），与注册清单同源。
COMPUTER_TOOL_NAMES = frozenset(
    spec.cls.name for spec in _TOOL_SPECS if spec.gate == "computer"
)


def default_tools(
    recorder: ChangeRecorder | None = None,
    store=None,
    websearch: dict | None = None,
    imagegen: dict | None = None,
    computer_control: bool = False,
    browser_control: bool = False,
    network: NetworkConfig | None = None,
) -> list[Tool]:
    """内置工具集：文件/命令/检索 + 任务清单 + 联网抓取/搜索 + 日程/记忆 + 文档 + 画图 + 电脑/浏览器控制。

    注册什么、按什么顺序，单一来源是上面的 _TOOL_SPECS——这里只按档位过滤、
    按注入标记构造，不维护第二份工具清单。

    recorder 传入时，write_file / edit_file / generate_image / write_document 会把落盘前
    的状态记进去，供服务层生成可回滚的检查点；CLI 等不回滚的场景可不传。
    store 传入时（SessionStore），注册跨会话日程管理工具 schedule_write 与
    本会话历史召回工具 search_history（两者都在 store 分支注册，不传则缺席）。
    websearch / imagegen 传入 config.resolve_*() 的解析结果（provider/api_key 等），
    为 None 时对应工具仍注册，调用时会给出配置指引（不阻塞其他工具）。
    computer_control=True 时才注册电脑控制七件套（screenshot / window_list / clipboard /
    mouse / keyboard / window），browser_control=True 时才注册 browser 工具与 web_page
    页内自动化工具。两者形参默认都是 False——与 config.computer_control / browser_control
    的默认值同一口径（安全审查 M16：旧默认值是 True，现有调用方都显式传值所以没成 bug，
    但新调用方漏传就会静默打开截屏/键鼠/剪贴板；默认值应该站在安全的那一边）。需要时在
    设置 · 远程控制 里打开，服务层与 CLI 都按 cfg 显式传值，热生效。
    """
    tools: list[Tool] = []
    for spec in _TOOL_SPECS:
        if spec.gate == "computer" and not computer_control:
            continue
        if spec.gate == "browser" and not browser_control:
            continue
        if spec.gate == "store" and store is None:
            continue
        kwargs: dict[str, object] = {}
        if spec.websearch:
            kwargs.update(websearch or {})
        if spec.imagegen:
            kwargs.update(imagegen or {})
        if spec.recorder:
            kwargs["recorder"] = recorder
        if spec.network:
            kwargs["network"] = network
        if spec.gate == "store":
            kwargs["store"] = store
        tools.append(spec.cls(**kwargs))
    return tools


# 包级导出：固定底座（字面量，保持与改前一致）+ 工具类由 _TOOL_SPECS 派生
# ——清单加一条，工具类即自动进入 __all__，不存在第二份可漏改的名单。
__all__ = [
    "Safety",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "truncate_output",
    "ChangeRecorder",
    "default_tools",
]
__all__ += [spec.cls.__name__ for spec in _TOOL_SPECS]
