"""mcp.json 鉴权头的落盘加密（审计 L16 的代码半边）。

`~/.skysheep/mcp.json` 与项目 `.skysheep/mcp.json` 的 ``headers`` 常见形态是
``Authorization: Bearer <token>``——远程 MCP 服务的凭据。config.toml 里的渠道
凭据早已按 DPAPI 加密，这里此前是全平台明文，两处保护口径不一致。

现在与 config.toml 同一套机制（:mod:`skysheep.secure_store`）：

- **写侧**（:func:`encrypt_headers`）：所有非空 header 值加密成 ``dpapi:<b64>``，
  已是密文的原样保留（幂等，重复保存不会二次加密）；
- **读侧**（:func:`decrypt_headers`）：连接前解密；解不开（换机器/换账户/非
  Windows 写入的明文）原样返回，交给服务端表现为 401，用户重填即可；
- **非 Windows / DPAPI 不可用**：``encrypt_value`` 原样返回，行为与改动前一致
  （与 config.toml 的取舍同一份，见 SECURITY.md）。

展示侧不用这个模块：设置页只回显「已保存」与字段名，从不回显 header 值。
"""

from __future__ import annotations

from .. import secure_store


def encrypt_headers(headers: dict | None) -> dict[str, str]:
    """把 header 值加密后返回新字典（非 dict 输入返回空字典）。"""
    if not isinstance(headers, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        out[key] = value if (not value or secure_store.is_encrypted(value)) \
            else secure_store.encrypt_value(value)
    return out


def decrypt_headers(headers: dict | None) -> dict[str, str]:
    """把 header 值解密后返回新字典；解不开的原样保留（不静默清空）。"""
    if not isinstance(headers, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if value and secure_store.is_encrypted(value):
            plain = secure_store.decrypt_value(value)
            out[key] = value if plain is None else plain
        else:
            out[key] = value
    return out
