"""渠道富卡片（P21）：飞书 interactive 卡的纯函数装配与文本降级。

只做「任务完成 / 权限请求」两类。安全口径：**权限卡绝不内嵌放行/拒绝按钮**
——只带说明与「打开应用处理」跳转（skysheep://focus 协议），操作永远回到
桌面端经 PermissionGate 确认。微信渠道不支持卡片：BaseChannel.send_card
的默认实现把卡片降级成纯文本走 send_text，调用方无感知。

卡片形状用飞书卡片 v1（config/header/elements 顶层）：兼容面最宽，
lark-cli 的 interactive 消息原样透传这份 JSON。
"""

from __future__ import annotations

import json

# 权限卡的跳转链接：桌面壳注册的 skysheep://<scheme>（通知点击唤回同一协议）
FOCUS_LINK_NOTE = "为防误触，卡片不带任何操作按钮；请回到应用里确认。"


def _md(text: str) -> dict:
    return {"tag": "markdown", "content": text}


def task_finish_card(*, name: str, lines: list[str], ok: bool = True) -> dict:
    """任务完成卡：标题栏按成败配色（绿/橙），正文为 markdown 行列表。"""
    body = [ln for ln in lines if str(ln).strip()] or ["（无详情）"]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": ("✅ " if ok else "⚠️ ") + name},
            "template": "green" if ok else "orange",
        },
        "elements": [_md("\n".join(body)), {"tag": "hr"}],
    }


def permission_request_card(*, title: str, detail: str, link: str | None = None) -> dict:
    """权限请求卡：说明 + 详情 +「打开应用处理」跳转。

    ``link`` 传 skysheep://focus（桌面壳注册的唤回协议）；为 None（如浏览器
    兜底模式没注册协议）时省略按钮，只留说明。
    """
    elements = [_md("**" + title + "**"), _md(detail)]
    if link:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": "打开 SkySheep 处理"},
                "type": "primary",
                "url": link,
            }],
        })
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": FOCUS_LINK_NOTE}],
    })
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "🔐 权限请求"},
                   "template": "yellow"},
        "elements": elements,
    }


def card_to_text(card: dict) -> str:
    """卡片降级纯文本：标题 + 逐元素里的 markdown/plain_text 拼行（降级共用）。"""
    lines: list[str] = []
    header = card.get("header") or {}
    title = ((header.get("title") or {}).get("content") or "").strip()
    if title:
        lines.append(title)
    for el in card.get("elements") or []:
        tag = el.get("tag")
        if tag == "markdown":
            lines.append(str(el.get("content") or ""))
        elif tag == "note":
            for sub in el.get("elements") or []:
                if sub.get("tag") == "plain_text":
                    lines.append("> " + str(sub.get("content") or ""))
        elif tag == "action":
            for act in el.get("actions") or []:
                lines.append(f"[{act.get('text', {}).get('content', '按钮')}] {act.get('url', '')}")
    return "\n".join(ln for ln in lines if ln.strip())


def card_to_json(card: dict) -> str:
    """卡片转 lark-cli --content 参数：紧凑 JSON，中文原样（消息体不转义更可读）。"""
    return json.dumps(card, ensure_ascii=False, separators=(",", ":"))
