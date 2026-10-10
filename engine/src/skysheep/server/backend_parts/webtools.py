"""联网搜索 / AI 画图 / 语音输入：设置页配置与语音转写。

从 backend.py 按职责注释整段搬入。方法体逐字保留，逻辑不变。
三个设置卡同属「给 web 工具与语音转写配服务」的职责区：读写
config.toml 的 websearch / imagegen / speech 段并热更新工具实例。
"""

from __future__ import annotations

import base64

import httpx

from ...config import (
    load_config,
    resolve_imagegen,
    resolve_speech,
    resolve_websearch,
    update_config_section,
)
from ...models.probe import _is_local


class WebtoolsMixin:
    """联网搜索 / AI 画图 / 语音输入设置卡与语音转写。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- 联网搜索 / AI 画图：设置页配置（写 config.toml + 热更新工具实例） ----

    async def websearch_detail(self) -> dict:
        ws = self.cfg.websearch
        resolved = resolve_websearch(self.cfg)
        key = resolved.get("api_key") if resolved else ""
        return {
            "provider": ws.provider,
            "providers": ["auto", "custom"],  # 前端只渲染「自动 / 自定义 / 已配置」三档
            "configured_services": self._configured_services(),
            "base_url": ws.base_url,
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "config_hint": (
                "「自动」会优先复用你已配置 Key 的服务；也可选博查（bocha.cn）或 "
                "Tavily 并填入对应 API Key；选「自定义」则填入自建搜索服务地址"
                "（如 SearXNG，无需 Key）；右侧「已配置」则列出你已在「模型服务」里"
                "配好的服务，选中即复用它的地址与 Key（前提是它本身提供搜索接口）。"
                "保存后立即生效。"
            ),
        }

    async def websearch_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "bocha", "tavily", "zhipu", "custom") and (
            provider not in self.cfg.providers
        ):
            raise RuntimeError("联网搜索服务商只支持 auto / custom，或已配置的模型服务")
        updates: dict = {"provider": provider or None}
        api_key = params.get("api_key")
        if api_key is not None:
            updates["api_key"] = str(api_key).strip()
        if params.get("base_url") is not None:
            updates["base_url"] = str(params["base_url"]).strip()
        update_config_section("websearch", updates)
        self.cfg = load_config()
        self._refresh_web_tool_configs()
        return await self.websearch_detail()

    async def imagegen_detail(self) -> dict:
        ig = self.cfg.imagegen
        resolved = resolve_imagegen(self.cfg)
        key = resolved.get("api_key") if resolved else ""
        return {
            "provider": ig.provider,
            "providers": ["auto", "custom"],  # 前端只渲染「自动 / 自定义 / 已配置」三档
            "configured_services": self._configured_services(),
            "model": ig.model,
            "base_url": ig.base_url,
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "resolved_model": (resolved or {}).get("model", ""),
            "config_hint": (
                "「自动」会优先复用你已配置 Key 的服务；自定义需填 "
                "OpenAI 兼容的 /images/generations 接口地址与 Key；右侧「已配置」"
                "列出你已在「模型服务」里配好的服务，选中即复用它的地址与 Key，"
                "此时请在「模型」里填它支持的画图模型名。保存后立即生效。"
            ),
        }

    async def imagegen_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "zhipu", "siliconflow", "custom") and (
            provider not in self.cfg.providers
        ):
            raise RuntimeError("画图服务商只支持 auto / custom，或已配置的模型服务")
        updates: dict = {"provider": provider or None}
        for field_name in ("api_key", "model", "base_url"):
            if params.get(field_name) is not None:
                updates[field_name] = str(params[field_name]).strip()
        update_config_section("imagegen", updates)
        self.cfg = load_config()
        self._refresh_web_tool_configs()
        return await self.imagegen_detail()

    # ---- 语音输入（麦克风转文字） ----

    async def speech_detail(self) -> dict:
        """设置页语音输入卡片的数据（不返回明文 Key）。"""
        sp = self.cfg.speech
        resolved = resolve_speech(self.cfg)
        key = (resolved or {}).get("api_key", "")
        return {
            "provider": sp.provider,
            "providers": ["auto", "custom"],  # 前端只渲染「自动 / 自定义 / 已配置」三档
            "configured_services": self._configured_services(),
            "base_url": sp.base_url,
            "model": sp.model,
            "language": sp.language,
            "has_key": bool(resolved),
            "key_mask": self._mask_key(key),
            "resolved_provider": (resolved or {}).get("provider", ""),
            "resolved_model": (resolved or {}).get("model", ""),
            "config_hint": (
                "决定输入框里的麦克风按钮用哪个服务把语音转成文字。"
                "「自动」会复用你已配置 Key 的同协议服务（智谱 / 硅基流动 / OpenAI）；"
                "也可选「自定义」填任何 OpenAI 兼容的 /audio/transcriptions 地址"
                "（如本地 faster-whisper 服务，无需 Key）；右侧「已配置」列出你已在"
                "「模型服务」里配好的服务，选中即复用它的地址与 Key，"
                "但需要它本身提供语音识别接口。未配置时麦克风按钮会提示来这里配置。"
            ),
        }

    async def speech_save(self, params: dict) -> dict:
        provider = str(params.get("provider", "")).strip()
        if provider not in ("", "auto", "zhipu", "siliconflow", "openai", "custom") and (
            provider not in self.cfg.providers
        ):
            raise RuntimeError("语音服务商只支持 auto / custom，或已配置的模型服务")
        updates: dict = {"provider": provider or None}
        for field_name in ("api_key", "model", "base_url", "language"):
            if params.get(field_name) is not None:
                updates[field_name] = str(params[field_name]).strip()
        update_config_section("speech", updates)
        self.cfg = load_config()
        return await self.speech_detail()

    async def speech_transcribe(self, audio_b64: str, mime: str = "") -> dict:
        """把一段录音（base64）交给配置好的服务转成文字。

        走 OpenAI 协议的 multipart /audio/transcriptions（智谱 GLM-ASR、硅基流动
        SenseVoice、OpenAI Whisper、本地 faster-whisper 服务都兼容）。
        没配置服务时抛可读错误——前端会把「去哪配」告诉用户，不静默失败。
        """


        resolved = resolve_speech(self.cfg)
        if not resolved:
            raise RuntimeError(
                "还没配置语音转写服务。打开 设置 · 语音输入，选一个服务商并填入 API Key"
                "（或在「自定义」里填本地转写服务地址），保存后再试。"
            )
        try:
            audio = base64.b64decode(audio_b64, validate=True)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("录音数据无效（base64 解码失败）") from e
        if not audio:
            raise RuntimeError("没有收到录音数据")
        if len(audio) > 25 * 1024 * 1024:
            raise RuntimeError("录音过长（上限 25 MB），请缩短后重试")

        suffix = ".wav"
        low = (mime or "").lower()
        if "webm" in low:
            suffix = ".webm"
        elif "ogg" in low:
            suffix = ".ogg"
        elif "mp4" in low or "m4a" in low:
            suffix = ".m4a"
        elif "mpeg" in low:
            suffix = ".mp3"
        files = {"file": ("audio" + suffix, audio, mime or "application/octet-stream")}
        data = {"model": resolved["model"]}
        if resolved.get("language"):
            data["language"] = resolved["language"]

        base = (resolved.get("base_url") or "").rstrip("/")
        if not base:
            raise RuntimeError("语音服务缺少接口地址，请到 设置 · 语音输入 里补全")
        headers = {}
        if resolved.get("api_key"):
            headers["Authorization"] = "Bearer " + resolved["api_key"]
        try:
            # 本地地址不走系统代理（与 models/probe.py 的「本地直连」约定同口径）：
            # Windows 上 httpx 默认 trust_env=True 会读注册表里的 WinINET 系统代理，
            # 系统代理进程没在跑时，发往 127.0.0.1 本地转写服务的请求会被转给代理
            # 然后连不上。远端维持默认（信任环境代理，用户可能需要经代理出网）。
            async with httpx.AsyncClient(timeout=120, trust_env=not _is_local(base)) as client:
                resp = await client.post(
                    base + "/audio/transcriptions", headers=headers, files=files, data=data
                )
        except httpx.HTTPError as e:
            raise RuntimeError(
                f"连接语音转写服务失败：{e}（可在 设置 · 语音输入 里检查接口地址/网络）"
            ) from e
        if resp.status_code >= 400:
            detail = resp.text.strip()[:300]
            raise RuntimeError(
                f"语音转写失败（HTTP {resp.status_code}）：{detail or '服务未返回内容'}"
            )
        try:
            payload = resp.json()
        except Exception as e:  # noqa: BLE001
            raise RuntimeError("语音转写服务返回的不是 JSON，检查接口地址是否指向 OpenAI 兼容服务") from e
        text = ""
        if isinstance(payload, dict):
            text = str(payload.get("text") or payload.get("result") or "").strip()
        if not text:
            raise RuntimeError("没有识别到文字（可能是静音或语种不符），请靠近麦克风重试")
        return {"text": text, "provider": resolved["provider"], "model": resolved["model"]}

    def _refresh_web_tool_configs(self) -> None:
        """把新的搜索/画图配置热更新到所有会话的工具实例上（不重建注册表）。"""
        ws_kw = self._websearch_kwargs() or {}
        ig_kw = self._imagegen_kwargs() or {}
        for ag in self._for_each_agent():
            if ag.registry is None:
                continue
            ws_tool = ag.registry.get("web_search")
            if ws_tool is not None:
                ws_tool.provider = ws_kw.get("provider", "")
                ws_tool.api_key = ws_kw.get("api_key", "")
                ws_tool.base_url = ws_kw.get("base_url", "")
            ig_tool = ag.registry.get("generate_image")
            if ig_tool is not None:
                ig_tool.provider = ig_kw.get("provider", "")
                ig_tool.api_key = ig_kw.get("api_key", "")
                ig_tool.base_url = ig_kw.get("base_url", "")
                ig_tool.model = ig_kw.get("model", "")




