"""内置常用 MCP 服务预设：设置页一键添加，不必用户手填 command/args。

每个预设声明它依赖的运行时（need）与参数里的 ``{dir}`` 占位符：
- need="uv"：走 uvx（uv 随 SkySheep 开发环境自带；打包版看用户机器有没有 uv）；
- need="node"：走 npx（需要本机装过 Node.js）；
- need="local"：本机已安装的独立程序（command 即程序名，缺了会连不上，
  由预设 desc 告知安装来源）；
- ``{dir}``：添加时由后端替换成当前工作目录（见 backend.add_mcp_preset）。

这里只声明"怎么配"，不预写入任何用户的 mcp.json——点了「＋ 添加」才落地，
避免首次启动就去下载外部包或因缺运行时失败。预设本身不引入任何 Python 依赖。
"""

from __future__ import annotations

from typing import TypedDict


class MCPPreset(TypedDict):
    name: str      # 写入 mcp.json 的服务名，也是 mcp__<name>__<tool> 的前缀
    label: str     # 界面展示名
    desc: str      # 一句话说明（卡片正文 + 悬停）
    need: str      # 依赖运行时："uv" | "node" | "local"
    command: str
    args: list[str]
    readonly: bool  # True → 工具自动放行（纯只读/纯推理，无副作用）


MCP_PRESETS: list[MCPPreset] = [
    {
        "name": "sequential-thinking",
        "label": "结构化分步思考",
        "desc": "把复杂问题拆成多步反思推理，可回溯修正（内置没有这种能力）",
        "need": "node",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        # 纯内存推理、无副作用，标只读免每次弹确认——它一次任务会调用十几次
        "readonly": True,
    },
    {
        "name": "fetch",
        "label": "网页抓取 fetch",
        "desc": "抓取网页转成 Markdown 给模型读",
        "need": "uv",
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "readonly": True,
    },
    {
        "name": "time",
        "label": "时间 / 时区",
        "desc": "查当前时间、时区换算",
        "need": "uv",
        "command": "uvx",
        "args": ["mcp-server-time"],
        "readonly": True,
    },
    {
        "name": "git",
        "label": "Git 仓库",
        "desc": "查看 / 操作本地 Git 仓库（调用时传入仓库路径）",
        "need": "uv",
        "command": "uvx",
        # 不带 --repository：它指向的目录不是 git 仓库时服务器会启动即退出
        # （Connection closed），整台服务器废掉；不带参数则启动常驻，仓库路径
        # 由模型在每次工具调用时传入，指错也只是单次调用报错。
        "args": ["mcp-server-git"],
        "readonly": True,
    },
    {
        "name": "filesystem",
        "label": "文件读写",
        "desc": "让 Agent 读写指定目录（默认当前项目）",
        "need": "node",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "{dir}"],
        "readonly": False,
    },
    {
        "name": "memory",
        "label": "记忆库",
        "desc": "知识图谱式长期记忆",
        "need": "node",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "readonly": False,
    },
    {
        "name": "cua-driver",
        "label": "cua 电脑驱动",
        "desc": "cua.ai 开源桌面驱动：按界面元素精准操控本机应用与浏览器，"
        "不抢鼠标键盘焦点；需先按 cua.ai 指引安装 cua-driver 程序",
        "need": "local",
        "command": "cua-driver",
        "args": ["mcp"],
        # 操控真实桌面，绝不能标只读——标了 READONLY 权限门会自动放行
        "readonly": False,
    },
]


def preset_by_name(name: str) -> MCPPreset | None:
    for p in MCP_PRESETS:
        if p["name"] == name:
            return p
    return None


# 前端展示用的安全视图：只给元数据，不给 command/args（本机路径等细节前端用不到，
# 添加时由后端按预设原文写入）
def presets_public() -> list[dict]:
    return [
        {
            "name": p["name"],
            "label": p["label"],
            "desc": p["desc"],
            "need": p["need"],
            "readonly": p["readonly"],
        }
        for p in MCP_PRESETS
    ]
