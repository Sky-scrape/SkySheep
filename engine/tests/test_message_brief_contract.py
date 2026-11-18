"""``_msg_brief`` 字段契约：Message 新增字段必须随历史下发，不能靠手抄。

背景（2026-11-18 返工复盘）：``_msg_brief`` 是「消息落库 / 回传前端」的唯一
轻量序列化口，但它逐字段手写。过去每加一个前端要用的字段就要改两处
（Message + _msg_brief），漏了不会报错，只在刷新/重开会话后静默丢功能：

- ``adversarial`` 漏发 → 对抗审查卡刷新即消失（2026-10-21，cb72dbd）；
- ``sources`` / ``duration_ms`` / ``estimate`` 漏发 → 来源 chip、用时芯片
  历史恢复不出来；
- ``system_note`` 漏发 → 撤销补记被当成用户消息，误点「编辑重发」会截断会话。

这条契约把「Message 的每个公开字段都要有去向」变成会变红的测试：新增字段时
要么在 ``_msg_brief`` 里加一格，要么在 ``_DERIVED`` 里写清为什么由别的键派生。
"""

from __future__ import annotations

from skysheep.messages import Message
from skysheep.server.backend import _msg_brief

# 不直接出现在 brief 里的 Message 字段：
# - content：由 text / images / thinking 三个派生键表达（图片只占位，不带 base64）
# - id / created_at：库内主键与排序用，前端历史渲染不需要
_DERIVED = {"content", "id", "created_at"}

# brief 里不是 Message 字段、而是派生出来的键
_BRIEF_DERIVED = {"text", "images", "thinking", "thinking_ms"}


def test_every_message_field_has_a_brief_slot_or_explicit_derivation():
    message = Message.user("你好")
    brief = _msg_brief(message)

    missing = set(Message.model_fields) - set(brief) - _DERIVED
    assert not missing, (
        "Message 新增了字段但 _msg_brief 没下发（刷新/重开会话后前端会静默丢功能）："
        f"{sorted(missing)}；若该字段不需要前端，加入本测试的 _DERIVED 并写清理由"
    )

    unknown = set(brief) - set(Message.model_fields) - _BRIEF_DERIVED
    assert not unknown, (
        f"_msg_brief 出现了 Message 上不存在的键（拼写错误或字段已删）：{sorted(unknown)}"
    )


def test_brief_preserves_non_default_metadata():
    """字段存在还不够，值也要原样带过去（避免只加键、忘了赋值）。"""
    message = Message.user("你好")
    message.seq = 7
    message.roundtable = {"mode": "review"}
    message.adversarial = {"verdicts": []}
    message.duration_ms = 1234
    message.estimate = {"low_s": 10, "high_s": 20}
    message.sources = [{"index": 1, "url": "https://example.com"}]
    message.system_note = True

    brief = _msg_brief(message)

    assert brief["seq"] == 7
    assert brief["roundtable"] == {"mode": "review"}
    assert brief["adversarial"] == {"verdicts": []}
    assert brief["duration_ms"] == 1234
    assert brief["estimate"] == {"low_s": 10, "high_s": 20}
    assert brief["sources"] == [{"index": 1, "url": "https://example.com"}]
    assert brief["system_note"] is True
    assert brief["text"] == "你好"
