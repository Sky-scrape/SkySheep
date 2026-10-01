"""评测基线 · 命令白名单沉淀行为场景（fake provider 驱动真实 Agent 循环）。

两个种子场景，都走「真实循环里弹确认 → 用户选『总是允许』 → 规则沉淀 →
下一条同类命令是否免确认」的完整链路：

4. docker run「总是允许」只沉淀 exact 当条命令，不沉淀两词前缀——后续
   docker run -v 挂载命令仍要确认；
7. 前缀规则（git status）不覆盖 shell 拼接命令——`git status & del …` 仍要确认。

注意：场景 4/7 中被「总是允许」的第一条命令会真实执行（这也是行为基线的
一部分），所以脚本选的都是无副作用、不联网、立即返回的形态：
`docker run`（缺图参数，CLI 端秒退，不触 daemon/网络）、`git status`
（本地只读）。第二条危险命令一律 deny，不会执行。
"""

from __future__ import annotations

from evals._harness import make_agent, requests, run_eval_turn
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import WhitelistRule

_MOUNT_CMD = "docker run --rm -v D:/data:/data alpine ls /data"


async def test_eval_docker_run_allow_always_sediments_exact_not_prefix(home):
    """场景 4（审查项 12 回归）：docker run「总是允许」只沉淀 exact。

    `docker run` 之后的参数可挂载宿主盘、跑任意镜像（任意执行面），两词前缀
    提炼不出有边界的规则。钉住的行为链：
    - 确认弹窗预告的规则是 exact「docker run」，不是前缀「docker run」；
    - 真实沉淀后，当初那条命令免确认，`docker run -v …` 挂载命令仍要确认；
    - 对照：即便历史遗留了一条两词前缀规则，匹配侧也 fail-closed 不命中
      （存量 docker/ssh/scp 前缀规则一并失效，回落逐次确认）。
    """
    proj = home / "proj"
    provider = FakeProvider([
        [ToolUseBlock(id="c1", name="run_command", input={"command": "docker run"})],
        [ToolUseBlock(id="c2", name="run_command", input={"command": _MOUNT_CMD})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, proj)

    def decide(ev):
        return "allow_always" if ev.input.get("command") == "docker run" else "deny"

    events = await run_eval_turn(agent, "跑个 docker 容器", decide)

    reqs = requests(events)
    assert [e.tool_name for e in reqs] == ["run_command", "run_command"], (
        "docker run -v 挂载命令没有因为上次「总是允许」而免确认"
    )
    # 预告的 = 落库的：确认弹窗上的「总是允许」范围就是 exact 当条命令
    assert (reqs[0].rule_kind, reqs[0].rule_pattern) == ("exact", "docker run")
    # 沉淀后：当初那条免确认；挂载命令没有命中任何规则
    hit = agent.gate.explain("run_command", "docker run")
    assert hit["allowed"] is True and hit["hit"]["kind"] == "exact"
    mount = agent.gate.explain("run_command", _MOUNT_CMD)
    assert mount["allowed"] is False and mount["hit"] is None
    # 对照：假如沉淀成两词前缀（或库里遗留这种规则），匹配侧同样 fail-closed
    legacy = WhitelistRule(tool="run_command", kind="prefix", pattern="docker run")
    assert legacy.matches("run_command", _MOUNT_CMD) is False
    # 被拒的挂载命令没有执行
    assert reqs[1].input["command"] == _MOUNT_CMD


async def test_eval_prefix_rule_does_not_cover_shell_chain(home):
    """场景 7（命令白名单拼接判定回归）：前缀规则不覆盖 shell 拼接命令。

    `git status` 选「总是允许」沉淀两词前缀后，`git status & del /q canary.txt`
    仍要确认——前缀规则只保证「完整词前缀命中」，命令里出现 shell 拼接/替换
    元字符就回退逐次确认（`git status; rm -rf /` 不被 `git status` 放行）。
    确认弹窗要带解释文案（用户不能觉得白名单坏了）。
    """
    proj = home / "proj"
    (proj / "canary.txt").write_text("DO-NOT-DELETE", encoding="utf-8")
    chain_cmd = "git status & del /q canary.txt"
    provider = FakeProvider([
        [ToolUseBlock(id="c1", name="run_command", input={"command": "git status"})],
        [ToolUseBlock(id="c2", name="run_command", input={"command": chain_cmd})],
        [TextBlock(text="done")],
    ])
    agent = make_agent(provider, proj)

    def decide(ev):
        return "allow_always" if ev.input.get("command") == "git status" else "deny"

    events = await run_eval_turn(agent, "看看仓库状态", decide)

    reqs = requests(events)
    assert [e.tool_name for e in reqs] == ["run_command", "run_command"], (
        "拼接命令没有因为前缀白名单而免确认"
    )
    assert (reqs[0].rule_kind, reqs[0].rule_pattern) == ("prefix", "git status")
    # 沉淀后：干净的同前缀命令免确认；拼接命令不命中且弹窗带解释
    clean = agent.gate.explain("run_command", "git status --short")
    assert clean["allowed"] is True and clean["hit"]["kind"] == "prefix"
    chained = agent.gate.explain("run_command", chain_cmd)
    assert chained["allowed"] is False and chained["hit"] is None
    assert "拼接" in chained["reason"]
    assert "拼接" in reqs[1].note, "确认弹窗应解释为什么前缀规则没放行这条命令"
    # 拼接命令被拒、未执行：金丝雀文件原样
    assert (proj / "canary.txt").read_text(encoding="utf-8") == "DO-NOT-DELETE"
