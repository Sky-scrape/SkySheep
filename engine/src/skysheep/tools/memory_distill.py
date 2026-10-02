"""轮次自动沉淀（记忆二期）：会话轮次收尾后抽「值得长期记住的事实/偏好」候选。

与归档提炼（tools/memory.py 的 digest_* / remember_lines）的分工：归档提炼
直接写入 memory.md（归档 = 「这事完了」的自然时机，已有产品语义）；本模块是
候选制——每轮收尾后用当前模型抽出候选，与 memory.md 既有条目去重后进
「待审列表」（~/.skysheep/memory-distill.json，引擎自有状态文件，原子写），
由用户在记忆页采纳/忽略后才动 memory.md。候选制是为了「不打扰」：不写记忆、
不刷系统提示词、不广播事件，开关默认关。

模型调用与轮次挂点在 server/backend_parts/memory.py（MemoryMixin 的
schedule_turn_distill / _turn_distill，与 _schedule_memory_digest 同一套
spawn_bg + 失败静默边界）；本模块只放纯函数与状态文件读写。

采纳/忽略（adopt_candidate / ignore_candidate）是接线阶段包成 WS 方法的
引擎层函数：采纳走 remember_lines 追加（去重、日期前缀、(自动) 标记、
容量护栏全部复用），忽略记入已忽略名单——否则同一事实下一轮又会被提回来，
「忽略」就成了永远点不完的按钮。
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from ..textio import write_text_atomic
from .memory import (
    memory_path,
    parse_digest,
    remember_lines,
    split_memory_entries,
)

DISTILL_STATE_FILE = "memory-distill.json"  # ~/.skysheep/ 下的待审列表 + 开关
DISTILL_MAX_CANDIDATES = 3  # 每轮最多抽 3 条候选（多了就不是「值得长期记住」而是流水账）
CANDIDATE_MAX_CHARS = 100   # 单条候选长度上限（与归档提炼的 DIGEST_ENTRY_MAX_CHARS 一致）
PENDING_MAX = 50            # 待审列表封顶：超出丢最旧（用户长期不处理不该无限膨胀）
IGNORED_MAX = 200           # 已忽略名单封顶：防「忽略后又回来」的比对表无限增长
CONTEXT_EXCERPT_CHARS = 200  # 待审条目带的上下文摘录长度（用户判断「记不记」的依据）


def distill_state_path() -> Path:
    from ..config import skysheep_home

    return skysheep_home() / DISTILL_STATE_FILE


def _empty_state() -> dict:
    return {"enabled": False, "pending": [], "ignored": []}


def load_distill_state() -> dict:
    """读待审列表与开关；文件缺失/损坏按出厂态处理（开关默认关，不抛错）。"""
    try:
        data = json.loads(distill_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    return {
        "enabled": bool(data.get("enabled")),
        "pending": [e for e in (data.get("pending") or []) if isinstance(e, dict)],
        "ignored": [str(t) for t in (data.get("ignored") or []) if t],
    }


def save_distill_state(state: dict) -> None:
    # 原子写（textio 同族）：待审列表是唯一事实源，写一半被杀不能留下半截 JSON
    write_text_atomic(distill_state_path(), json.dumps(state, ensure_ascii=False))


def distill_enabled() -> bool:
    return load_distill_state()["enabled"]


def set_distill_enabled(enabled: bool) -> bool:
    """沉淀总闸（记忆页开关，接线阶段包成 WS 方法）：默认关，写状态文件热生效。"""
    state = load_distill_state()
    state["enabled"] = bool(enabled)
    save_distill_state(state)
    return state["enabled"]


def list_candidates() -> list[dict]:
    """待审列表（记忆页候选区数据源，接线阶段包成 WS 方法）。"""
    return load_distill_state()["pending"]


def build_distill_prompt(transcript: str) -> str:
    """候选抽取的提示词：结构化中文、限 3 条、宁缺毋滥、勿记任务细节与机密。"""
    return (
        "下面是一轮刚结束的对话记录。请从里面挑出「值得为这位用户长期记住」的信息候选，"
        "例如：用户明确的偏好与习惯、常用的工具链与目录位置、长期有效的背景事实。\n"
        "要求：\n"
        f"- 每条一行、以「- 」开头，最多 {DISTILL_MAX_CANDIDATES} 条；"
        "这一轮没有值得记的就只输出：无\n"
        "- 这是候选清单而不是最终记忆：宁缺毋滥，拿不准的不要列\n"
        f"- 每条不超过 {CANDIDATE_MAX_CHARS} 字，具体、可长期有效、可独立理解\n"
        "- 一次性的任务细节（改了哪个文件、报了什么错）不要列\n"
        "- 密码、API Key 等机密信息绝对不要列\n\n"
        "对话记录：\n" + transcript
    )


def parse_distill_output(raw: str) -> list[str]:
    """解析模型输出的候选清单：复用归档提炼的 parse_digest（剥列表符号/编号、
    滤占位行与前导语、批内去重），再按二期上限截到 3 条。"""
    return parse_digest(raw)[:DISTILL_MAX_CANDIDATES]


_WS_RE = re.compile(r"\s+")


def _is_dup(text: str, existing: list[str]) -> bool:
    """候选与既有内容是否重复：剥空白后互为子串即算（与 remember_lines 的
    「content in ln」同一族，只是双向比对——候选往往比记忆条目更精炼）。"""
    t = _WS_RE.sub("", text)
    if not t:
        return True
    for e in existing:
        n = _WS_RE.sub("", e)
        if n and (t in n or n in t):
            return True
    return False


def filter_new_candidates(
    cands: list[str], memory_text: str, state: dict | None = None,
) -> list[str]:
    """三方去重：memory.md 既有条目（一期 split_memory_entries 切条）、待审列表、
    已忽略名单。返回真正的新候选（保持原顺序、批内去重）。"""
    state = state or _empty_state()
    known = split_memory_entries(memory_text or "")
    known += [str(e.get("text") or "") for e in state.get("pending") or []]
    known += [str(t) for t in state.get("ignored") or []]
    out: list[str] = []
    for c in cands:
        c = (c or "").strip()
        if not c or _is_dup(c, known):
            continue
        if c not in out:
            out.append(c)
    return out


def _read_memory_text() -> str:
    # 与 tools/memory.py 同一读法：memory.md 是引擎自产文件，恒以 UTF-8 落盘
    try:
        return memory_path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def add_candidates(
    cands: list[str], *, context: str = "", session_id: str = "",
    memory_text: str | None = None,
) -> list[dict]:
    """去重后把新候选写进待审列表（原子写），返回新增的条目。

    每条带 id（采纳/忽略的句柄）、上下文摘录、时间与会话归属；并发调用方
    （backend 的轮次沉淀服务）应持 _memory_io_lock——本函数读 memory.md 做
    去重比对，与采纳路径的 memory.md 追加共用一把锁才不产生竞态窗口。
    """
    state = load_distill_state()
    if memory_text is None:
        memory_text = _read_memory_text()
    fresh = filter_new_candidates(cands, memory_text, state)
    if not fresh:
        return []
    now = time.time()
    added: list[dict] = []
    for text in fresh:
        ent = {
            "id": uuid.uuid4().hex[:12],
            "text": text[:CANDIDATE_MAX_CHARS],
            "context": (context or "")[:CONTEXT_EXCERPT_CHARS],
            "session_id": session_id or "",
            "ts": now,
        }
        state["pending"].append(ent)
        added.append(ent)
    state["pending"] = state["pending"][-PENDING_MAX:]  # 封顶丢最旧
    save_distill_state(state)
    return added


def _pop_by_id(pending: list[dict], cid: str) -> tuple[dict | None, list[dict]]:
    for i, ent in enumerate(pending):
        if str(ent.get("id") or "") == cid:
            return ent, pending[:i] + pending[i + 1:]
    return None, pending


def adopt_candidate(cid: str) -> dict:
    """采纳：候选从待审列表移入 memory.md（remember_lines 追加：去重、日期前缀、
    「(自动)」标记、容量护栏全部复用一期落盘路径）。

    并发注记：本函数写 memory.md，异步调用方（backend 的 WS 包装）应持
    _memory_io_lock。返回 {"adopted": bool, ...}；候选不存在或记忆里已有
    相同内容时 adopted=False（待审条目同样移除——已处理过，不该反复出现）。
    """
    state = load_distill_state()
    ent, rest = _pop_by_id(state["pending"], cid)
    if ent is None:
        return {"adopted": False, "reason": "候选不存在或已被处理"}
    added = remember_lines([str(ent.get("text") or "")])
    state["pending"] = rest
    save_distill_state(state)
    if not added:
        return {"adopted": False, "reason": "记忆里已有相同内容", "entry": ent}
    return {"adopted": True, "added": added, "entry": ent}


def ignore_candidate(cid: str) -> dict:
    """忽略：候选直接丢弃，文本记入已忽略名单（封顶滚动）——下一轮同一事实
    不会再进待审列表，「忽略」才是一次性的动作。"""
    state = load_distill_state()
    ent, rest = _pop_by_id(state["pending"], cid)
    if ent is None:
        return {"ignored": False, "reason": "候选不存在或已被处理"}
    state["pending"] = rest
    ignored = [str(t) for t in state.get("ignored") or []]
    text = str(ent.get("text") or "")
    if text not in ignored:
        ignored.append(text)
    state["ignored"] = ignored[-IGNORED_MAX:]
    save_distill_state(state)
    return {"ignored": True, "entry": ent}
