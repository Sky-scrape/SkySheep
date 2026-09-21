"""工具基类与注册表。

每个工具声明：
- name / description：给模型看的接口
- safety：安全分级，决定 Permission Gate 的放行策略
- args_model：pydantic 参数模型，自动导出 JSON Schema 给模型

注意：工具入口方法命名为 run（而非 execute），避免与 SQL 客户端的
execute 方法同名——静态审计工具会将其误判为 SQL 拼接。
"""

from __future__ import annotations

import abc
import asyncio
import json
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..messages import ImageBlock


class Safety(StrEnum):
    READONLY = "readonly"    # 只读操作，自动放行
    WRITE = "write"          # 写文件等写操作，默认需确认
    DANGEROUS = "dangerous"  # 执行命令等高危操作，默认需确认


class ToolError(Exception):
    """工具执行失败——错误信息会作为 tool_result 返回给模型。"""


class ToolContext:
    """工具执行上下文。"""

    def __init__(
        self,
        working_dir: Path,
        aborted: asyncio.Event | None = None,
        *,
        supports_vision: bool = True,
        restrict_to_workdir: bool = False,
    ) -> None:
        self.working_dir = working_dir
        self.aborted = aborted or asyncio.Event()
        # 当前模型是否能看图：不能时截图类工具直接给可读提示，
        # 而不是产出一张模型根本看不见的图片再让上游报错。
        self.supports_vision = supports_vision
        # 只允许访问工作目录内的路径（设置里可开；默认关，保持"能做任意文件活"的能力）
        self.restrict_to_workdir = restrict_to_workdir
        # 工具产生的图片附件（如 screenshot 的截图）：agent 循环在 tool_result
        # 之后把它们作为 user 消息并入历史，模型才能"看见"图像。
        self.images: list[ImageBlock] = []


class Tool(abc.ABC):
    name: str = ""
    description: str = ""
    safety: Safety = Safety.READONLY
    args_model: type[BaseModel]
    # 写入类工具且写目标就是 args 里的 path 字段时置 True：「自动允许写入」档
    # 只能凭它判断目标是否落在工作目录内；凭不出来的工具（MCP 写工具、剪贴板等）
    # 一律回退逐次确认——开关放行「写文件」不等于放行「任意写操作」。
    write_path_arg: bool = False
    # 写目标不是 args.path 时（如 move_file 的落点是 destination），在这里指明字段名。
    # 权限门按它取目标路径做目录边界判断，避免把「源在工作目录内」误当成落点在内。
    write_target_arg: str = "path"
    # 除写目标外还需一并留在工作目录内的路径参数（如 move_file 的 source）：
    # 「自动允许写入」档要求列出的每个字段都解析在工作目录内。
    guard_path_args: tuple[str, ...] = ()

    @abc.abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        """执行工具，返回文本结果。失败抛 ToolError。"""

    def to_schema(self) -> dict[str, Any]:
        """导出为 OpenAI function / Anthropic tool 通用的 schema。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args_model.model_json_schema(),
        }

    def arg_text(self, input_dict: dict[str, Any]) -> str:
        """用于白名单匹配与确认展示的语义文本。默认为完整参数 JSON，
        工具可覆写（如 run_command 返回命令本身，让"命令前缀"规则有意义）。"""
        return json.dumps(input_dict, ensure_ascii=False)

    def describe_call(self, args: BaseModel) -> str:
        """人类可读的一次调用描述（用于确认弹窗与事件展示）。"""
        return self.name + "(" + args.model_dump_json() + ")"


class ChangeRecorder:
    """检查点辅助：收集一轮对话内被写入文件的「改前内容」。

    write_file / edit_file 在真正覆盖前调用 record()；同一轮里同一文件
    只记第一次（即本轮开始前的状态），轮末由 backend 存入 CheckpointStore，
    用户可一键回滚本轮改动（对标 Claude Code checkpoints / Codex rollback）。
    """

    def __init__(self) -> None:
        self.pre: dict[str, bytes | None] = {}

    def record(self, path: Path) -> None:
        key = str(path)
        if key in self.pre:
            return
        try:
            self.pre[key] = path.read_bytes()
        except OSError:
            self.pre[key] = None  # 改前不存在 → 回滚时应删除

    def reset(self) -> None:
        self.pre.clear()


class ToolRegistry:
    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        # schema 缓存：注册表运行期静态不变，但 schemas() 每次模型调用都会被
        # 拉一遍——pydantic 的 model_json_schema() 不缓存，几十个工具每次重新
        # 生成一遍是纯浪费。注册时置 None，下次取时重建。
        self._schema_cache: list[dict[str, Any]] | None = None
        for t in tools or []:
            self.register(t)

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError("duplicate tool name: " + tool.name)
        self._tools[tool.name] = tool
        self._schema_cache = None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        if self._schema_cache is None:
            self._schema_cache = [t.to_schema() for t in self._tools.values()]
        return list(self._schema_cache)

    def __len__(self) -> int:
        return len(self._tools)


# ---- 通用工具函数 ----

MAX_OUTPUT_CHARS = 30_000

# 单次写入的内容上限（字符数）。读取端有各自的截断（read_file 的 MAX_FILE_CHARS
# 等），写入端此前没有任何限制：模型（或被注入污染的上下文）可以一次写一个任意大的
# 文件把磁盘写满。这里给一个远高于正常源码/文本文件的额度，超出直接报错而不是静默
# 截断——截断会写出半个文件却报告成功，比拒绝更难排查。真要写更大的产物，应由
# run_command 走命令的执行确认链路。
MAX_WRITE_CHARS = 5_000_000


def check_write_size(content: str, shown: str, previous_len: int = 0) -> None:
    """单次写入内容过大直接报错（不截断），避免模型写爆磁盘。

    ``previous_len`` 是改动前的长度（新建/覆盖时为 0）：已经超过上限的文件仍然允许
    被改小，不因存量文件而挡住修复动作。
    """
    if len(content) > MAX_WRITE_CHARS and len(content) > previous_len:
        raise ToolError(
            f"写入内容过大（{len(content):,} 字符，上限 {MAX_WRITE_CHARS:,}）：{shown}\n"
            "（需要生成更大的文件时，请分多次写入或用 run_command 执行生成命令）"
        )


def truncate_output(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """超长输出截断，保留头尾。"""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2) :]
    note = f"\n\n... [output truncated, {len(text)} chars total] ...\n\n"
    return head + note + tail


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    """把模型给的路径解析为绝对路径（相对路径基于工作目录）。

    设置了 restrict_to_workdir（设置 · 高级里的「仅允许访问工作目录」）时，
    越界路径直接拒绝——只读工具是自动放行的，没有这道闸 Agent 可以不问就
    读走磁盘上任何文件（SSH 私钥、浏览器数据、别的项目）。
    """
    if raw is not None and raw.startswith("@") and len(raw) > 1:
        raw = raw[1:]  # 用户 @ 引用带进提示词的写法，容错剥掉
    p = Path(raw)
    if not p.is_absolute():
        p = ctx.working_dir / p
    p = p.resolve()
    if getattr(ctx, "restrict_to_workdir", False):
        try:
            p.relative_to(Path(ctx.working_dir).resolve())
        except ValueError:
            raise ToolError(
                f"路径在工作目录之外，已被「仅允许访问工作目录」设置拒绝：{raw}\n"
                "（如需处理目录外的文件，请在 设置 · 高级 里关掉该开关）"
            ) from None
    return p


def rel_path(ctx: ToolContext, p: Path) -> str:
    """展示用：优先显示相对工作目录的路径。"""
    try:
        return str(p.relative_to(ctx.working_dir))
    except ValueError:
        return str(p)
