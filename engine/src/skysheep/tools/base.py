"""工具基类与注册表。

每个工具声明：
- name / description：给模型看的接口
- safety：安全分级，决定 Permission Gate 的放行策略
- args_model：pydantic 参数模型，自动导出 JSON Schema 给模型
- read_only_hint 等四个 MCP 注解：导出到 schema 的 annotations，供外部宿主
  与工具目录在调用前分级提示（safety 管本地权限门，注解管对外呈现，两者独立）

注意：工具入口方法命名为 run（而非 execute），避免与 SQL 客户端的
execute 方法同名——静态审计工具会将其误判为 SQL 拼接。
"""

from __future__ import annotations

import abc
import asyncio
import json
import os
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


# 无项目态的统一提示：没有绑定项目 = 没有工作目录，文件/命令类工具不可用。
# 所有依赖目录的工具（fs / search / shell / docs / image）都经由 resolve_path
# 或 require_working_dir 拿到这条可读错误，而不是对 None 直接崩。
NO_PROJECT_HINT = (
    "当前没有打开任何项目（没有工作目录），文件与命令类工具无法使用。"
    "请先在侧栏「项目」区点 ＋ 添加项目并选择一个文件夹，再重试。"
)


class ToolContext:
    """工具执行上下文。

    working_dir 为 None 表示「无项目」态（没有绑定任何文件夹）：路径/命令类
    工具一律拒绝执行（见 resolve_path / require_working_dir），只读的
    对话/联网/任务类工具不受影响。
    """

    def __init__(
        self,
        working_dir: Path | None,
        aborted: asyncio.Event | None = None,
        *,
        supports_vision: bool = True,
        restrict_to_workdir: bool = False,
        session_id: str = "",
        job_containment: bool = True,
        sandbox_level: str = "job",
        use_ripgrep: bool = True,
    ) -> None:
        self.working_dir = working_dir
        self.aborted = aborted or asyncio.Event()
        # 当前模型是否能看图：不能时截图类工具直接给可读提示，
        # 而不是产出一张模型根本看不见的图片再让上游报错。
        self.supports_vision = supports_vision
        # 只允许访问工作目录内的路径（设置里可开；默认关，保持"能做任意文件活"的能力）
        self.restrict_to_workdir = restrict_to_workdir
        # 命令执行沙箱化一期：run_command 子进程纳入 Job Object 进程遏制
        # （security/sandbox_win.py，仅 Windows，失败自动降级）。默认开。
        self.job_containment = job_containment
        # 命令执行沙箱化二期："restricted" = 在 Job Object 之上叠加受限令牌
        # （特权全剥 + 低完整性，仅 Windows 且 job_containment 开启时生效，
        # 任一步失败自动降回 job-only 并附「受限令牌未生效」注记）。默认
        # "job"（一期现状）；其他取值一律按 "job" 处理。
        self.sandbox_level = sandbox_level
        # 内容搜索（grep）可选加速：检测到本机 rg（ripgrep）且模式为纯 ASCII
        # 时用 rg 扫描；不可用或失败自动回退内置纯 Python 实现。默认开。
        self.use_ripgrep = use_ripgrep
        # 本次工具调用所属的会话：子代理派生（用量/任务簿归属）、后台进程
        # 注册表（读写归属校验）都从 ctx 取，不再依赖任何全局「当前会话」指针
        # ——并行会话各跑各的轮时，全局指针会被后来者覆盖导致串台。
        self.session_id = session_id
        # 工具产生的图片附件（如 screenshot 的截图）：agent 循环在 tool_result
        # 之后把它们作为 user 消息并入历史，模型才能"看见"图像。
        self.images: list[ImageBlock] = []
        # 写工具（write_file / edit_file / write_document）本次调用产生的 diff，
        # agent 循环在工具返回后立即取走放进 ToolCallFinished 事件。挂在 ctx
        # 而不是工具实例上：同一套工具实例会被并行任务共享（任务簿缓存、
        # 自定义子代理复用主注册表），实例属性会把 A 任务的 diff 给到 B 任务。
        # 只在串行路径上由写工具写入——并发批只收 READONLY 工具，不会写它。
        self.last_diff: str = ""
        # 本次轮次里「已被权限门按『递归删除目录』确认过」的落点（绝对路径字符串）。
        # 用途只有一处：move_file(overwrite) 执行前复核目标此刻是不是目录——是，
        # 但本集合里没有它（authorize 时目标还不是目录），说明这条删除面是批准
        # 之后才长出来的（TOCTOU，审计 L04），工具必须拒绝并让模型重试，重试会
        # 重新走一次确认。授权戳只在 authorize 产出的判定里写入，工具不得自行添加。
        self.approved_deletes: set[str] = set()


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

    # MCP 工具注解（to_schema 导出为 annotations 四布尔，语义见 MCP 规范）。
    # 默认值取规范的保守缺省；内置工具一律显式声明四值，接入的远程 MCP 工具
    # 未声明时落到这里——宁可疑其有写、有破坏性，让外部宿主多提醒一次。
    read_only_hint: bool = False    # 不改动任何环境状态（宿主可考虑免确认调用）
    destructive_hint: bool = True   # 可能对环境做破坏性更新（规范缺省 true）
    idempotent_hint: bool = False   # 同参数重复调用无额外效果
    open_world_hint: bool = True    # 与本地环境之外的开放实体（网页、API）交互

    @abc.abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        """执行工具，返回文本结果。失败抛 ToolError。"""

    def to_schema(self) -> dict[str, Any]:
        """导出为 OpenAI function / Anthropic tool 通用的 schema。

        annotations 是 MCP tools/list 的标准注解，给外部宿主与目录看；
        发往模型 API 的载荷不含它——openai_compat 端点可能拒收未知字段，
        provider 层按白名单取键（anthropic_provider 本就如此）。
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.args_model.model_json_schema(),
            "annotations": {
                "readOnlyHint": self.read_only_hint,
                "destructiveHint": self.destructive_hint,
                "idempotentHint": self.idempotent_hint,
                "openWorldHint": self.open_world_hint,
            },
        }

    def arg_text(self, input_dict: dict[str, Any]) -> str:
        """用于白名单匹配与确认展示的语义文本。默认为完整参数 JSON，
        工具可覆写（如 run_command 返回命令本身，让"命令前缀"规则有意义）。"""
        return json.dumps(input_dict, ensure_ascii=False)

    def describe_call(self, args: BaseModel) -> str:
        """人类可读的一次调用描述（用于确认弹窗与事件展示）。"""
        return self.name + "(" + args.model_dump_json() + ")"


# 「改前是目录」的哨兵。read_bytes 读不了目录，旧实现把目录一律记成 None
# （语义=改前不存在）——回滚时 move_file 的源目录整树丢失、目标目录被连内容
# 删除。哨兵随检查点 blob 无损持久化，restore() 见到它就重建目录本身，
# 目录内容由逐文件条目（record_tree）还原。
DIR_MARKER = b"\x00skysheep-dir-v1\x00"


class ChangeRecorder:
    """检查点辅助：收集一轮对话内被写入文件的「改前内容」。

    write_file / edit_file 在真正覆盖前调用 record()；同一轮里同一文件
    只记第一次（即本轮开始前的状态），轮末由 backend 存入 CheckpointStore，
    用户可一键回滚本轮改动（对标 Claude Code checkpoints / Codex rollback）。

    读取一律经 asyncio.to_thread 进工作线程（审查 SEC-M10）：record 的整读、
    record_tree 的全树扫描此前都跑在事件循环上，大文件/大目录会把所有会话
    的流式 UI 一起冻结数秒到数十秒。并发前提：同一把 recorder 只被同一条
    agent 任务链顺序调用——每个会话 runtime 各持一把；团队队员按名册串行
    唤醒（_wake_actionable 的 for-await）；普通子代理的写操作被 SubagentGate
    预拒绝，够不到 recorder。
    """

    def __init__(self) -> None:
        self.pre: dict[str, bytes | None] = {}

    async def record(self, path: Path) -> None:
        """记下 path 的改前状态。读不到时按真实语义分流，绝不把「读失败」
        当成「改前不存在」——记 None 等于告诉回滚「这个文件本来就不在」，
        会把还活着的文件删掉（审查 M09）：

        - 读到内容：进检查点；
        - FileNotFoundError：改前不存在 → 记 None（回滚时应删除）；
        - 读的是目录（Windows 上 read_bytes 对目录抛 PermissionError，POSIX
          抛 IsADirectoryError，同属 OSError）：记 DIR_MARKER（回滚时重建
          目录本身）；
        - 其余读失败（文件被占用 / 无权限的普通文件）：内容拿不到，记 None
          或静默跳过都会让随后的覆盖/删除变得不可回滚——抛 ToolError 让
          调用方拒绝本次操作。

        读取在工作线程里跑（to_thread），线程里抛出的 ToolError 原样传回；
        读失败时不留半条记录（见 _pre_state）。
        """
        key = str(path)
        if key in self.pre:  # 同一轮去重：第一次记下的才是改前状态
            return
        self.pre[key] = await asyncio.to_thread(self._pre_state, path)

    def _pre_state(self, path: Path) -> bytes | None:
        """读出「改前状态」并分类（语义见 record；在工作线程执行，无 I/O 语义变更）。"""
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None  # 改前不存在 → 回滚时应删除
        except OSError:
            pass
        try:
            is_dir = path.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            return DIR_MARKER  # 改前是目录 → 回滚时重建目录本身
        raise ToolError(
            f"无法读取 {path} 的当前内容（文件可能被占用或权限不足），"
            "无法为本次改动建立可回滚的检查点，操作已拒绝。"
            "请先关闭占用该文件的程序或确认权限后重试。"
        )

    async def record_tree(self, root: Path) -> None:
        """目录：逐文件记录改前内容（目录本身的哨兵由调用方按需 record）。

        移动/删除目录时只有文件条目进检查点，回滚才能逐文件还原；空子目录
        不重建（与 delete_file 的既有行为一致）。

        扫描失败必须抛 ToolError 拒绝调用方的删除/移动，不能「记了半棵树就
        放行」：pathlib 的 rglob 会把遍历中的 PermissionError 静默吞掉（实测
        碰到受保护子目录即静默截断、不抛错），旧实现靠 rglob 时未记录的文件
        回滚时无处可还（审查 M09）。这里用 os.walk + onerror 显式收错。

        全树扫描与逐文件读取整体放进工作线程（to_thread，审查 SEC-M10），
        逐文件读取前先 stat：超过 MAX_CHECKPOINT_FILE_BYTES 的文件直接跳过
        （save 端 _limit_pre 反正会把它丢弃，白读整份还撑内存）。
        """
        await asyncio.to_thread(self._record_tree_sync, root)

    def _record_tree_sync(self, root: Path) -> None:
        # 延迟导入：core.checkpoints 反向 import 本模块的 DIR_MARKER，模块级
        # 引用会造成循环依赖；在函数内取属性，测试也能按值 monkeypatch 上限。
        from ..core.checkpoints import MAX_CHECKPOINT_FILE_BYTES

        def _on_error(e: OSError) -> None:
            raise ToolError(
                f"无法完整读取目录 {root}（{e}），检查点可能记录不全，"
                "为避免不可回滚的删除/移动，操作已拒绝。"
            ) from e

        try:
            # 先物化整棵树的遍历结果再逐个记录：扫描中途失败（onerror 抛出或
            # entry 判定抛 OSError）时一个条目都还没进检查点，不会留下「半棵树」。
            # ToolError 不是 OSError，不会被下面 except 误吞。
            entries = list(os.walk(root, onerror=_on_error))
        except OSError as e:
            raise ToolError(
                f"无法完整读取目录 {root}（{e}），检查点可能记录不全，"
                "为避免不可回滚的删除/移动，操作已拒绝。"
            ) from e
        for dirpath, _dirnames, filenames in entries:
            for name in filenames:
                p = Path(dirpath) / name
                key = str(p)
                if key in self.pre:  # 同一轮去重：第一次记下的才是改前状态
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    size = -1  # stat 不出来不跳过：交给 _pre_state 按读失败分流
                if size > MAX_CHECKPOINT_FILE_BYTES:
                    # 与 save 端 _limit_pre 的单文件上限对齐：超限文件反正进不了
                    # 快照，提前跳过省掉整份读盘与内存占用（审查 SEC-M10）。
                    continue
                self.pre[key] = self._pre_state(p)

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


def require_working_dir(ctx: ToolContext) -> Path:
    """需要真实工作目录的工具从这里取：无项目态给出可读错误。"""
    if ctx.working_dir is None:
        raise ToolError(NO_PROJECT_HINT)
    return ctx.working_dir


def resolve_path(ctx: ToolContext, raw: str) -> Path:
    """把模型给的路径解析为绝对路径（相对路径基于工作目录）。

    无项目态（working_dir 为 None）直接拒绝——没有工作目录就没有「基于工作
    目录」可言，绝对路径也不放行，否则无项目会话变成无沙箱的任意文件读写。

    设置了 restrict_to_workdir（设置 · 高级里的「仅允许访问工作目录」）时，
    越界路径直接拒绝——只读工具是自动放行的，没有这道闸 Agent 可以不问就
    读走磁盘上任何文件（SSH 私钥、浏览器数据、别的项目）。
    """
    if ctx.working_dir is None:
        raise ToolError(NO_PROJECT_HINT)
    if raw is not None and raw.startswith("@") and len(raw) > 1:
        raw = raw[1:]  # 用户 @ 引用带进提示词的写法，容错剥掉
    p = Path(raw)
    # 审查 L03：盘符相对路径（如 "C:file.txt"，盘符有、根没有）在 pathlib 的
    # join 里会重置驱动器、按引擎进程在该盘的当前目录解析——落点既不在工作
    # 目录、也不是用户以为的位置。fail-closed 直接拒绝，要求补全写法。
    if p.drive and not p.root:
        raise ToolError(
            f"盘符相对路径（{raw}）的落点会漂移到该盘的当前目录，已被拒绝；"
            "请改用绝对路径（如 C:/dir/file.txt）或不含盘符的相对路径"
        )
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
    if ctx.working_dir is None:
        return str(p)  # 无项目态到不了这里（resolve_path 已拒绝），兜个底
    try:
        return str(p.relative_to(ctx.working_dir))
    except ValueError:
        return str(p)
