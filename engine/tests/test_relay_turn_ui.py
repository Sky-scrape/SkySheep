"""接力轮次卡的呈现：一行概括 + 可展开的详细内容（前端源码锚定）。"""

from conftest import read_app_bundle


def test_relay_turn_card_renders_summary_and_collapsible_detail():
    """轮次卡不再把整段原始 Markdown 截 120 字塞进一行。

    概括走 relayPlainSummary（去标题井号/强调/表格边线、取第一句），详细内容
    收进 <details>，完整产出按 Markdown 渲染，展开状态按轮次号保留。
    """
    js = read_app_bundle()
    assert "function relayPlainSummary(" in js
    assert "function relayDualTurnSummary(" in js
    assert "relay-turn-summary" in js
    assert "relay-turn-detail" in js
    assert "relay-turn-full" in js
    assert "详细内容（" in js
    assert "relayDualTurnPreview" not in js, "旧的整段截断预览不该再存在"
