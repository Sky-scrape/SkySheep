"""验收工位：只读判定、解析容错、失败不反噬运行。

契约见 `core/review.py` 与 `docs/工厂设计.md` §17.11：
- 只给任务要求 + 产出，不挂工具、不联网（模型调用本身除外）；
- 解析兼容纯 JSON / ```json 围栏 / JSON 前后带解释；
- 超时、上游报错、空输出只记备注，保留自动判定；
- token 随结果返回，由调用方补记 usage_log。
"""

from __future__ import annotations

from skysheep.core.review import parse_review_verdict, review_output
from skysheep.models.fake import FakeProvider


def test_parse_review_verdict_accepts_common_forms():
    plain = parse_review_verdict('{"verdict":"failed","reason":"没有结论"}')
    assert plain == {"outcome": "failed", "note": "没有结论"}

    fenced = parse_review_verdict(
        '```json\n{"verdict":"ok","reason":"覆盖要求"}\n```')
    assert fenced["outcome"] == "ok" and fenced["note"] == "覆盖要求"

    prose = parse_review_verdict(
        '我判断如下：{"verdict":"partial","reason":"缺一项"} 供参考')
    assert prose["outcome"] == "partial" and prose["note"] == "缺一项"


def test_parse_review_verdict_rejects_unusable_output():
    for text in ("", "我看了，还行", '{"verdict":"weird","reason":"x"}', "[]"):
        got = parse_review_verdict(text)
        assert got["outcome"] == "", f"无法识别的输出必须保留自动判定：{text!r}"
        assert got["note"]


async def test_review_output_reads_verdict_and_tokens():
    provider = FakeProvider([
        '{"verdict":"failed","reason":"只有过程，没有结论"}',
    ])
    got = await review_output(
        provider, task_name="日报", task_prompt="汇总今天的进展", result="我看了日志")
    assert got["outcome"] == "failed"
    assert "没有结论" in got["note"]
    assert got["in_tokens"] == 11 and got["out_tokens"] == 7, \
        "评审 token 要带回来，调用方补记 usage_log"
    # 任务要求与产出都进了 user 消息；system 里写明只作验收材料
    assert provider.calls, "验收必须真的发起一次模型调用"
    system_text = provider.calls[0][0].text
    user_text = provider.calls[0][-1].text
    assert "验收员" in system_text and "不要执行" in system_text
    assert "汇总今天的进展" in user_text and "我看了日志" in user_text


async def test_review_output_never_raises_on_provider_error():
    class _Boom:
        async def stream(self, messages, tools, effort=None):
            raise RuntimeError("上游炸了")
            yield  # pragma: no cover - 只为让它成为异步生成器

    got = await review_output(_Boom(), task_name="x", task_prompt="y", result="z")
    assert got["outcome"] == ""
    assert "验收调用失败" in got["note"]
    assert got["in_tokens"] == 0 and got["out_tokens"] == 0
