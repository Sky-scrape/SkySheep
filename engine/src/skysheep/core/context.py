"""上下文管理：token 估算与历史压缩（compaction）。

长会话的历史会撑爆模型上下文窗口。策略：
- 估算 token（CJK 与西文分开折算，见下；真实 usage 用来兜底下限）；
- 超过 context_limit_tokens 时，保留 system + 最近 keep_recent 条消息，
  其余历史用一次专用 LLM 调用生成结构化摘要，替换为单条 user 消息；
- 切分边界保证「保留段」不以孤儿 tool_result 开头（协议一致性要求）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from ..events import CompactionEvent
from ..messages import Message
from ..models.base import ProviderDone, ProviderTextDelta
from ..textio import read_text_file, write_text_atomic
from .collab_budget import BudgetExceeded, RunBudget
from .provider_errors import is_transient_error

logger = logging.getLogger("skysheep.core.context")

# 西文/代码约 3.5 字符一个 token；CJK 一个字符约 0.7 个 token。
# 早期版本一律按 3.5 折算，中文会话的占用率会被低估约 2 倍（中文并不是
# 3.5 个字符才一个 token），环形仪表显得很乐观、自动压缩触发过晚，
# 长中文会话有撞上上游上下文上限直接报错的风险。
OTHER_CHARS_PER_TOKEN = 3.5
CJK_TOKENS_PER_CHAR = 0.7
# CJK 标点 / 假名 / 汉字（含扩展 A、兼容区、扩展 B 以上）/ 全角符号
_CJK_RE = re.compile(
    "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    "\uf900-\ufaff\uff00-\uffef\U00020000-\U0003ffff]"
)

COMPACT_SYSTEM = (
    "You are a conversation summarizer for a coding agent. "
    "Produce a dense, factual summary in the SAME LANGUAGE as the conversation."
)

# 压缩摘要消息的识别标记。摘要以一条 user 消息的形式插进历史（对模型来说这是
# 最自然的「此前对话」载体），但它是引擎的上下文管理产物、不是用户说的话：
# 落库与回传前端时都要能把它认出来（见 server/backend.py 的轮末落库）。
# 标记文本只能由这里的常量生成，不要 elsewhere 再字面量写一份，否则两边会走偏。
SUMMARY_OPEN_TAG = "<earlier-conversation-summary>"
SUMMARY_CLOSE_TAG = "</earlier-conversation-summary>"
SUMMARY_NOTE = "(以上是此前对话的摘要，原始消息已省略。)"
# 压缩时原文落盘的目录名（相对工作目录，与 .skysheep/reports 同一层）。
# 「摘要 + 指针」而不是「只摘要」：摘要一定丢细节（路径、数字、报错原文），
# 而完整原文在 messages 表与这份文件里都还在，模型按需取回即可——即
# Super-agent 那套 Inline / Spill 的最小可用版。
TRANSCRIPT_DIR = "transcripts"
# 单份 transcript 的字符上限：超过就截断并注明。压缩本来就是为了省窗口，
# 落盘不该反过来把磁盘写爆（一次压缩的量级通常是几十万字符）。
MAX_TRANSCRIPT_CHARS = 2_000_000


def is_compaction_summary(m: Message) -> bool:
    """这条消息是不是 compact_history 生成的压缩摘要。"""
    return m.role == "user" and m.text.lstrip().startswith(SUMMARY_OPEN_TAG)

COMPACT_USER_TEMPLATE = """\
Summarize the conversation segment below for an AI agent that will continue the task \
with no other memory. Capture, in compact markdown:
1. The user's goal(s) and any constraints
2. What has been done so far (files created/modified, commands run, key results)
3. Important file paths, errors encountered and their causes, decisions made
4. What remains to be done

--- CONVERSATION SEGMENT ---
{transcript}
--- END ---"""

# 进入压缩提示词的单条消息上限：触发压缩时历史已经逼近窗口，把超长的
# 工具结果（单条最多 3 万字符）原样再发一遍，压缩请求自己就会先吃 400——
# 最容易失败的时刻恰好最需要它成功。保头去尾：摘要要的是事实与进展，
# 不是逐字全文。
COMPACT_MSG_CHAR_LIMIT = 8_000
COMPACT_MSG_KEEP_TAIL = 1_500

# 压缩摘要调用的瞬态错误重试：限流/超时这类一闪而过的故障不该让整轮对话
# 以 ok=false 收场（主循环同类错误有 3 次退避重试，见 agent.py）。这里给
# 2 次；耗尽（或非瞬态错误）记日志并返回 None——调用方已有的 compaction_stuck
# 路径会自然降级为「本轮不压缩、继续跑」。摘要可从零重生成，不存在主循环
# 「已吐出内容不能重放」的顾虑。
COMPACT_STREAM_RETRIES = 2
COMPACT_RETRY_BASE_DELAY_S = 1.5


def _clip_for_transcript(text: str) -> str:
    if len(text) <= COMPACT_MSG_CHAR_LIMIT:
        return text
    return (
        text[: COMPACT_MSG_CHAR_LIMIT - COMPACT_MSG_KEEP_TAIL]
        + "\n…（本条过长，中间已截断）…\n"
        + text[-COMPACT_MSG_KEEP_TAIL:]
    )


def estimate_text_tokens(text: str) -> int:
    """按字符类别估算 token 数（CJK 与西文分别折算）。"""
    cjk = sum(1 for _ in _CJK_RE.finditer(text))
    other = len(text) - cjk
    return int(cjk * CJK_TOKENS_PER_CHAR + other / OTHER_CHARS_PER_TOKEN)


def estimate_tokens(messages: list[Message]) -> int:
    return estimate_text_tokens("".join(m.to_plain() for m in messages))


def _safe_recent_start(messages: list[Message], keep: int) -> int:
    """返回保留段的起始下标：保证保留段首条不落在 tool_use/tool_result 配对
    中间（协议要求 tool_result 必须紧跟其 assistant tool_use，切在配对中间
    序列化即 400）。

    两类边界都要回退：
    - 保留段首条是 role=="tool"：配对 assistant 在它之前；
    - 保留段首条是 role=="user" 且紧跟着 tool：说明这条 user 插在 tool_result
      中间（旧版本并发批把产图工具的截图 user 消息穿插在工具结果之间；
      agent.py 现已改为整批 tool_result 落齐后追加，但持久化恢复的旧会话
      历史仍是穿插形态），它身后的 tool_result 与身前的同属一个更早的
      assistant(tool_use)，不回退就会留下孤儿 tool_result。

    回退的终点（保留段首条）只有两种合法形态：assistant(tool_use)——它的
    tool_result 全部跟在同段内（回退只发生在 tool / 穿插 user 上，绝不会
    越过 assistant 把保留段切得更靠前）；或普通 user——后面跟 assistant 或
    到头。反向同理：摘要段以 tool_result 结尾时，其配对 assistant 必然也在
    摘要段内。不要对「带 tool_use 的 assistant」回退——长工具链回合的历史
    几乎全是 assistant(tool_use)/tool 交替，无条件回退会一路退到本轮 user，
    导致 to_summarize 为空、整轮压不动（或反复压缩摘要自己）。
    """
    start = max(1, len(messages) - keep)  # 跳过 system（下标 0）

    def _splits_a_pair(idx: int) -> bool:
        if messages[idx].role == "tool":
            return True
        # user 后紧跟 tool：该 user 插在 tool_result 中间（如旧版截图穿插）
        return (
            messages[idx].role == "user"
            and idx + 1 < len(messages)
            and messages[idx + 1].role == "tool"
        )

    while start < len(messages) and _splits_a_pair(start):
        start -= 1
    return max(1, start)


def _ensure_transcript_gitignore(root: Path) -> None:
    """transcripts 目录自带 .gitignore，防 `git add -A` 把会话原文提交进版本库。

    原文可能含用户读过的 .env、密钥、客户数据；用户项目未必忽略 .skysheep/。
    嵌套 .gitignore 只影响这个目录本身，不碰用户自己的 .gitignore，也不依赖
    .git/info/exclude（worktree 下 .git 是文件，未必定位得到）。
    """
    ignore = root / ".gitignore"
    try:
        if ignore.exists():
            text = read_text_file(ignore).text
            if any(line.strip() == "*" for line in text.splitlines()):
                return
            # 已有一份不完全的忽略文件（旧版本/用户手写）：补上通配行，
            # 不覆盖已有内容。目录是引擎自己的产物区，以不泄露为优先。
            write_text_atomic(ignore, text.rstrip("\n") + "\n*\n")
        else:
            write_text_atomic(ignore, "*\n")
    except OSError:
        pass  # 落盘是增强，忽略文件写不出来也不拖垮压缩


def _spill_target(agent, session_id: str) -> tuple[Path, str] | None:
    """transcript 落点：返回 (绝对路径, 给模型看的路径)。

    给模型看的那份优先用相对工作目录的写法（模型的 cwd 就是工作目录，
    read_file 直接可用，也短）；无项目态（工作目录为 None）才给绝对路径。
    目录建不出来就返回 None，压缩照常进行——落盘是增强，不是前置条件。
    """
    base = getattr(agent, "working_dir", None)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^0-9A-Za-z_-]", "", session_id or "")[:24] or "session"
    name = f"{safe}-{stamp}.md"
    try:
        if base is not None:
            root = Path(base) / ".skysheep" / TRANSCRIPT_DIR
            shown = f".skysheep/{TRANSCRIPT_DIR}/{name}"
        else:
            root = Path.home() / ".skysheep" / TRANSCRIPT_DIR
            shown = str(root / name)
        root.mkdir(parents=True, exist_ok=True)
        _ensure_transcript_gitignore(root)
        return root / name, shown
    except OSError as e:  # noqa: BLE001 - 磁盘/权限问题不该拖垮压缩
        logger.warning("compact_history: 无法创建 transcript 目录（%s），本次不落盘", e)
        return None


def spill_transcript(agent, messages: list[Message], session_id: str = "") -> str:
    """把即将被摘要替换的原文写到盘上，返回给模型看的路径（失败返回空串）。

    只做「保存 + 报路径」两件事，不做索引、不做切片：模型要哪段自己用
    read_file / grep 找。这样压缩是无损的（对能读盘的模型而言），窗口里
    只留摘要 + 一个指针。
    """
    target = _spill_target(agent, session_id)
    if target is None:
        return ""
    path, shown = target
    parts: list[str] = []
    used = 0
    for m in messages:
        try:
            body = m.to_plain()
        except Exception:  # noqa: BLE001 - 单条坏消息不该毁掉整份 transcript
            body = str(getattr(m, "text", ""))
        chunk = f"[{m.role.upper()}]\n{body}\n\n"
        # 最后一条可能只剩一半：宁可少存也不写出一个看似完整的截断稿
        if used + len(chunk) > MAX_TRANSCRIPT_CHARS:
            parts.append(f"（超出 {MAX_TRANSCRIPT_CHARS} 字符上限，以下内容省略）\n")
            break
        parts.append(chunk)
        used += len(chunk)
    header = (
        f"# 会话原文（压缩前）\n\n"
        f"会话：{session_id or '（未知）'}\n"
        f"落盘：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"消息数：{len(messages)}\n\n---\n\n"
    )
    try:
        write_text_atomic(path, header + "".join(parts))
    except OSError as e:  # noqa: BLE001
        logger.warning("compact_history: 原文落盘失败（%s），本次只保留摘要", e)
        return ""
    return shown


async def compact_history(
    agent,  # Agent，避免循环导入用鸭子类型
    keep_recent: int = 8,
    *,
    budget: RunBudget | None = None,
) -> CompactionEvent | None:
    """压缩 agent.history；没有可压缩内容时返回 None。

    ``budget`` 非空时，摘要调用也计入模板预算（每次真实 provider 调用前
    spend、返回的 usage 累计回填）。压缩属于模板运行内的真实模型调用，
    不应绕过 max_calls / max_tokens（审查 finding 17）。
    """
    history = agent.history
    if len(history) <= keep_recent + 1:
        return None

    start = _safe_recent_start(history, keep_recent)
    to_summarize = history[1:start]
    if not to_summarize:
        return None
    recent = history[start:]

    transcript = "\n\n".join(
        f"[{m.role.upper()}] {_clip_for_transcript(m.to_plain())}" for m in to_summarize
    )
    summary_parts: list[str] = []
    usage_total = 0
    # 摘要调用与主循环流式一样可能碰上瞬态 429/超时/断流：异常隔离 + 有限
    # 退避重试，耗尽后放弃压缩（返回 None）而不是把异常炸回 run_turn——
    # 那会让整轮对话以 ok=false 终止且不重试。CancelledError 不是 Exception，
    # 用户取消照常穿透。
    for attempt in range(1, COMPACT_STREAM_RETRIES + 2):
        summary_parts = []
        try:
            if budget is not None:
                budget.spend()
            async for pe in agent.provider.stream(
                [
                    Message.system(COMPACT_SYSTEM),
                    Message.user(COMPACT_USER_TEMPLATE.format(transcript=transcript)),
                ],
                [],
            ):
                # 只收正文增量：思考型模型的 reasoning 增量同样带 text 字段，混进摘要
                # 既浪费注入预算又污染事实（与 backend 归档提炼同一过滤口径）
                if isinstance(pe, ProviderTextDelta):
                    summary_parts.append(pe.text)
                elif isinstance(pe, ProviderDone):
                    usage_total += pe.input_tokens + pe.output_tokens
            if budget is not None and usage_total:
                # 增量累加：摘要调用与后续成员/阶段调用各自只知道自己这段
                # 的用量，不能用 add_tokens 的累计值语义互相覆盖。
                budget.add_tokens_delta(usage_total)
            break  # 摘要调用正常完成
        except BudgetExceeded:
            # 预算耗尽不是「压缩失败降级」，必须原样上抛让协作链路收尾
            raise
        except Exception as e:  # noqa: BLE001 - 摘要失败降级为不压缩，不炸整轮
            if attempt > COMPACT_STREAM_RETRIES or not is_transient_error(e):
                logger.warning(
                    "compact_history: 摘要生成失败（%s: %.160s），本轮放弃压缩",
                    type(e).__name__, e,
                )
                return None
            delay = min(COMPACT_RETRY_BASE_DELAY_S * (2 ** (attempt - 1)), 10.0)
            logger.warning(
                "compact_history: 摘要生成失败（%s: %.160s），%.0f 秒后重试（第 %d/%d 次）",
                type(e).__name__, e, delay, attempt, COMPACT_STREAM_RETRIES,
            )
            await asyncio.sleep(delay)
    summary = "".join(summary_parts).strip()
    if not summary:
        return None

    # 摘要成功之后才落盘：摘要失败直接返回 None，此时落一份没人引用的原文
    # 只会在 .skysheep/transcripts 里堆垃圾。
    pointer = spill_transcript(agent, list(to_summarize), getattr(agent, "session_id", ""))
    note = SUMMARY_NOTE
    if pointer:
        note += (
            f"完整原文已落盘：{pointer}"
            "（read_file / grep 可续读；只想找某个关键词时优先用 search_history）。"
        )
    summary_msg = Message.user(
        f"{SUMMARY_OPEN_TAG}\n{summary}\n{SUMMARY_CLOSE_TAG}\n{note}"
    )
    agent.history = [history[0], summary_msg] + list(recent)
    return CompactionEvent(
        before_messages=len(history),
        after_messages=len(agent.history),
        summary_chars=len(summary),
    )
