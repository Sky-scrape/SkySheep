"""评测基线 · 命令白名单 exact/prefix 边界矩阵场景（真实循环沉淀 + 真实门匹配）。

两个种子场景，是场景 4/7（docker exact、拼接拦截）之外的边界补充：

26. exact 边界：带 shell 拼接的命令「总是允许」沉淀的是 exact——它只放行
    「当初那一条」：原样复跑免确认；加长尾巴、以及作为它前缀的干净命令，
    都要重新确认（exact 不是 prefix，也不被更短的前缀 sibling 命中）；
27. prefix 边界矩阵：沉淀出的两词前缀按**完整词**命中——statusx 不命中；
    带解释器代码旗标（-c）的命令不命中；带拼接元字符的不命中且解释原因；
    空 pattern 的前缀规则匹配不到任何命令（历史脏数据 fail-closed）；
    停用的规则不参与匹配、但测试器单独提示（与「白名单坏了」区分）。

沉淀走真实循环的「确认卡 → allow_always」（被允许的第一条命令会真实执行，
选的都是本地只读、秒退、无副作用的 git 命令）；矩阵走真实权限门的
explain()——它与 authorize 同一套匹配逻辑（含 shell 拼接拦截）。
"""

from __future__ import annotations

from evals._harness import make_agent, requests, run_eval_turn
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import PermissionGate, WhitelistRule

_CHAIN = "git status & echo done"


def _decide_always_first(target: str):
    def decide(ev):
        return "allow_always" if ev.input.get("command") == target else "deny"
    return decide


async def test_eval_exact_rule_from_chained_command_is_narrow(home):
    """场景 26（白名单 exact 边界）：拼接命令沉淀的 exact 只放行当条。

    「含拼接的命令没有安全前缀可提炼，改成 exact 只放行用户当时批准的
    这一条」（security/gate.py rule_for）。真实循环钉住的行为链：
    - 首次执行弹确认，预告规则是 exact「git status & echo done」；
    - 原样复跑免确认（exact 命中自身）；
    - 加长尾巴的同前缀命令要重新确认（exact 不覆盖扩展）；
    - 干净的 `git status`（exact 规则的字符串前缀）也要重新确认——
      exact 不是 prefix，短命令不能蹭长规则。
    """
    proj = home / "proj"
    provider = FakeProvider([
        [ToolUseBlock(id="c1", name="run_command", input={"command": _CHAIN})],
        [ToolUseBlock(id="c2", name="run_command", input={"command": _CHAIN})],
        [ToolUseBlock(id="c3", name="run_command",
                      input={"command": _CHAIN + " & echo more"})],
        [ToolUseBlock(id="c4", name="run_command", input={"command": "git status"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, proj)
    events = await run_eval_turn(agent, "看看仓库状态", _decide_always_first(_CHAIN))

    reqs = requests(events)
    assert [e.input.get("command") for e in reqs] == [_CHAIN, _CHAIN + " & echo more", "git status"], (
        "原样复跑免确认；扩展与更短前缀命令都要重新确认"
    )
    assert (reqs[0].rule_kind, reqs[0].rule_pattern) == ("exact", _CHAIN), (
        "确认弹窗预告的规则就是沉淀的：exact 当条"
    )
    gate = agent.gate
    hit = gate.explain("run_command", _CHAIN)
    assert hit["allowed"] is True and hit["hit"]["kind"] == "exact"
    ext = gate.explain("run_command", _CHAIN + " & echo more")
    assert ext["allowed"] is False and ext["hit"] is None
    short = gate.explain("run_command", "git status")
    assert short["allowed"] is False and short["hit"] is None, (
        "exact 规则不等于同前缀放行：短命令不命中"
    )


async def test_eval_prefix_rule_boundary_matrix(home):
    """场景 27（白名单 prefix 边界矩阵）：完整词、代码旗标、拼接、脏数据、停用。

    真实循环沉淀 `git status` 两词前缀后（该命令真实执行，本地只读）：
    - `git status --short` 免确认（完整词前缀命中，复跑也是免确认路径）；
    - `git statusx` 不命中——前缀必须断在词边界（场景 4/7 之外的边界）；
    - `git status -c` 不命中——`-c` 是「下一参数即任意代码」的解释器旗标，
      命令类前缀规则不放行它（审查 B-1）；
    - `git status & echo x` 不命中且 explain 给出「拼接」原因（与 authorize
      同一套判定，测试结果就是实际行为）；
    - 空 pattern 的前缀规则匹配不到任何命令（历史脏数据 fail-closed，
      不等于「整个工具放行」）；
    - 停用的规则不参与匹配，但 explain 单独提示「有条规则本可命中但已停用」
      ——用户能区分「规则关了」与「白名单坏了」。
    """
    proj = home / "proj"
    provider = FakeProvider([
        [ToolUseBlock(id="c1", name="run_command", input={"command": "git status"})],
        [ToolUseBlock(id="c2", name="run_command", input={"command": "git status --short"})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, proj)
    events = await run_eval_turn(agent, "看看仓库状态", _decide_always_first("git status"))

    reqs = requests(events)
    assert [e.input.get("command") for e in reqs] == ["git status"], (
        "沉淀前缀后：干净的同前缀复跑免确认"
    )
    assert (reqs[0].rule_kind, reqs[0].rule_pattern) == ("prefix", "git status")
    gate = agent.gate

    ok = gate.explain("run_command", "git status --short")
    assert ok["allowed"] is True and ok["hit"]["kind"] == "prefix"

    glued = gate.explain("run_command", "git statusx")
    assert glued["allowed"] is False and glued["hit"] is None, "词边界：statusx 不是 status"

    codeflag = gate.explain("run_command", "git status -c")
    assert codeflag["allowed"] is False and codeflag["hit"] is None, (
        "解释器代码旗标命令不吃前缀放行"
    )

    chained = gate.explain("run_command", "git status & echo x")
    assert chained["allowed"] is False and chained["hit"] is None
    assert "拼接" in chained["reason"], "测试器要解释为什么前缀没放行拼接命令"

    # 脏数据 fail-closed：空 pattern 的前缀规则等于没有规则
    assert WhitelistRule(tool="run_command", kind="prefix", pattern="").matches(
        "run_command", "git status"
    ) is False, "空 pattern 不得退化成整个工具放行"

    # 停用规则：不参与匹配，但 explain 单独提示（与「白名单坏了」区分）
    gate2 = PermissionGate(working_dir=proj)
    gate2.add_session_rule(WhitelistRule(
        tool="run_command", kind="prefix", pattern="git status", enabled=False))
    off = gate2.explain("run_command", "git status")
    assert off["allowed"] is False and off["hit"] is None
    assert "停用" in off["reason"], "停用规则有单独提示"
