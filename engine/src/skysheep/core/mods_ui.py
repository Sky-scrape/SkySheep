"""Mods 元素树（P3）：Claude Code Mods 风格的声明式 UI 树校验。

Mod 产出的是**数据**不是 HTML：前端用固定渲染器逐节点落地，所有文本走
textContent，永不拼进 innerHTML。本模块负责把第三方数据收敛到安全白名单：

- 节点类型闭集：box / text / button / input / select / image
- 深度 ≤ MAX_DEPTH、每层 children ≤ MAX_CHILDREN、总节点 ≤ MAX_NODES
- 所有文本限长并压空白
- id（button/input/select）唯一、限长、只允许 [A-Za-z0-9_-]
- image.src 只允许 data:image/* 或 http(s)://——挡住 javascript: / vbscript: 等
- 样式只允许有限的枚举值（tone / direction / style），不接受任意 CSS

校验失败一律**丢弃该节点**（fail-closed），不抛错阻断整轮。
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

MAX_DEPTH = 8
MAX_CHILDREN = 50
MAX_NODES = 200
MAX_TEXT_CHARS = 500
MAX_ID_CHARS = 64
MAX_OPTIONS = 30
MAX_IMAGE_BYTES = 2 * 1024 * 1024  # data URI 体积上限（约 1.5MB 原始图）

NODE_TYPES = frozenset({"box", "text", "button", "input", "select", "image"})
TONES = frozenset({"normal", "dim", "ok", "warn", "danger"})
DIRECTIONS = frozenset({"row", "column"})
BUTTON_STYLES = frozenset({"normal", "primary", "danger"})

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DATA_IMAGE_RE = re.compile(r"^data:image/(png|jpeg|webp|gif);base64,", re.IGNORECASE)


def _clean_text(value: object, limit: int = MAX_TEXT_CHARS) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def _clean_id(value: object) -> str:
    raw = str(value or "").strip()
    return raw if _ID_RE.match(raw) else ""


def _clean_image_src(value: object) -> str:
    src = str(value or "").strip()
    if not src:
        return ""
    if _DATA_IMAGE_RE.match(src):
        return src if len(src) <= MAX_IMAGE_BYTES else ""
    try:
        parsed = urlparse(src)
    except ValueError:
        return ""
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return src
    return ""


def _normalize_node(node: object, *, depth: int, seen_ids: set[str],
                    counter: list[int]) -> dict | None:
    """递归校验一个节点；不合法返回 None（整棵子树丢弃）。"""
    if depth > MAX_DEPTH:
        return None
    if counter[0] >= MAX_NODES:
        return None
    if not isinstance(node, dict):
        return None
    ntype = str(node.get("type") or "").strip()
    if ntype not in NODE_TYPES:
        return None
    counter[0] += 1

    if ntype == "box":
        direction = str(node.get("direction") or "column").strip()
        if direction not in DIRECTIONS:
            direction = "column"
        children_raw = node.get("children")
        children: list[dict] = []
        if isinstance(children_raw, list):
            for child in children_raw[:MAX_CHILDREN]:
                normalized = _normalize_node(
                    child, depth=depth + 1, seen_ids=seen_ids, counter=counter,
                )
                if normalized is not None:
                    children.append(normalized)
        return {"type": "box", "direction": direction, "children": children}

    if ntype == "text":
        tone = str(node.get("tone") or "normal").strip()
        if tone not in TONES:
            tone = "normal"
        return {"type": "text", "text": _clean_text(node.get("text")), "tone": tone}

    if ntype == "button":
        node_id = _clean_id(node.get("id"))
        if not node_id or node_id in seen_ids:
            return None
        seen_ids.add(node_id)
        style = str(node.get("style") or "normal").strip()
        if style not in BUTTON_STYLES:
            style = "normal"
        return {
            "type": "button", "id": node_id,
            "label": _clean_text(node.get("label"), 80), "style": style,
        }

    if ntype == "input":
        node_id = _clean_id(node.get("id"))
        if not node_id or node_id in seen_ids:
            return None
        seen_ids.add(node_id)
        return {
            "type": "input", "id": node_id,
            "value": _clean_text(node.get("value"), 200),
            "placeholder": _clean_text(node.get("placeholder"), 120),
        }

    if ntype == "select":
        node_id = _clean_id(node.get("id"))
        if not node_id or node_id in seen_ids:
            return None
        seen_ids.add(node_id)
        options: list[dict] = []
        raw_options = node.get("options")
        if isinstance(raw_options, list):
            for opt in raw_options[:MAX_OPTIONS]:
                if not isinstance(opt, dict):
                    continue
                value = _clean_text(opt.get("value"), 120)
                if not value:
                    continue
                options.append({"value": value, "label": _clean_text(opt.get("label"), 80)})
        if not options:
            return None
        return {
            "type": "select", "id": node_id,
            "value": _clean_text(node.get("value"), 120), "options": options,
        }

    # image
    src = _clean_image_src(node.get("src"))
    if not src:
        return None
    try:
        width = max(0, min(2000, int(node.get("width") or 0)))
        height = max(0, min(2000, int(node.get("height") or 0)))
    except (TypeError, ValueError):
        width = height = 0
    out: dict[str, Any] = {"type": "image", "src": src, "alt": _clean_text(node.get("alt"), 120)}
    if width:
        out["width"] = width
    if height:
        out["height"] = height
    return out


def normalize_ui_tree(raw: object) -> dict | None:
    """校验并归一 Mod 产出的元素树；整棵不合法返回 None。"""
    root = _normalize_node(raw, depth=0, seen_ids=set(), counter=[0])
    if root is None:
        return None
    return root
