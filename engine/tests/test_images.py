"""图片输入（多模态）：消息模型、Provider 序列化、WS 端到端持久化。"""

from __future__ import annotations

import base64

from skysheep.messages import ImageBlock, Message
from skysheep.models.anthropic_provider import to_anthropic_messages
from skysheep.models.openai_compat import to_openai_messages

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode()


def test_message_user_with_images():
    m = Message.user("看这张图", images=[ImageBlock(media_type="image/png", data=PNG_B64)])
    assert m.text == "看这张图"
    assert isinstance(m.content[0], type(Message.user("").content[0]))  # TextBlock
    img = m.content[1]
    assert img.type == "image" and img.media_type == "image/png"
    assert "[image image/png" in m.to_plain()


def test_message_user_image_only():
    m = Message.user("", images=[ImageBlock(media_type="image/jpeg", data="AA=")])
    assert m.text == ""
    assert any(b.type == "image" for b in m.content)


def test_openai_multimodal_payload():
    m = Message.user("看图", images=[ImageBlock(media_type="image/png", data=PNG_B64)])
    out = to_openai_messages([m])
    assert out[0]["role"] == "user"
    parts = out[0]["content"]
    assert parts[0] == {"type": "text", "text": "看图"}
    assert parts[1]["type"] == "image_url"
    assert parts[1]["image_url"]["url"] == f"data:image/png;base64,{PNG_B64}"
    # 纯文本消息保持字符串 content（老格式，兼容面不变）
    plain = to_openai_messages([Message.user("hi")])
    assert plain[0]["content"] == "hi"


def test_anthropic_multimodal_payload():
    m = Message.user("看图", images=[ImageBlock(media_type="image/png", data=PNG_B64)])
    out = to_anthropic_messages([m])
    blocks = out[0]["content"]
    assert blocks[0] == {"type": "text", "text": "看图"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["source"]["media_type"] == "image/png"
    assert blocks[1]["source"]["data"] == PNG_B64


def test_chat_send_with_image_persisted(home):
    """WS 端到端：图片随消息落库；非法类型被静默丢弃；纯图片消息也能发。"""
    from test_server import make_client, recv_until

    from skysheep.messages import TextBlock
    from skysheep.models.fake import FakeProvider

    provider = FakeProvider([[TextBlock(text="看到了，图里是…")], [TextBlock(text="收到")]])

    with make_client(home, [], provider=provider) as client, \
            client.websocket_connect("/ws") as ws:
        ws.send_json({
            "id": "c1", "method": "chat.send",
            "params": {
                "text": "看这张图",
                "images": [
                    {"media_type": "image/png", "data": PNG_B64},
                    {"media_type": "image/bmp", "data": "bm9wZQ=="},  # 非法类型 → 丢弃
                ],
            },
        })
        r = recv_until(ws, "c1")
        assert r["ok"], r.get("error")
        sid = r["result"]["session_id"]

        ws.send_json({"id": "e1", "method": "session.export", "params": {"id": sid}})
        md = recv_until(ws, "e1")["result"]["markdown"]
        assert "[image image/png" in md
        assert "image/bmp" not in md

        # 纯图片消息（无文字）也能发，标题兜底为 [图片]
        ws.send_json({
            "id": "c2", "method": "chat.send",
            "params": {"text": "", "images": [{"media_type": "image/png", "data": PNG_B64}]},
        })
        r2 = recv_until(ws, "c2")
        assert r2["ok"], r2.get("error")
        ws.send_json({"id": "sl", "method": "session.list", "params": {}})
        lst = recv_until(ws, "sl")["result"]
        assert lst["sessions"][0]["title"] == "看这张图"
