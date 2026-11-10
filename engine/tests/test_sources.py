"""引用溯源：来源登记簿、工具侧标注、Agent 轮末随消息落库。"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from skysheep.core import Agent
from skysheep.messages import TextBlock, ToolUseBlock
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import PermissionGate
from skysheep.sources import MAX_SOURCES, SourceRegistry, register_source
from skysheep.tools.base import Safety, Tool, ToolContext, ToolRegistry
from skysheep.tools.web import WebSearchTool, html_title

# ---------------------------------------------------------------- 登记簿


def test_registry_assigns_stable_indices_and_dedupes():
    reg = SourceRegistry()
    assert reg.register("https://a.example/1", "A 页") == 1
    assert reg.register("https://b.example/2") == 2
    # 同一 URL 复用编号；后到的标题补上前一个没有的标题
    assert reg.register("https://a.example/1") == 1
    assert reg.to_list()[0]["title"] == "A 页"
    assert len(reg) == 2


def test_registry_ignores_empty_and_caps():
    reg = SourceRegistry(max_sources=2)
    assert reg.register("") == 0
    assert reg.register("https://a/1") == 1
    assert reg.register("https://a/2") == 2
    assert reg.register("https://a/3") == 0  # 超上限：不编号，但抓取不受影响
    assert len(reg) == 2


def test_label_and_helper_without_registry():
    reg = SourceRegistry()
    assert reg.label("https://a/1") == "[来源 1] "
    assert reg.label("https://a/1") == "[来源 1] "
    # ctx 没有登记簿（测试/子代理路径）：零标注，行为与加来源之前一致
    ctx = ToolContext(working_dir=None)
    assert register_source(ctx, "https://a/1") == ""


def test_to_list_shape():
    reg = SourceRegistry()
    reg.register("https://a/1", "标题", "web_search")
    assert reg.to_list() == [
        {"index": 1, "url": "https://a/1", "title": "标题", "kind": "web_search"}
    ]
    assert MAX_SOURCES >= 20  # 上限至少覆盖一页搜索结果的常见条数


# ---------------------------------------------------------------- 工具侧标注


async def test_web_search_labels_each_result(monkeypatch):
    tool = WebSearchTool(provider="bocha", api_key="k")
    monkeypatch.setattr(
        tool, "_search_sync",
        lambda q, n: [
            {"title": "文档 A", "url": "https://a/1", "snippet": "摘要 A"},
            {"title": "文档 B", "url": "https://b/2", "snippet": "摘要 B"},
        ],
    )
    ctx = ToolContext(working_dir=None)
    ctx.sources = SourceRegistry()
    out = await tool.run(
        type("Args", (), {"query": "q", "max_results": 2})(), ctx
    )
    assert "[来源 1] 文档 A" in out and "[来源 2] 文档 B" in out
    assert [s["url"] for s in ctx.sources.to_list()] == ["https://a/1", "https://b/2"]


def test_html_title_extracts_and_normalizes():
    body = b"<html><head><title>  Sky\n Sheep  \xe6\x96\x87\xe6\xa1\xa3 </title></head></html>"
    assert html_title(body, "utf-8") == "Sky Sheep 文档"
    assert html_title(b"<html>no title</html>", "utf-8") == ""
    assert html_title(b"<title>\xff\xfe</title>", "utf-8") != ""  # 坏字节不抛


# ---------------------------------------------------------------- Agent 接线


class _Args(BaseModel):
    url: str = "https://a/1"


class _SourceTool(Tool):
    name = "fake_source_tool"
    description = "登记一个来源（测试用）"
    safety = Safety.READONLY
    read_only_hint = True
    destructive_hint = False
    idempotent_hint = True
    open_world_hint = False
    args_model = _Args

    async def run(self, args, ctx):  # noqa: ANN001
        label = register_source(ctx, args.url, "示例页")
        return f"{label}抓取内容正文"


def _agent(tmp_path: Path, scripted: list[list]) -> Agent:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return Agent(
        provider=FakeProvider(scripted),
        registry=ToolRegistry([_SourceTool()]),
        gate=PermissionGate(working_dir=proj),
        working_dir=proj,
    )


async def _run(agent: Agent, text: str = "查一下"):
    events = []
    async for ev in agent.run_turn(text):
        events.append(ev)
        if ev.kind == "permission_request":
            agent.respond_permission(ev.request_id, "deny")
    return events


async def test_agent_attaches_sources_to_final_message(tmp_path):
    agent = _agent(tmp_path, [
        [ToolUseBlock(id="t1", name="fake_source_tool", input={"url": "https://a/1"})],
        [TextBlock(text="结论见 [来源 1]")],
    ])
    await _run(agent)
    final = agent.history[-1]
    assert final.role == "assistant"
    assert final.sources == [
        {"index": 1, "url": "https://a/1", "title": "示例页", "kind": "web_fetch"}
    ]
    # 工具输出里的编号前缀也真的进了历史（模型能看到）
    tool_msgs = [m for m in agent.history if m.role == "tool"]
    assert tool_msgs[0].content[0].content == "[来源 1] 抓取内容正文"


async def test_agent_without_sources_leaves_field_none(tmp_path):
    agent = _agent(tmp_path, [[TextBlock(text="直接回答")]])
    await _run(agent)
    assert agent.history[-1].sources is None


async def test_duplicate_url_reuses_same_index(tmp_path):
    agent = _agent(tmp_path, [
        [
            ToolUseBlock(id="t1", name="fake_source_tool", input={"url": "https://a/1"}),
            ToolUseBlock(id="t2", name="fake_source_tool", input={"url": "https://a/1"}),
        ],
        [TextBlock(text="两次抓同一页")],
    ])
    await _run(agent)
    assert agent.history[-1].sources == [
        {"index": 1, "url": "https://a/1", "title": "示例页", "kind": "web_fetch"}
    ]


async def test_sources_are_per_turn(tmp_path):
    agent = _agent(tmp_path, [
        [ToolUseBlock(id="t1", name="fake_source_tool", input={"url": "https://a/1"})],
        [TextBlock(text="第一轮")],
        [ToolUseBlock(id="t2", name="fake_source_tool", input={"url": "https://b/2"})],
        [TextBlock(text="第二轮")],
    ])
    await _run(agent, "第一问")
    first = agent.history[-1].sources
    await _run(agent, "第二问")
    second = agent.history[-1].sources
    assert first[0]["url"] == "https://a/1"
    assert second == [{"index": 1, "url": "https://b/2", "title": "示例页", "kind": "web_fetch"}]


def test_source_tool_stays_readonly():
    """来源标注不改变权限分级：读取网页仍是 READONLY，不会凭空多一次确认。"""
    assert _SourceTool().to_schema()["annotations"]["readOnlyHint"] is True
