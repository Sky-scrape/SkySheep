"""评测基线 · 全局记忆注入检索化行为场景（SKYSHEEP_HOME 隔离下驱动真实落盘与注入）。

场景 19/20/21：记忆检索化第一期（2026-10「长记忆挤占注入预算」回归史）的阈值切换：
- 记忆不超过 MEMORY_RETRIEVAL_THRESHOLD：**整块注入**——带不带本轮查询，
  render_memory_section 的输出逐字节一致（与检索化之前完全一致的老行为）；
- 超过阈值且拿得到本轮查询：切条目按相关性选取，只注入命中条目并附
  「共 N 条、已按相关性注入 M 条」说明——相关子集，不是全部；
- 查询与全部条目零交集：按「宁多勿漏」约定退回整块注入。

落盘用引擎的 write_text_atomic（memory.md 是引擎自有状态文件），注入读取走
render_memory_section——backend 的 compose_system_for 把它拼进每轮系统提示词
（server/backend.py），本场景钉的就是这个接缝的行为。
"""

from __future__ import annotations

from evals._harness import finished, make_agent, requests, run_eval_turn
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.textio import write_text_atomic
from skysheep.tools.memory import (
    MAX_MEMORY_CHARS,
    MEMORY_RETRIEVAL_THRESHOLD,
    render_memory_section,
)

_IRRELEVANT = "这是一条与当前查询毫无交集的长期背景知识，用来撑起记忆总量"


def _seed_memory(home, lines: list[str]) -> None:
    """按 memory.md 的标准条目格式写入隔离 home（引擎自有状态文件走原子写）。"""
    path = home / "home" / "memory.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, "\n".join(lines) + "\n")


def _memory_chars(lines: list[str]) -> int:
    return sum(len(ln) + 1 for ln in lines)


def _small_memory_lines() -> list[str]:
    return [
        f"- [2026-09-0{i}] 偏好{i}：交付前先跑一遍 lint 与测试" for i in range(1, 4)
    ]


def _large_memory_lines() -> list[str]:
    """40 条记忆（总长 > 阈值）：3 条 docker 部署相关，其余与查询零交集。"""
    lines = []
    for i in range(40):
        if i in (5, 17, 33):
            lines.append(
                f"- [2026-09-01] 条目{i:02d}：服务器上用 docker 部署服务，"
                "镜像需在本地构建后推送"
            )
        else:
            lines.append(f"- [2026-09-01] 条目{i:02d}：{_IRRELEVANT}")
    return lines


async def test_eval_memory_small_stays_whole_block_regardless_of_query(home):
    """场景 19（记忆检索阈值切换 · 小记忆侧）：整块注入不受查询影响。

    记忆不超过阈值时是「逐字节一致」的老行为：带查询与不带查询走同一条
    整块路径；输出里不得出现检索化说明行。
    """
    lines = _small_memory_lines()
    _seed_memory(home, lines)
    assert _memory_chars(lines) <= MEMORY_RETRIEVAL_THRESHOLD, "前置不成立：应低于阈值"

    whole = render_memory_section("")
    with_query = render_memory_section("lint 测试 交付偏好")
    assert whole == with_query, "低于阈值：带不带查询，注入必须逐字节一致"
    assert "已按相关性注入" not in whole, "整块路径不带检索化说明"
    assert whole.startswith("\n# User memory"), "记忆段标题契约不变"
    for ln in lines:
        assert ln in whole, "小记忆整块完整注入"
    assert len(whole) <= MAX_MEMORY_CHARS + 200, "整块注入受 MAX_MEMORY_CHARS 上限约束"


async def test_eval_memory_large_switches_to_relevant_subset(home):
    """场景 20（记忆检索阈值切换 · 大记忆侧）：超阈值按相关性注入子集。

    40 条记忆超过阈值、本轮查询命中 docker 部署条目时：只注入相关条目，
    说明行如实报告「共 N 条、注入 M 条」；与查询零交集的条目不进系统提示词
    （注入预算让给相关性）。零命中查询与拿不到查询（轮外 set_system 场景）
    都退回整块——宁多勿漏。
    """
    lines = _large_memory_lines()
    _seed_memory(home, lines)
    assert _memory_chars(lines) > MEMORY_RETRIEVAL_THRESHOLD, "前置不成立：应超过阈值"

    out = render_memory_section("docker 部署流程")
    assert "已按相关性注入" in out, "超阈值+有查询：切换为检索注入并附说明"
    assert "共 40 条记忆" in out
    for ln in (x for x in lines if "docker" in x):
        assert ln in out, "命中的条目逐条原文注入"
    assert _IRRELEVANT not in out, "零相关条目不占注入预算"
    assert out.startswith("\n# User memory")

    # 零命中查询：全部条目 0 分 → 按约定退回整块注入（宁多勿漏）
    fallback = render_memory_section("量子天文观测")
    assert "已按相关性注入" not in fallback
    assert _IRRELEVANT in fallback, "零命中退回整块：全部条目可见"

    # 拿不到查询（轮外 set_system 场景）：同样整块，且与零命中逐字节一致
    no_query = render_memory_section("")
    assert no_query == fallback, "无查询 = 整块路径，与检索化之前一致"


async def test_eval_memory_written_by_real_tool_then_injected(home):
    """场景 21（写读闭环）：真实 memory_write 工具落盘的记忆进入下一轮注入。

    memory_write 是名义 READONLY、实际写引擎主目录的工具（append 要用户
    确认，安全审查 FINDING 2 的守卫口径）——本场景在真实权限门下放行一次
    append，然后断言刚写入的条目出现在 render_memory_section 的输出里：
    「记住的事下一轮就能被模型看到」这条产品承诺的组装行为。
    """
    proj = home / "proj"
    provider = FakeProvider([
        [ToolUseBlock(id="m1", name="memory_write",
                      input={"action": "append",
                             "content": "用户偏好用 Markdown 表格收日报"})],
        [TextBlock(text="已记住。")],
    ])
    agent = make_agent(provider, proj)
    events = await run_eval_turn(agent, "记住我用 Markdown 表格收日报",
                                 decide=lambda ev: "allow_once")
    reqs = requests(events)
    assert [e.tool_name for e in reqs] == ["memory_write"], (
        "写引擎主目录的记忆工具必须先确认（不能因 READONLY 名义免确认）"
    )
    done = finished(events)
    assert not done["m1"].is_error, "放行后 append 成功"
    assert "remembered" in done["m1"].preview

    injected = render_memory_section("")
    assert "用户偏好用 Markdown 表格收日报" in injected, "刚写入的记忆进入下一轮注入"
