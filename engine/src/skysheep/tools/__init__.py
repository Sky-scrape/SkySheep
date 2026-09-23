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
from .computer import (
    ClipboardReadTool,
    ClipboardWriteTool,
    KeyboardTool,
    MouseTool,
    ScreenshotTool,
    WindowListTool,
    WindowTool,
)
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
from .image import ReadImageTool
from .imagegen import GenerateImageTool
from .memory import MemoryWriteTool
from .schedule import ScheduleWriteTool
from .search import GrepTool
from .shell import RunCommandTool
from .todo import TodoWriteTool
from .web import WebFetchTool, WebSearchTool

# 电脑控制七件套的工具名（子代理 tools="all" 排除清单、审计口径共用这份清单：
# 截屏/窗口列表属于隐私敏感只读，不该因为 "all" 就顺手进子代理工具集）
COMPUTER_TOOL_NAMES = frozenset({
    "screenshot", "window_list", "clipboard_read", "clipboard_write",
    "mouse", "keyboard", "window",
})


def default_tools(
    recorder: ChangeRecorder | None = None,
    store=None,
    websearch: dict | None = None,
    imagegen: dict | None = None,
    computer_control: bool = False,
    browser_control: bool = False,
) -> list[Tool]:
    """内置工具集：文件/命令/检索 + 任务清单 + 联网抓取/搜索 + 日程/记忆 + 文档 + 画图 + 电脑/浏览器控制。

    recorder 传入时，write_file / edit_file / generate_image / write_document 会把落盘前
    的状态记进去，供服务层生成可回滚的检查点；CLI 等不回滚的场景可不传。
    store 传入时（SessionStore），注册跨会话日程管理工具 schedule_write。
    websearch / imagegen 传入 config.resolve_*() 的解析结果（provider/api_key 等），
    为 None 时对应工具仍注册，调用时会给出配置指引（不阻塞其他工具）。
    computer_control=True 时才注册电脑控制七件套（screenshot / window_list / clipboard /
    mouse / keyboard / window），browser_control=True 时才注册 browser 工具。两者形参
    默认都是 False——与 config.computer_control / browser_control 的默认值同一口径
    （安全审查 M16：旧默认值是 True，现有调用方都显式传值所以没成 bug，但新调用方
    漏传就会静默打开截屏/键鼠/剪贴板；默认值应该站在安全的那一边）。需要时在
    设置 · 远程控制 里打开，服务层与 CLI 都按 cfg 显式传值，热生效。
    """
    tools = [
        ReadFileTool(),
        ReadImageTool(),
        WriteFileTool(recorder=recorder),
        EditFileTool(recorder=recorder),
        MoveFileTool(recorder=recorder),
        DeleteFileTool(recorder=recorder),
        MakeDirTool(),
        ListDirTool(),
        GlobTool(),
        GrepTool(),
        RunCommandTool(),
        TodoWriteTool(),
        WebFetchTool(),
        WebSearchTool(**(websearch or {})),
        ReadDocumentTool(),
        WriteDocumentTool(recorder=recorder),
        MemoryWriteTool(),
        GenerateImageTool(recorder=recorder, **(imagegen or {})),
    ]
    if computer_control:
        tools += [
            ScreenshotTool(),
            WindowListTool(),
            ClipboardReadTool(),
            ClipboardWriteTool(),
            MouseTool(),
            KeyboardTool(),
            WindowTool(),
        ]
    if browser_control:
        tools.append(BrowserTool())
    if store is not None:
        tools.append(ScheduleWriteTool(store))
    return tools


__all__ = [
    "Safety",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolRegistry",
    "truncate_output",
    "ChangeRecorder",
    "default_tools",
    "BrowserTool",
    "ReadFileTool",
    "ReadImageTool",
    "WriteFileTool",
    "EditFileTool",
    "MoveFileTool",
    "DeleteFileTool",
    "MakeDirTool",
    "ListDirTool",
    "GlobTool",
    "GrepTool",
    "RunCommandTool",
    "TodoWriteTool",
    "ScheduleWriteTool",
    "WebFetchTool",
    "WebSearchTool",
    "ReadDocumentTool",
    "WriteDocumentTool",
    "MemoryWriteTool",
    "GenerateImageTool",
    "ScreenshotTool",
    "WindowListTool",
    "ClipboardReadTool",
    "ClipboardWriteTool",
    "MouseTool",
    "KeyboardTool",
    "WindowTool",
]
