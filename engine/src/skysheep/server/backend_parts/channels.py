"""聊天软件渠道（Bot Channel）：飞书 / 微信接入桌面 Agent 的设置、会话与运行。

从 backend.py 按职责注释整段搬入：渠道预授权告警名单等模块级小件随职责区
落在本模块，backend 引用以保持旧命名空间可见（测试直接 import）。
方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import logging

from ...bgtasks import spawn_bg
from ...channels import ChannelGate
from ...channels.cards import permission_request_card
from ...config import _read_raw_config, load_config, update_config_section
from ...tools import ChangeRecorder
from ._shared import SessionRuntime

logger = logging.getLogger("skysheep.security")

def _normalize_id_list(raw) -> list[str]:
    """把界面传来的允许名单归一成字符串列表。

    界面用 textarea（每行一个）提交，备份/脚本可能传列表；数字 id 是常见笔误，
    而名单比较是字符串相等，不转换会静默失效——这种失败很难排查。
    """
    if isinstance(raw, str):
        raw = raw.replace(",", "\n").splitlines()
    if not isinstance(raw, (list, tuple)):
        raw = [raw] if raw is not None else []
    return [str(x).strip() for x in raw if str(x).strip()]


# 渠道预授权名单里出现这些工具时告警：它们在无人值守渠道里等于「任意命令 / 任意写」
_CHANNEL_DANGEROUS_TOOLS = (
    "run_command", "write_file", "edit_file", "delete_file", "move_file",
    "make_dir", "write_document", "generate_image",
)


def _channel_allowed_tools_warning(allowed) -> str:
    """渠道预授权名单里含写/执行类工具时返回告警文案，否则空串。"""
    names = {str(t).strip() for t in (allowed or []) if str(t).strip()}
    hit = sorted(names & set(_CHANNEL_DANGEROUS_TOOLS))
    if not hit:
        return ""
    return (
        "注意：" + "、".join(hit) + " 是写/执行类工具，加入预授权名单后，"
        "渠道会话在无人值守时也会直接执行、不再逐次确认。"
        "只在你完全信任该渠道的允许名单成员时这样做。"
    )


def _raw_platforms_base() -> dict:
    """渠道写路径的 platforms 基底：取 _read_raw_config 的原值形态，不用解密视图。

    load_config() 的解密视图会把「本机解不开」的 dpapi: 密文置空（值要送进
    适配器，绝不能把密文当凭据用）；但写路径若也拿它当基底，改名单、启停、
    推游标这类无关保存就会经 update_config_section 整表覆盖 platforms，把
    解不开的密文抹成空串——文件换回原机器/原账户再也解不开，违背
    keep_on_failure=True 的设计承诺。基底必须取 keep_on_failure=True 的
    raw：解得开的已是明文（写盘时重新加密），解不开的原样透传（写侧对已
    加密值幂等），本次要改的字段再逐个合并进去。
    """
    _, raw = _read_raw_config()
    channels = raw.get("channels")
    platforms = channels.get("platforms") if isinstance(channels, dict) else None
    return dict(platforms) if isinstance(platforms, dict) else {}


# ---- 权限请求推渠道（P21 剩余半边）----
# 安全口径：这条通路**只提醒、不审批**。渠道里没有任何放行/拒绝入口，卡片只带
# 「打开 SkySheep 处理」的唤回链接，决策仍由桌面端 PermissionGate 做。因此开关
# 默认关、由用户显式打开，打开也不改变任何门语义。

# 详情截断：渠道消息是「提醒」，不该把整段 diff 或超长命令推到聊天软件里
PERMISSION_PUSH_DETAIL_CHARS = 400
# 桌面壳注册的唤回协议（点击通知/卡片回到同一个 SkySheep 窗口）
PERMISSION_FOCUS_LINK = "skysheep://focus"


def permission_push_headline(ev: dict) -> str:
    """权限提醒的标题：<工具> 请求执行。"""
    return f"{str(ev.get('tool_name') or '工具')} 请求执行"


def permission_push_body(ev: dict, session_title: str = "") -> str:
    """权限提醒正文：会话 / 操作说明 / 影响范围 / 发起方 + 一句「怎么处理」。

    只取事件里已有的字段，不做任何推断：事件里没有的信息（比如超出 scope 的
    路径）宁可不写，也不让渠道侧看到比桌面确认卡更多的内容。
    """
    session = (session_title or str(ev.get("session_id") or "")).strip() or "（未命名会话）"
    lines = [f"会话：{session}"]
    detail = str(ev.get("detail") or "").strip()
    if detail:
        if len(detail) > PERMISSION_PUSH_DETAIL_CHARS:
            detail = detail[:PERMISSION_PUSH_DETAIL_CHARS] + "…（已截断）"
        lines.append("操作：" + detail)
    scope = str(ev.get("scope") or "").strip()
    if scope:
        lines.append("影响范围：" + scope)
    origin = str(ev.get("origin") or "").strip()
    if origin:
        lines.append("发起方：" + origin)
    lines.append("请回到 SkySheep 窗口确认；超时未回复按拒绝处理。")
    return "\n".join(lines)


async def push_permission_request(mgr, store, ev: dict) -> int:
    """把一条权限请求推到所有目标渠道（纯提醒）。返回成功送达次数。

    会话标题现场查（emit 侧还没有它）；查不到退回会话 id，再退回「未命名会话」。
    任何异常都只记日志——提醒失败绝不能影响权限流程本身。
    """
    from .automation import push_text_to_targets  # 局部导入避免模块环

    title = ""
    sid = str(ev.get("session_id") or "")
    if store is not None and sid:
        try:
            title = str(await store.get_session_title(sid) or "")
        except Exception as e:  # noqa: BLE001 - 标题只是修饰，取不到不影响提醒
            logger.debug("权限提醒取会话标题失败：%s", e)
    body = permission_push_body(ev, title)
    card = permission_request_card(
        title=permission_push_headline(ev),
        detail=body,
        link=PERMISSION_FOCUS_LINK,
    )
    return await push_text_to_targets(mgr, body, what="权限请求提醒", card=card)


class ChannelsMixin:
    """聊天软件渠道：设置、微信扫码登录、会话与运行（ChannelManager 回调面）。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- 聊天软件渠道（Bot Channel）：设置、会话、运行 ----
    # 安全姿态与 computer_control / browser_control 同类：默认关、属降低防护的开关，
    # 因此 channel.* 的写操作在 server/app.py 的 dispatch 层仅允许本机调用。

    def _channels_config(self) -> dict:
        """给 ChannelManager 的配置提供者：每次都读最新 cfg，改完设置即时生效。"""
        if self.cfg is None:
            return {}
        return dict(self.cfg.channels.platforms or {})

    def _channel_credentials_ready(self, name: str, section: dict) -> bool:
        """该平台的凭据是否齐全（未启用、未配置的渠道也要能正确判定）。

        各平台凭据形状不同：飞书是 app_id + app_secret 两个字段，微信是扫码换来的
        bot_token（运行态而非手填），webhook 是推送地址 URL，其余历史平台用单个 token。
        """
        if name == "weixin":
            return bool(str(section.get("bot_token", "")).strip())
        if name == "feishu":
            return bool(
                str(section.get("app_id", "")).strip()
                and str(section.get("app_secret", "")).strip()
            )
        if name == "webhook":
            return bool(str(section.get("url", "")).strip())
        return bool(str(section.get("token", "")).strip())

    async def channel_status(self) -> dict:
        """渠道运行态 + 见过的来源（设置页渲染用）。"""
        cfg = self.cfg.channels if self.cfg is not None else None
        platforms = dict((cfg.platforms if cfg else {}) or {})
        manager_status = self.channels.status() if self.channels else {
            "channels": [], "supported": ["feishu", "weixin"],
        }
        # 把配置里与支持的平台都并进来：未启动、未配置的渠道也要在界面上可见可编辑，
        # 否则用户得先“添加”才能看到入口（体验上多一步且不像开关）。
        supported = set(manager_status.get("supported") or [])
        known = {c["name"] for c in manager_status["channels"]}
        for name in manager_status.get("supported") or []:
            if name not in known:
                manager_status["channels"].append({
                    "name": name,
                    "enabled": False,
                    "running": False,
                    "configured": False,
                    "error": "",
                    "seen_sources": [],
                })
                known.add(name)
        for name, section in platforms.items():
            # 只列当前版本真的支持（有适配器）的平台：升级后配置里可能残留已下线的
            # 平台（如旧的 telegram 段），把它当卡片列出来会得到一个永远启不动的
            # 死入口；这里直接跳过，不动用户的配置文件。
            if name not in supported or name in known:
                continue
            manager_status["channels"].append({
                "name": name,
                "enabled": bool(section.get("enabled", False)),
                "running": False,
                "configured": self._channel_credentials_ready(name, section),
                "error": "",
                "seen_sources": [],
            })
        # 允许名单回显（含凭据是否就绪，不回显凭据本身）
        for item in manager_status["channels"]:
            section = platforms.get(item["name"]) or {}
            item["allowed_ids"] = [str(x) for x in (section.get("allowed_ids") or [])]
            item["has_token"] = bool(str(section.get("token", "")).strip())
            item["approve_enabled"] = bool(section.get("approve_enabled", False))
            # 微信：凭据来自扫码，且会失效，界面要显示得更具体
            item["has_login"] = bool(str(section.get("bot_token", "")).strip())
            item["needs_qr"] = item["name"] == "weixin" and not item["has_login"]
            # 飞书：凭据是两个字段，界面要分别回显“已保存”而不是只认一个 token
            item["has_app_id"] = bool(str(section.get("app_id", "")).strip())
            item["has_app_secret"] = bool(str(section.get("app_secret", "")).strip())
            # Webhook：推送地址与签名密钥同样只回显“已保存”
            item["has_url"] = bool(str(section.get("url", "")).strip())
            item["has_secret"] = bool(str(section.get("secret", "")).strip())
            # 预授权名单（只读回显 + 危险工具告警）：配置里手写的 run_command
            # 等于「无人值守任意命令」，界面上必须看得见（安全审查低危项）
            allowed_tools = [str(x) for x in (section.get("allowed_tools") or [])]
            item["allowed_tools"] = allowed_tools
            item["tools_warning"] = _channel_allowed_tools_warning(allowed_tools)
        manager_status["approve_timeout"] = int(cfg.approve_timeout) if cfg else 120
        manager_status["permission_push"] = bool(cfg.permission_push) if cfg else False
        # 见过的来源合并持久化记录（重启后不丢，方便事后认领）
        if self.store is not None:
            try:
                seen = await self.store.list_channel_sources()
            except Exception:  # noqa: BLE001
                seen = []
            by_channel: dict[str, list] = {}
            for row in seen:
                by_channel.setdefault(row["channel"], []).append({
                    "actor": row["actor"], "chat_id": row["chat_id"],
                    "ts": row["last_seen"], "count": row["hits"],
                })
            for item in manager_status["channels"]:
                memory = {s["chat_id"] for s in item.get("seen_sources") or []}
                for row in by_channel.get(item["name"], []):
                    if row["chat_id"] not in memory:
                        item.setdefault("seen_sources", []).append(row)
        return manager_status

    async def channel_save(self, params: dict) -> dict:
        """保存一个平台的配置（不启停，启停走 channel_enable / channel_disable）。

        只在显式传入凭据时才覆盖已存值：界面保存允许名单时不该把凭据清掉。
        """
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("缺少平台名 name")
        # 基底取原值形态（见 _raw_platforms_base）：解不开的密文不能被这次保存抹掉
        platforms = _raw_platforms_base()
        section = dict(platforms.get(name) or {})
        if params.get("token") is not None:
            section["token"] = str(params["token"]).strip()
        # 飞书的凭据是两个字段（App ID / App Secret），不像微信那样是扫码换来的
        # 单个 bot_token。与 token 一样只在显式传入时覆盖，避免保存名单时把已存凭据清掉。
        if params.get("app_id") is not None:
            section["app_id"] = str(params["app_id"]).strip()
        if params.get("app_secret") is not None:
            section["app_secret"] = str(params["app_secret"]).strip()
        # 飞书依赖外部 lark-cli；允许显式指定路径（留空即用 PATH 查找）
        if params.get("cli_path") is not None:
            section["cli_path"] = str(params["cli_path"]).strip()
        # 通用 Webhook 的推送地址与签名密钥：与凭据同一待遇，只在显式传入时覆盖，
        # 保存其它字段（如允许名单）不会把它们清掉
        if params.get("url") is not None:
            section["url"] = str(params["url"]).strip()
        if params.get("secret") is not None:
            section["secret"] = str(params["secret"]).strip()
        if params.get("allowed_ids") is not None:
            section["allowed_ids"] = _normalize_id_list(params["allowed_ids"])
        if params.get("approve_enabled") is not None:
            section["approve_enabled"] = bool(params["approve_enabled"])
        if params.get("allowed_tools") is not None:
            raw_tools = params["allowed_tools"]
            if isinstance(raw_tools, str):
                raw_tools = [x for x in raw_tools.replace(",", "\n").splitlines()]
            section["allowed_tools"] = [str(x).strip() for x in (raw_tools or []) if str(x).strip()]
        platforms[name] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        # 预授权名单是整工具级放行：把 run_command / write_file 这类写进渠道配置，
        # 等于「无人值守时任意命令/任意写入」——保存成功时明确告警（安全审查低危项）
        warning = _channel_allowed_tools_warning(section.get("allowed_tools"))
        # 名单/凭据改动必须重建适配器才能热生效：适配器拿的是构造时那份 section，
        # 不重建的话「加入允许名单」后机器人仍用旧名单判断，消息继续被忽略——
        # 这是真实发生过的 bug（名单存进去了，机器人却永远不回话）。
        # 微信的运行态 token 由 channel_save_state 持久化，重建不会丢登录态。
        if self.channels is not None:
            await self.channels.restart()
        self._refresh_channel_gates()
        out = await self.channel_status()
        if warning:
            out["warning"] = warning
        return out

    async def channel_save_state(self, name: str, state: dict) -> None:
        """把适配器的运行时状态（微信的 bot_token / 游标）落盘。

        与 channel_save 分开：这是适配器自己触发的（扫码成功、游标推进），
        不是用户在界面上的操作。写前重读最新配置再合并，避免并发覆盖用户的改动。
        """
        if not isinstance(state, dict):
            return
        try:
            # 基底取原值形态（见 _raw_platforms_base）：游标推进这类自动落盘
            # 不得把其他平台本机解不开的密文一并抹掉
            platforms = _raw_platforms_base()
            section = dict(platforms.get(name) or {})
            for key in ("bot_token", "base_url", "cursor"):
                if key in state and state[key] is not None:
                    section[key] = str(state[key])
            platforms[name] = section
            update_config_section("channels", {"platforms": platforms})
            self.cfg = load_config()
        except Exception as e:  # noqa: BLE001 - 状态落盘失败不应影响对话
            logger.warning("渠道 %s 状态落盘失败：%s", name, e)

    # ---- 微信扫码登录（交互式流程，桌面端驱动） ----

    async def channel_weixin_login_start(self) -> dict:
        """生成微信登录二维码。"""
        channel = self._channel_obj("weixin")
        if channel is None:
            # 尚未建渠道实例（未启用过）时临时建一个，只用于走登录流程
            from ...channels.weixin import WeixinChannel

            section = dict((self.cfg.channels.platforms or {}).get("weixin") or {})
            channel = WeixinChannel(section, lambda _m: None)
            self._weixin_login_channel = channel
        info = await channel.fetch_qrcode()
        return {
            "qrcode": info["qrcode"],
            "url": info["url"],
            "note": "用手机微信扫码并在手机上确认，确认后点下方「我已完成扫码」。",
        }

    async def channel_weixin_login_poll(self, params: dict) -> dict:
        """轮询扫码状态；confirmed 时保存凭据并重建渠道。"""
        qrcode = str(params.get("qrcode", "")).strip()
        if not qrcode:
            raise RuntimeError("缺少 qrcode")
        channel = self._channel_obj("weixin") or getattr(self, "_weixin_login_channel", None)
        if channel is None:
            raise RuntimeError("微信渠道尚未初始化，请先点「生成二维码」")
        result = await channel.poll_qrcode(
            qrcode, verify_code=str(params.get("verify_code", "") or "")
        )
        if result.get("status") != "confirmed":
            return result
        token = str(result.get("bot_token") or "").strip()
        if not token:
            raise RuntimeError(
                "服务器返回已确认，但没有拿到 bot_token（响应结构已记入引擎日志），"
                "请重新生成二维码再试一次；仍失败请带日志反馈"
            )
        base_url = str(result.get("base_url") or "")
        # 落盘（含 base_url，服务器可能下发不同的接入点）
        await self.channel_save_state("weixin", {
            "bot_token": token, **({"base_url": base_url} if base_url else {}),
        })
        if channel is not None:
            await channel.apply_login(token, base_url)
        # 重建渠道使新 token 生效
        if self.channels is not None:
            await self.channels.restart()
        self._refresh_channel_gates()
        return {"status": "confirmed", "saved": True}

    async def channel_weixin_logout(self) -> dict:
        """清除微信登录态（用户主动退出或 token 失效后重登）。"""
        # 基底取原值形态（见 _raw_platforms_base）：退出微信只清微信自己的
        # 登录态，其他平台解不开的密文不能被连带抹掉
        platforms = _raw_platforms_base()
        section = dict(platforms.get("weixin") or {})
        section.pop("bot_token", None)
        section.pop("cursor", None)
        platforms["weixin"] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        if self.channels is not None:
            await self.channels.restart()
        self._refresh_channel_gates()
        return await self.channel_status()

    def _channel_obj(self, name: str):
        if self.channels is None:
            return None
        return self.channels.channels.get(name)

    async def channel_enable(self, params: dict) -> dict:
        """启用一个平台并立即重建渠道（不再要求重启整个应用）。

        只校验凭据（飞书是手填的 App ID + App Secret，微信是扫码换来的 bot_token）。
        允许名单**允许为空**：chat id 只能由运行中的机器人记进「发现的来源」，
        不先启用就永远拿不到第一条消息——这里的名单检查曾把首次配置锁死。
        空名单的安全语义（拒绝一切、未授权只记录不回复）由消息层强制，不在这一步。
        """
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("缺少平台名 name")
        # 基底取原值形态（见 _raw_platforms_base）：启用开关的落盘不得抹掉密文
        platforms = _raw_platforms_base()
        section = dict(platforms.get(name) or {})
        if name == "weixin":
            if not str(section.get("bot_token", "")).strip():
                raise RuntimeError("微信还没登录：先在下方点「生成二维码」并扫码确认")
        elif name == "feishu":
            if not str(section.get("app_id", "")).strip():
                raise RuntimeError("飞书还没填 App ID，先填好再启用")
            if not str(section.get("app_secret", "")).strip():
                raise RuntimeError("飞书还没填 App Secret，先填好再启用")
        elif name == "webhook":
            if not str(section.get("url", "")).strip():
                raise RuntimeError("Webhook 还没填推送地址（URL），先填好再启用")
        elif not str(section.get("token", "")).strip():
            raise RuntimeError(f"「{name}」还没填凭据，先填好再启用")
        # 注意：这里**不能**再要求允许名单非空。chat id 只能由运行中的机器人
        # 收到第一条消息后记进「发现的来源」（见 manager.note_seen），而机器人
        # 只有启用后才会轮询——先启用再认领是唯一能走通的顺序，把名单检查
        # 放在这里就是一个引导死锁（永远拿不到第一个 chat id）。
        # 安全性不受影响：空名单 = 拒绝一切由消息层的 manager._on_message 强制，
        # 未授权来源只记录不回复，机器人跑着也不会应答陌生人。
        section["enabled"] = True
        platforms[name] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        await self.channels.restart()
        self._refresh_channel_gates()
        return await self.channel_status()

    async def channel_disable(self, params: dict) -> dict:
        name = str(params.get("name", "")).strip()
        if not name:
            raise RuntimeError("缺少平台名 name")
        # 基底取原值形态（见 _raw_platforms_base）：停用开关的落盘不得抹掉密文
        platforms = _raw_platforms_base()
        section = dict(platforms.get(name) or {})
        section["enabled"] = False
        platforms[name] = section
        update_config_section("channels", {"platforms": platforms})
        self.cfg = load_config()
        await self.channels.restart()
        self._refresh_channel_gates()
        return await self.channel_status()

    async def channel_set_timeout(self, params: dict) -> dict:
        value = max(10, min(3600, int(params.get("approve_timeout") or 120)))
        update_config_section("channels", {"approve_timeout": value})
        self.cfg = load_config()
        return await self.channel_status()

    async def channel_set_permission_push(self, params: dict) -> dict:
        """开关「权限请求推送到渠道」（P21 剩余半边，默认关）。

        只改配置里的提醒开关，不碰门语义：权限请求照旧在桌面端确认，渠道侧的
        消息里没有任何放行/拒绝入口。关掉时连日志都不留，避免把「用户此刻在等
        确认」这类信息无谓地写进日志。
        """
        value = bool(params.get("enabled"))
        update_config_section("channels", {"permission_push": value})
        self.cfg = load_config()
        return await self.channel_status()

    def _fire_permission_push(self, ev: dict) -> None:
        """权限请求事件处的接线：开关开且渠道管理器就绪才推，全程 fire-and-forget。

        放在 emit 链路的旁路上，不 await——权限请求本身要立刻挂起等用户决策，
        推送慢（飞书子进程、webhook 超时）绝不能拖慢确认卡出现的时机。
        """
        cfg = getattr(self, "cfg", None)
        channels_cfg = getattr(cfg, "channels", None) if cfg is not None else None
        if not channels_cfg or not bool(getattr(channels_cfg, "permission_push", False)):
            return
        mgr = getattr(self, "channels", None)
        if mgr is None:
            return
        try:
            spawn_bg(push_permission_request(mgr, getattr(self, "store", None), ev))
        except RuntimeError:
            pass  # 无事件循环（如纯测试环境）

    async def channel_test(self, params: dict) -> dict:
        """试发一条消息，验证 token 与 chat_id 是否都对。

        纯出站渠道（webhook）没有 chat_id 概念：不填也放行，直接对配置的
        URL 试发一条。
        """
        name = str(params.get("name", "")).strip()
        chat_id = str(params.get("chat_id", "")).strip()
        if not name:
            raise RuntimeError("需要平台名与 chat_id")
        if self.channels is None:
            raise RuntimeError("渠道管理器尚未就绪")
        channel = self.channels.channels.get(name)
        if channel is None:
            raise RuntimeError(f"平台「{name}」还没启用")
        if not chat_id and not getattr(channel, "pure_outbound", False):
            raise RuntimeError("需要平台名与 chat_id")
        ok = await channel.send_text(chat_id, "🐑 SkySheep 测试消息：这条能收到，说明配置通了。")
        return {"ok": ok, "error": channel.error}

    # ---- 渠道会话与运行（ChannelManager 的回调面） ----

    async def channel_ensure_session(self, channel_name: str) -> str:
        """取（或建）某个渠道绑定的会话。不切活动会话指针——渠道与桌面可并行。

        渠道会话归属「产生它时桌面所在的项目」（用户在渠道里操作的就是桌面
        当时的项目，对话应归到那个项目名下）：跟随当前项目——桌面切了项目，
        渠道下一条消息会在新项目下开新会话（旧会话留在原项目可查）；无项目
        态落快聊。绑定指向的会话必须属于当前项目：项目切换/会话删除都会走
        到下方的自愈路径重开，而不是把别的项目的会话挂进渠道的门控下
        （归属校验，同 B 族）。工作目录随归属（真实项目→项目目录）。
        """
        target_pid = self._cur_project_id()
        bound = await self.store.get_channel_binding(channel_name)
        if bound and await self.store.get_session_for_project(bound, target_pid) is not None:
            # 绑定存在但 runtime 可能不在：重启后首次使用、或该会话被桌面端切走时。
            # 这里必须补建，否则 channel_run 会因「会话不存在」直接失败。
            await self._get_channel_runtime(bound, channel_name)
            return bound
        sess = await self.store.create_session(target_pid, title=f"🤖 {channel_name}")
        await self.store.set_channel_binding(channel_name, sess.id)
        await self._get_channel_runtime(sess.id, channel_name)
        return sess.id

    async def channel_new_session(self, channel_name: str) -> str:
        """给渠道开一个新会话（/new），旧的保留可查。归当前项目（无项目态落快聊）。"""
        sess = await self.store.create_session(self._cur_project_id(), title=f"🤖 {channel_name}")
        await self.store.set_channel_binding(channel_name, sess.id)
        await self._get_channel_runtime(sess.id, channel_name)
        return sess.id

    async def _get_channel_runtime(self, session_id: str, channel_name: str) -> SessionRuntime:
        """渠道会话的 runtime：用 ChannelGate 而非默认门控。

        与 _get_runtime 的差别只在 gate——渠道的权限姿态必须由渠道决定：
        默认门控会产出 PermissionRequest 并无限期等前端，渠道场景下没有前端。
        工作目录随会话归属（_session_project）：归真实项目就在项目目录里干活，
        旧「远程连接」会话仍在数据目录 remote-control 文件夹、快聊在 default。
        门控姿态不随项目变：只读 + 渠道 allowed_tools 放行，写/执行走聊天
        审批或拒绝；项目白名单不随渠道会话生效（无人值守口径，写操作必须
        经聊天窗口逐次确认或渠道级预授权）。
        """
        rt = self.runtimes.get(session_id)
        if rt is not None:
            return rt
        project_id, workdir = await self._session_project(session_id)
        section = dict((self.cfg.channels.platforms or {}).get(channel_name) or {})
        gate = ChannelGate(
            allowed=list(section.get("allowed_tools") or []),
            approve_enabled=bool(section.get("approve_enabled", False)),
            approve_timeout=int(self.cfg.channels.approve_timeout),
            store=self.store,
            project_id=project_id,
            working_dir=workdir,
        )
        # notify 绑定建好的 gate：审批卡优先回本轮发起消息所在的聊天（审查 P3-15）
        gate.notify = lambda pending, _g=gate: self._notify_channel_approval(
            channel_name, pending, _g
        )
        recorder = ChangeRecorder()
        rt = SessionRuntime(
            sid=session_id,
            agent=self._build_agent(
                provider=self.provider,
                gate=gate,
                working_dir=workdir,
                session_id=session_id,
                recorder=recorder,
            ),
            recorder=recorder,
        )
        await self._reload_agent_history(rt.agent, session_id)
        self.runtimes[session_id] = rt
        self._channel_gates[session_id] = gate
        self._channel_names[session_id] = channel_name
        return rt

    def _refresh_channel_gates(self) -> None:
        """配置保存/启停后刷新已缓存渠道会话的无人值守门控（审查 P2-11）。

        门控参数（approve_enabled / allowed_tools）是会话首次使用时从配置快照
        构造的：不刷新的话，改完配置要等新渠道会话（或重启）才生效，窗口期内
        界面显示与实际放行口径不一致。正在跑的轮次不动（换门会丢掉在等的审批），
        空闲会话就地换新门；新会话本就走 _get_channel_runtime 重建，不受影响。
        """
        for sid, name in list(self._channel_names.items()):
            rt = self.runtimes.get(sid)
            if rt is None:
                continue
            run_task = getattr(rt, "run_task", None)
            if run_task is not None and not run_task.done():
                continue
            old = self._channel_gates.get(sid)
            section = dict((self.cfg.channels.platforms or {}).get(name) or {})
            gate = ChannelGate(
                allowed=list(section.get("allowed_tools") or []),
                approve_enabled=bool(section.get("approve_enabled", False)),
                approve_timeout=int(self.cfg.channels.approve_timeout),
                store=self.store,
                project_id=getattr(old, "project_id", None),
                working_dir=getattr(old, "working_dir", None),
            )
            gate.notify = lambda pending, _g=gate, _n=name: self._notify_channel_approval(
                _n, pending, _g
            )
            rt.agent.gate = gate
            self._channel_gates[sid] = gate

    async def _notify_channel_approval(self, channel_name: str, pending, gate=None) -> None:
        """把审批卡片推到聊天窗口。

        gate 带 turn_chat_id（本轮发起消息所在的聊天）时优先回它——回信地址
        若取「最近一条入站消息」，发起轮之后其它名单内聊天来一条消息就会把
        卡片带偏（审查 P3-15）。
        """
        if self.channels is None:
            return
        chat_id = str(getattr(gate, "turn_chat_id", "") or "") or self._channel_last_chat.get(
            channel_name, ""
        )
        channel = self.channels.channels.get(channel_name)
        if not chat_id or channel is None:
            return
        lines = [
            "⚠ 需要你确认一个操作",
            "",
            f"工具：{pending.tool_name}",
            f"内容：{pending.detail}",
        ]
        if pending.diff:
            lines += ["", "改动预览：", "```", pending.diff[:1500], "```"]
        lines += [
            "",
            # 渠道端没有「总是允许」：白名单规则是持久化的，从聊天窗口写入后
            # 所有渠道会话都不再询问，代价与便利不成比例（ChannelGate 里强制
            # 降级为单次），需要预授权时在桌面端本机界面操作。
            "回复 allow（允许一次）或 deny（拒绝），只有发起这一轮的账号能决定；"
            f"超过 {self.cfg.channels.approve_timeout} 秒未回复会自动拒绝。",
        ]
        try:
            await channel.send_text(chat_id, "\n".join(lines))
        except Exception as e:  # noqa: BLE001 - 推卡片失败由 Gate 退化成拒绝
            logger.warning("推送审批卡片失败：%s", e)
            raise

    async def channel_run(self, session_id: str, text: str, actor: str = "",
                          chat_id: str = "") -> dict:
        """跑一轮渠道对话，返回回复文本。不劫持活动会话。

        actor 是这一轮的发起人（渠道消息里的 sender 标识）：写进门控后，
        本轮的审批决定只认他，群聊里其他成员的 allow/deny 不生效。
        chat_id 是发起消息所在的聊天：审批卡回这个聊天，而不是「最近一条
        入站消息」的聊天（审查 P3-15）。
        """
        if self.provider is None:
            return {"error": "尚未配置可用的模型 API Key"}
        rt = self.runtimes.get(session_id)
        if rt is None:
            # 自愈：runtime 可能尚未建立（重启后首次、或已被回收）。反查渠道绑定补建，
            # 而不是直接把「会话不存在」丢给聊天窗口——用户看到这句无从下手。
            channel_name = self._channel_names.get(session_id, "")
            if not channel_name:
                try:
                    channel_name = await self.store.find_channel_by_session(session_id) or ""
                except Exception:  # noqa: BLE001 - 反查失败退化成下方提示
                    channel_name = ""
            if not channel_name:
                return {"error": "这个会话已不是渠道会话了，发送 /new 开一个新的"}
            # 自愈前先确认会话还在（可能被删）：归属不再按固定项目检查——
            # 渠道会话归「产生它时所在的项目」，会话自身携带归属，runtime
            # 与门控都按它解析（_get_channel_runtime）。
            if await self.store.get_session(session_id) is None:
                return {"error": "这个会话已不存在，发送 /new 开一个新的"}
            rt = await self._get_channel_runtime(session_id, channel_name)
        # 本轮发起人写进门控：审批决定只认他（见 ChannelGate.submit_latest）。
        gate = self._channel_gates.get(session_id)
        if gate is not None:
            gate.turn_actor = str(actor or "")
            gate.turn_chat_id = str(chat_id or "")
        collected: list[str] = []
        think_text = ""
        think_ms = 0
        turn_ms = 0

        async def collect(ev: dict) -> None:
            nonlocal think_text, think_ms, turn_ms
            if ev.get("kind") != "assistant_message":
                return
            msg = ev.get("message")
            if not isinstance(msg, dict):
                return
            text_out = msg.get("text", "")
            if text_out:
                collected.append(str(text_out))
            # 思考与耗时只在最终回答上取（中间迭代是过程消息，没有耗时）
            if msg.get("duration_ms"):
                turn_ms = int(msg.get("duration_ms") or 0)
                think_ms = int(msg.get("thinking_ms") or 0)
                think_text = "".join(
                    str(b.get("text", "")) for b in (msg.get("content") or [])
                    if isinstance(b, dict) and b.get("type") == "thinking"
                )

        try:
            result = await self._run_turn_pipeline(
                text, collect, plan_mode=False, runtime=rt, session_id=session_id,
            )
        except asyncio.CancelledError:
            return {"error": "这一轮被中断"}
        except Exception as e:  # noqa: BLE001 - 把原因回给聊天窗口
            return {"error": str(e)}
        if result.get("stopped") and not collected:
            return {"error": "这一轮被中断"}
        # assistant_message 事件带的是完整消息；没有则回落到历史里最后一条助手文本
        reply = collected[-1] if collected else ""
        if not reply:
            for m in reversed(rt.agent.history):
                if m.role == "assistant" and m.text.strip():
                    reply = m.text.strip()
                    if getattr(m, "duration_ms", 0):
                        turn_ms = int(m.duration_ms)
                        think_ms = int(m.thinking_ms)
                        think_text = m.thinking
                    break
        return {
            "text": reply,
            "session_id": session_id,
            # 渠道端也透出思考与用时（遥控时看不到桌面界面，这是唯一的进度感）
            "thinking_chars": len(think_text),
            "thinking_ms": think_ms,
            "duration_ms": turn_ms,
        }

    async def channel_stop(self, session_id: str) -> None:
        self.cancel_run(session_id)

    async def channel_status_text(self, session_id: str) -> str:
        """给 /status 命令用的简短状态。"""
        sess = await self.store.get_session(session_id)
        title = (sess.title if sess else "") or "（无标题）"
        if sess is not None and sess.project_id is None:
            proj_name, workdir = "快聊", self._quick_workdir()
        elif sess is not None:
            proj = await self.store.get_project(sess.project_id)
            proj_name = f"「{proj.name}」" if proj else "未知项目"
            _, workdir = await self._session_project(session_id)
        else:
            proj_name, workdir = "未知项目", None
        return (
            f"会话：{title}\n"
            f"模型：{self.provider_name or '未配置'} / {self.provider_model or '-'}\n"
            f"项目：{proj_name}（渠道会话归产生它的项目）\n"
            f"工作目录：{workdir if workdir is not None else '无'}\n"
            f"上下文：{self._context_limit():,} tokens"
        )

    async def channel_list_sessions(self) -> list[dict]:
        """渠道端 /sessions：只列渠道自己建的会话（审查 P3-14）。

        渠道会话归产生它的项目、散在各项目下，不再有一个专属项目可查；
        按创建时的 🤖 标题前缀识别（渠道会话标题自动生成不覆盖它，
        _auto_title 只在 wants_title 时跑，渠道路径不触发）。桌面用户自己的
        会话清单依然不透给渠道侧。
        """
        rows = [
            s
            for s in await self.store.list_sessions()  # 全量后按前缀过滤，量级可控
            if (s.title or "").startswith("🤖")
        ]
        current_ids = set(self._channel_gates.keys())
        return [
            {
                "id": s.id,
                "title": s.title,
                "current": s.id in current_ids,
            }
            for s in rows
        ]

    async def channel_submit_decision(
        self, channel_name: str, decision: str, actor: str = ""
    ) -> dict:
        """把聊天窗口的审批回复投给等待中的门控。

        返回 ``{"hit": bool, "actor_mismatch": bool}``：hit=True 表示决定已生效；
        actor_mismatch=True 表示有待决策项、但回复者不是这一轮的发起人（群聊
        场景），决定不生效——调用方据此提示，而不是把 "allow" 当新消息再跑一轮。
        """
        session_id = await self.store.get_channel_binding(channel_name)
        if not session_id:
            return {"hit": False}
        gate = self._channel_gates.get(session_id)
        if gate is None or not gate.waiting:
            return {"hit": False}
        expected = gate.turn_actor or ""
        actor_s = str(actor or "")
        # 与 gate.submit_latest 同一收紧（审查 S-08 + P3-17）：发起人 id 是
        # 校验的根本依据，任一侧为空都视为不匹配——「绑定或回复缺 id」不再
        # 退回旧行为（否则丢失 actor 的消息可替发起人批准）。
        if not expected or expected != actor_s:
            return {"hit": False, "actor_mismatch": True}
        return {"hit": bool(gate.submit_latest(decision, actor))}

    async def channel_has_waiting_decision(self, channel_name: str) -> bool:
        """该渠道是否有等待中的审批（manager 用它区分「回应确认卡」与普通消息）。"""
        session_id = await self.store.get_channel_binding(channel_name)
        if not session_id:
            return False
        gate = self._channel_gates.get(session_id)
        return bool(gate is not None and gate.waiting)

    def note_channel_chat(self, channel_name: str, chat_id: str) -> None:
        self._channel_last_chat[channel_name] = chat_id
