"""出站密钥扫描：提示注入纵深防御的机械防线（security/gate.py 执行路径上）。

背景：网页/文档里的注入指令可能诱导模型把本机凭据（config.toml 里的模型
API Key、渠道 token 等）拼进命令、文件写入等工具参数里外传。既有防线是
「外部内容按数据处理」（sanitize.untrusted_frame / scan_injection_patterns）
与权限门逐次确认；本模块在其下再加一层**机械**检查，供权限门在工具真正
执行前调用：

- 命中**已知密钥值**（collect_secrets 从配置收集的值）→ 该参数里带着本机
  配置的真实凭据，调用方必须拒绝执行，白名单与权限档位都放不了行；
- 仅命中**通用密钥形态**（sk-… / ghp_… / xox…- 等高置信前缀）→ 放行，但
  调用方须在结果前注明，让模型与用户都看得见「参数里有疑似密钥」。

设计取舍：
- 只做机械匹配，不做语义判断。已知值命中一律拦（宁可误拦用户确实想操作
  自己 Key 的少数场景）；通用形态只注记不拦（前缀形态误报面更大）。
- 防线自身故障绝不挡执行：配置读不了、正则异常一律按「无已知密钥」降级
  ——这道闸失效时权限门与用户确认仍在，把工具面打瘫反而是更糟的故障。
- 已知值缓存带 TTL：设置页热更新密钥后最迟一个周期内生效；测试用
  reset_cache() 复位。密钥值只驻留本进程内存，绝不进日志（obs 约定：
  fields 只放标识与度量，命中只报配置定位标签如 providers.openai.api_key）。
"""

from __future__ import annotations

import re
import time
from typing import Any

from .. import secure_store

# 短于该长度的配置值不收（如 Ollama 预设的演示 Key「ollama」、占位符）：
# 已知值匹配按子串命中，太短的值会把大量普通词误判成密钥外传。
MIN_SECRET_VALUE_CHARS = 8

# 通用密钥形态清单：高置信前缀 + 足够长的主体。只做线索提示（放行 + 注记），
# 不据此拦截——误报（文档示例、测试夹具里的假 Key）远比已知值误拦常见。
# sk- 一类故意放宽到 [A-Za-z0-9_-]：sk-proj-… / sk-ant-api03-… 等真实发行
# 形态中间带连字符，按 ask 原始的 [A-Za-z0-9]{20,} 会全部漏掉。
GENERIC_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI 兼容 Key（sk-…）", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("GitHub Token（ghp_…）", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("GitHub 细粒度 PAT（github_pat_…）", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")),
    ("Slack Token（xox…-）", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("AWS 访问密钥（AKIA…）", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitLab Token（glpat-…）", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
)

# 已知密钥缓存的刷新周期（秒）：设置页保存新 Key 后，最迟一个周期内生效。
_CACHE_TTL_S = 30.0

# value -> 配置定位标签（如 "providers.openai.api_key"）；只用于报错文案与日志
_cache: dict[str, str] | None = None
_cache_at: float = 0.0


def _section_get(section: Any, key: str) -> Any:
    """小节字段取值，pydantic 模型与原始 dict 两种形态都认（load_config 产
    前者，手改 TOML / 测试夹具可能产后者）。"""
    if isinstance(section, dict):
        return section.get(key)
    return getattr(section, key, None)


def _secret_map(config: Any) -> dict[str, str]:
    """collect_secrets 的带标签本体：value -> 配置定位标签。

    覆盖清单（与 secure_store 的加密字段清单同源，刻意不含 server.token /
    env_key —— 前者不在本期范围，后者是环境变量**名**不是值）：
    - providers.<名>.api_key
    - websearch / imagegen / speech 的 api_key
    - channels.platforms.<平台> 内凭据类字段（按键名子串识别）
    """
    out: dict[str, str] = {}

    def _add(value: Any, label: str) -> None:
        if not isinstance(value, str):
            return
        v = value.strip()
        if len(v) < MIN_SECRET_VALUE_CHARS:
            return
        if v.startswith(secure_store.DPAPI_PREFIX):
            return  # 密文形态不该出现在已解密的配置里；真出现也不当密钥用
        out.setdefault(v, label)

    providers = getattr(config, "providers", None)
    if isinstance(providers, dict):
        for name, section in providers.items():
            _add(_section_get(section, "api_key"), f"providers.{name}.api_key")
    for section_name in ("websearch", "imagegen", "speech"):
        section = getattr(config, section_name, None)
        if section is not None:
            _add(_section_get(section, "api_key"), f"{section_name}.api_key")
    channels = getattr(config, "channels", None)
    platforms = getattr(channels, "platforms", None) if channels is not None else None
    if isinstance(platforms, dict):
        for pname, entry in platforms.items():
            if not isinstance(entry, dict):
                continue
            for key, value in entry.items():
                if secure_store.is_channel_secret_key(key):
                    _add(value, f"channels.platforms.{pname}.{key}")
    return out


def collect_secrets(config: Any) -> list[str]:
    """从配置收集已知密钥值（只收值，去重、剔短值与密文形态）。

    config 是 load_config() 产出的 SkySheepConfig（字段访问做了 pydantic
    模型 / 原始 dict 的双形态兼容）。清单见 _secret_map。
    """
    return list(_secret_map(config))


def reset_cache() -> None:
    """清空已知密钥缓存（测试换 SKYSHEEP_HOME 后、或需要强制重读配置时用）。"""
    global _cache, _cache_at
    _cache = None
    _cache_at = 0.0


def _known_map() -> dict[str, str]:
    """已知密钥表（缓存）：load_config() 读不到 / 解析失败按空表降级。

    首次命中工具执行路径时才拉起配置读取（延迟导入防环）；缓存期内不再
    碰盘。防线只许降级、不许把工具执行面打瘫，所以这里吞掉一切异常。
    """
    global _cache, _cache_at
    now = time.monotonic()
    if _cache is not None and (now - _cache_at) < _CACHE_TTL_S:
        return _cache
    m: dict[str, str] = {}
    try:
        from ..config import load_config  # noqa: PLC0415  延迟导入防环

        m = _secret_map(load_config())
    except Exception:  # noqa: BLE001  读不了配置 = 无已知密钥，防线降级
        m = {}
    _cache = m
    _cache_at = now
    return m


def scan_outbound(text: str, *, known: list[str] | None = None) -> dict:
    """扫描出站文本里的密钥：命中已知值或通用形态即报。

    返回 ``{"ok": bool, "redacted": str, "hits": list[dict]}``：
    - ``ok``：False = 命中**已知密钥值**，调用方必须拒绝执行（fail-closed）；
      仅命中通用形态时 True，放行但调用方应把注记摆在结果前；
    - ``redacted``：把命中片段替换成 ``[已脱敏:类型]`` 后的文本（供日志/预览，
      别把密钥原样落盘）；
    - ``hits``：去重后的命中列表，每项 ``{"kind": "known"|"generic",
      "label": ...}``——known 的 label 是配置定位标签（如
      providers.openai.api_key），generic 的 label 是形态名；**绝不包含
      密钥值本身**。

    known：显式传入的已知密钥值（测试注入用）；缺省用模块缓存的配置密钥表。
    """
    text = text or ""
    known_map: dict[str, str]
    if known is None:
        known_map = _known_map()
    else:
        known_map = {}
        for v in known:
            v = str(v or "").strip()
            if len(v) >= MIN_SECRET_VALUE_CHARS:
                known_map.setdefault(v, "已知密钥")

    # 已知值优先：子串命中全部记下（known 标签）；通用形态只补没被已知值
    # 覆盖的区段（用户真实 Key 同时长得像 sk-… 时按已知值报，文案更有用）。
    spans: list[tuple[int, int, str, str]] = []
    has_known = False
    for value, label in known_map.items():
        start = text.find(value)
        while start != -1:
            spans.append((start, start + len(value), label, "known"))
            has_known = True
            start = text.find(value, start + len(value))
    for name, pat in GENERIC_SECRET_PATTERNS:
        for m in pat.finditer(text):
            s, e = m.span()
            if any(s < pe and ps < e for ps, pe, _, _ in spans):
                continue
            spans.append((s, e, name, "generic"))

    if not spans:
        return {"ok": True, "redacted": text, "hits": []}

    # 拼脱敏文本：重叠区段取靠前、更长的一个，避免嵌套替换损坏文本
    spans.sort(key=lambda t: (t[0], -(t[1] - t[0])))
    merged: list[tuple[int, int, str, str]] = []
    last_end = 0
    for s, e, label, kind in spans:
        if s < last_end:
            continue
        merged.append((s, e, label, kind))
        last_end = e
    parts: list[str] = []
    pos = 0
    for s, e, label, _kind in merged:
        parts.append(text[pos:s])
        parts.append(f"[已脱敏:{label}]")
        pos = e
    parts.append(text[pos:])

    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for _s, _e, label, kind in spans:
        key = (kind, label)
        if key not in seen:
            seen.add(key)
            hits.append({"kind": kind, "label": label})
    return {"ok": not has_known, "redacted": "".join(parts), "hits": hits}


__all__ = [
    "GENERIC_SECRET_PATTERNS",
    "MIN_SECRET_VALUE_CHARS",
    "collect_secrets",
    "reset_cache",
    "scan_outbound",
]
