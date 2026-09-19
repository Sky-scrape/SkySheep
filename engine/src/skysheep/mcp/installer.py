"""MCP 服务导入：把服务定义写进 ~/.skysheep/mcp.json，程序里点几下就能接上。

支持的来源（设置页里三种入口，最终都汇到这里的 import_servers）：
- 粘贴一段 JSON：可以是 Claude Desktop 风格的 ``{"mcpServers": {...}}``，
  也可以是单个服务的定义 ``{"command": "uvx", "args": ["mcp-server-fetch"]}``；
- 粘贴单个服务的 command / url（表单里的分字段输入）；
- 选一个本机的 .json 配置文件。

写入前每个服务都做一次校验：必须有 command（stdio）或 url（http）之一，
字段类型正确；重名默认不覆盖，需要用户明确选择覆盖。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .client import MCPServerConfig

SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")


class MCPInstallError(Exception):
    pass


def mcp_config_path(home: Path) -> Path:
    """全局 MCP 配置文件位置（与 client.load_mcp_configs 的约定保持一致）。"""
    return home / "mcp.json"


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not SERVER_NAME_RE.match(name):
        raise MCPInstallError(
            "服务名只能用字母、数字、下划线、点、短横线（需以字母或数字开头）: "
            + (name or "(空)")
        )
    return name


def normalize_server(raw: dict) -> MCPServerConfig:
    """把一段用户给的 JSON（或表单字段）变成校验过的 MCPServerConfig。"""
    if not isinstance(raw, dict):
        raise MCPInstallError("服务定义必须是一个 JSON 对象")
    known = {"command", "args", "env", "url", "headers", "readonly"}
    section = {k: v for k, v in raw.items() if k in known}
    if not section:
        raise MCPInstallError(
            "缺少必要字段：需要 command（本地命令）或 url（远程地址）之一"
        )
    try:
        cfg = MCPServerConfig(**section)
    except Exception as e:  # pydantic 校验失败 → 转成能读懂的报错
        first = str(e).strip().splitlines()[0]
        raise MCPInstallError("服务定义不合法：" + first) from e
    if cfg.transport == "invalid":
        raise MCPInstallError(
            "缺少必要字段：需要 command（本地命令）或 url（远程地址）之一"
        )
    if cfg.args and not all(isinstance(a, str) for a in cfg.args):
        raise MCPInstallError("args 必须是字符串数组，例如 [\"-y\", \"mcp-server-fetch\"]")
    if cfg.env and not all(
        isinstance(k, str) and isinstance(v, str) for k, v in cfg.env.items()
    ):
        raise MCPInstallError("env 必须是「字符串→字符串」的对象")
    if cfg.headers and not all(
        isinstance(k, str) and isinstance(v, str) and k.strip()
        for k, v in cfg.headers.items()
    ):
        raise MCPInstallError(
            "headers 必须是「字符串→字符串」的对象，例如 "
            '{"Authorization": "Bearer ..."}'
        )
    return cfg


def parse_snippet(text: str) -> dict[str, MCPServerConfig]:
    """解析粘贴的 JSON：整份配置 / {"mcpServers": {...}} / 单个服务定义都认。"""
    text = (text or "").strip()
    if not text:
        raise MCPInstallError("请先粘贴要导入的 MCP 配置")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise MCPInstallError(f"JSON 格式有误（第 {e.lineno} 行）：{e.msg}") from e
    if not isinstance(data, dict):
        raise MCPInstallError("最外层需要是一个 JSON 对象")
    # 包一层 mcpServers 的完整配置
    if "mcpServers" in data:
        servers = data["mcpServers"]
        if not isinstance(servers, dict) or not servers:
            raise MCPInstallError("mcpServers 里没有服务定义")
        out: dict[str, MCPServerConfig] = {}
        for name, raw in servers.items():
            out[validate_name(str(name))] = normalize_server(raw)
        return out
    # 单个服务定义：带 name 时用它，否则报错让用户补
    if any(k in data for k in ("command", "url")):
        cfg = normalize_server(data)  # 先校验定义本身，报错更贴近问题
        name = str(data.get("name") or "").strip()
        if not name:
            raise MCPInstallError(
                "这是单个服务的定义，请在 JSON 里加一个 \"name\": \"服务名\" 字段"
            )
        return {validate_name(name): cfg}
    raise MCPInstallError(
        "看不懂这段配置：需要 {\"mcpServers\": {...}} 或含 command/url 的服务定义"
    )


def load_servers(path: Path) -> dict[str, dict]:
    """读取 mcp.json 的 mcpServers 段（文件不存在或坏掉时返回空）。"""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def parse_file(path: str | Path) -> dict[str, MCPServerConfig]:
    """从一个本机 .json 文件里读出服务定义。"""
    p = Path(path).expanduser()
    if not p.is_file():
        raise MCPInstallError("文件不存在：" + str(p))
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise MCPInstallError("读取失败：" + str(e)) from e
    try:
        return parse_snippet(text)
    except MCPInstallError as e:
        raise MCPInstallError(f"{p.name}：{e}") from e


def save_servers(path: Path, servers: dict[str, dict]) -> None:
    """整体写回 mcp.json（保持 Claude Desktop 的 mcpServers 结构）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"mcpServers": servers}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def import_servers(
    servers: dict[str, MCPServerConfig],
    path: Path,
    *,
    overwrite: bool = False,
) -> dict:
    """把服务写进 mcp.json。同名的默认跳过（除非 overwrite=True）。"""
    if not servers:
        raise MCPInstallError("没有要导入的服务")
    existing = load_servers(path)
    added: list[str] = []
    skipped: list[str] = []
    for name, cfg in servers.items():
        if name in existing and not overwrite:
            skipped.append(name)
            continue
        existing[name] = cfg.model_dump(exclude_none=True)
        added.append(name)
    if added:
        save_servers(path, existing)
    return {
        "added": added,
        "skipped": skipped,
        "path": str(path),
        "total": len(existing),
    }


def remove_server(name: str, path: Path) -> dict:
    """从 mcp.json 里删掉一个服务。"""
    existing = load_servers(path)
    if name not in existing:
        raise MCPInstallError("找不到 MCP 服务：" + name)
    del existing[name]
    save_servers(path, existing)
    return {"removed": name, "path": str(path), "total": len(existing)}
