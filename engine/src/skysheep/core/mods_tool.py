"""Mod 注册的工具 → 引擎 Tool 的包装（P6）。

Mod 在 Node 侧调用 $.tool.register({name, description, schema}, handler)：
- 元数据（name/description/schema/safety）经 host 桥回到 Python 侧登记
- handler 函数本身留在 Node 进程内（函数不能过 JSON）
- 模型调用该工具时，这里的包装类转调 runtime.invoke_tool 反向执行 handler

安全边界：Mod 工具一律按 DANGEROUS 分级——它们是第三方代码，且运行在
无沙箱的 Node 进程内，不能凭 Mod 自报的 safety 就获得免确认资格。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

from ..tools.base import Safety, Tool, ToolContext, ToolError


def _make_args_model(name: str, schema: dict) -> type[BaseModel]:
    """把 JSON Schema 的 properties 转成宽松的 pydantic 模型。

    只取字段名与是否必填，类型一律 Any：Mod 自己负责校验参数，引擎不做
    严格类型转换（MCP 工具同款宽松策略，避免 schema 方言差异导致误拒）。
    """
    props = schema.get("properties") if isinstance(schema, dict) else {}
    required = schema.get("required") if isinstance(schema, dict) else []
    if not isinstance(props, dict) or not props:
        return create_model(f"ModToolArgs_{name}", __config__=ConfigDict(extra="allow"))
    req = set(required) if isinstance(required, list) else set()
    fields: dict[str, Any] = {}
    for key in props:
        if not isinstance(key, str) or not key.isidentifier():
            continue
        fields[key] = (Any, ... if key in req else None)
    return create_model(
        f"ModToolArgs_{name}", __config__=ConfigDict(extra="allow"), **fields,
    )


class ModTool(Tool):
    """一个由 Mod 注册的工具。"""

    safety = Safety.DANGEROUS  # 第三方代码，一律要确认（见模块注释）
    read_only_hint = False
    destructive_hint = True
    idempotent_hint = False

    def __init__(self, *, mod_id: str, name: str, description: str,
                 schema: dict, loaded_mod: object) -> None:
        self.mod_id = mod_id
        self.name = name
        self.description = description or f"由 Mod「{mod_id}」提供的工具"
        self.args_model = _make_args_model(name.replace("-", "_"), schema or {})
        self._raw_schema = schema or {}
        self._mod = loaded_mod  # ClaudeLoadedMod

    def to_schema(self) -> dict:
        """直接用 Mod 声明的原始 schema，不做 pydantic 反推（保真）。"""
        params = self._raw_schema if isinstance(self._raw_schema, dict) and self._raw_schema else (
            {"type": "object", "properties": {}}
        )
        schema = super().to_schema()
        schema["input_schema"] = params
        return schema

    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        payload = args.model_dump() if hasattr(args, "model_dump") else dict(args)
        runtime = getattr(self._mod, "_runtime", None)
        if runtime is None:
            raise ToolError(f"Mod「{self.mod_id}」的运行时不可用")
        text, err = await runtime.invoke_tool(self.name, payload)
        if err:
            raise ToolError(err)
        return text
