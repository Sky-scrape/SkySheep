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


def default_tools(
    recorder: ChangeRecorder | None = None,
    store=None,
    websearch: dict | None = None,
    imagegen: dict | None = None,
    computer_control: bool = True,
    browser_control: bool = True,
) -> list[Tool]:
    """内置工具集：文件/命令/检索 + 任务清单 + 联网抓取/搜索 + 日程/记忆 + 文档 + 画图 + 电脑/浏览器控制。

    recorder 传入时，write_file / edit_file / generate_image / write_document 会把落盘前
    的状态记进去，供服务层生成可回滚的检查点；CLI 等不回滚的场景可不传。
    store 传入时（SessionStore），注册跨会话日程管理工具 schedule_write。
    websearch / imagegen 传入 config.resolve_*() 的解析结果（provider/api_key 等），
    为 None 时对应工具仍注册，调用时会给出配置指引（不阻塞其他工具）。
    computer_control=False 时不注册电脑控制七件套（screenshot / window_list / clipboard /
    mouse / keyboard / window），对应设置 · 远程控制里的总开关（默认关，缩小攻击面）。
    browser_control=False 时不注册 browser 工具（用系统浏览器开网页/搜索），同样默认关。
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
