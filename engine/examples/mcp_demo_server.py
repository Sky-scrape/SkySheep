"""本地 MCP 演示服务器（stdio 传输），用于验收与集成测试。

运行：python examples/mcp_demo_server.py
SkySheep 接入：~/.skysheep/mcp.json 写入
    {"mcpServers": {"demo": {"command": "python", "args": ["examples/mcp_demo_server.py"]}}}
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("skysheep-demo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers and return the sum."""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """Echo the given text back."""
    return text


@server.tool()
def now() -> str:
    """Return the current local time as a string."""
    import datetime

    return datetime.datetime.now().isoformat(timespec="seconds")


if __name__ == "__main__":
    server.run()
