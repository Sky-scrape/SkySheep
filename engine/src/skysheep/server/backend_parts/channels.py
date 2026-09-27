"""聊天软件渠道（Bot Channel）：飞书 / 微信接入桌面 Agent 的设置、会话与运行。

从 backend.py 按职责注释整段搬入：渠道预授权告警名单等模块级小件随职责区
落在本模块，backend 引用以保持旧命名空间可见（测试直接 import）。
方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ...channels import ChannelGate
from ...config import load_config, update_config_section
from ...core import Agent
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
        bot_token（运行态而非手填），其余历史平台用单个 token。
        """
        if name == "weixin":
            return bool(str(section.get("bot_token", "")).strip())
        if name == "feishu":
            return bool(
                str(section.get("app_id", "")).strip()
                and str(section.get("app_secret", "")).strip()
            )
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
            # 预授权名单（只读回显 + 危险工具告警）：配置里手写的 run_command
            # 等于「无人值守任意命令」，界面上必须看得见（安全审查低危项）
            allowed_tools = [str(x) for x in (section.get("allowed_tools") or [])]
            item["allowed_tools"] = allowed_tools
            item["tools_warning"] = _channel_allowed_tools_warning(allowed_tools)
        manager_status["approve_timeout"] = int(cfg.approve_timeout) if cfg else 120
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
        platforms = dict(self.cfg.channels.platforms or {})
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
            fresh = load_config()
            platforms = dict(fresh.channels.platforms or {})
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
        fresh = load_config()
        platforms = dict(fresh.channels.platforms or {})
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
        platforms = dict(self.cfg.channels.platforms or {})
        section = dict(platforms.get(name) or {})
        if name == "weixin":
            if not str(section.get("bot_token", "")).strip():
                raise RuntimeError("微信还没登录：先在下方点「生成二维码」并扫码确认")
        elif name == "feishu":
            if not str(section.get("app_id", "")).strip():
                raise RuntimeError("飞书还没填 App ID，先填好再启用")
            if not str(section.get("app_secret", "")).strip():
                raise RuntimeError("飞书还没填 App Secret，先填好再启用")
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
        platforms = dict(self.cfg.channels.platforms or {})
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

    async def channel_test(self, params: dict) -> dict:
        """试发一条消息，验证 token 与 chat_id 是否都对。"""
        name = str(params.get("name", "")).strip()
        chat_id = str(params.get("chat_id", "")).strip()
        if not name or not chat_id:
            raise RuntimeError("需要平台名与 chat_id")
        if self.channels is None:
            raise RuntimeError("渠道管理器尚未就绪")
        channel = self.channels.channels.get(name)
        if channel is None:
            raise RuntimeError(f"平台「{name}」还没启用")
        ok = await channel.send_text(chat_id, "🐑 SkySheep 测试消息：这条能收到，说明配置通了。")
        return {"ok": ok, "error": channel.error}

    # ---- 渠道会话与运行（ChannelManager 的回调面） ----

    async def channel_ensure_session(self, channel_name: str) -> str:
        """取（或建）某个渠道绑定的会话。不切活动会话指针——渠道与桌面可并行。

        渠道会话固定归到「远程连接」项目（不落当前项目或快聊）：渠道对话的
        工作目录与桌面项目无关。绑定指向的会话必须属于该项目：会话可能被
        桌面端移到别的项目或删除，此时并到下方的自愈路径重开一个，而不是把
        别的项目的会话挂进渠道的工作目录与门控下（归属校验，同 B 族）。
        """
        remote_pid = await self._remote_project_id()
        bound = await self.store.get_channel_binding(channel_name)
        if bound and await self.store.get_session_for_project(bound, remote_pid) is not None:
            # 绑定存在但 runtime 可能不在：重启后首次使用、或该会话被桌面端切走时。
            # 这里必须补建，否则 channel_run 会因「会话不存在」直接失败。
            await self._get_channel_runtime(bound, channel_name)
            return bound
        sess = await self.store.create_session(remote_pid, title=f"🤖 {channel_name}")
        await self.store.set_channel_binding(channel_name, sess.id)
        await self._get_channel_runtime(sess.id, channel_name)
        return sess.id

    async def channel_new_session(self, channel_name: str) -> str:
        """给渠道开一个新会话（/new），旧的保留可查。固定挂在「远程连接」项目下。"""
        remote_pid = await self._remote_project_id()
        sess = await self.store.create_session(remote_pid, title=f"🤖 {channel_name}")
        await self.store.set_channel_binding(channel_name, sess.id)
        await self._get_channel_runtime(sess.id, channel_name)
        return sess.id

    async def _get_channel_runtime(self, session_id: str, channel_name: str) -> SessionRuntime:
        """渠道会话的 runtime：用 ChannelGate 而非默认门控。

        与 _get_runtime 的差别只在 gate——渠道的权限姿态必须由渠道决定：
        默认门控会产出 PermissionRequest 并无限期等前端，渠道场景下没有前端。
        工作目录与白名单归属都走「远程连接」项目（渠道对话与桌面工作目录无关；
        文件/命令类工具本就在 ChannelGate 允许清单默认拒绝之列）。
        """
        rt = self.runtimes.get(session_id)
        if rt is not None:
            return rt
        remote_pid = await self._remote_project_id()
        remote_dir = Path.home()
        section = dict((self.cfg.channels.platforms or {}).get(channel_name) or {})
        gate = ChannelGate(
            allowed=list(section.get("allowed_tools") or []),
            approve_enabled=bool(section.get("approve_enabled", False)),
            approve_timeout=int(self.cfg.channels.approve_timeout),
            store=self.store,
            project_id=remote_pid,
            working_dir=remote_dir,
        )
        # notify 绑定建好的 gate：审批卡优先回本轮发起消息所在的聊天（审查 P3-15）
        gate.notify = lambda pending, _g=gate: self._notify_channel_approval(
            channel_name, pending, _g
        )
        recorder = ChangeRecorder()
        rt = SessionRuntime(
            sid=session_id,
            agent=Agent(
                provider=self.provider,
                registry=self._build_full_registry(recorder),
                gate=gate,
                working_dir=remote_dir,
                max_iterations=self.cfg.max_iterations,
                context_limit_tokens=self._context_limit(),
                compaction_keep_recent=self.cfg.compaction_keep_recent,
                compaction_trigger=self.cfg.compaction_trigger,
                compaction_auto=self.cfg.compaction_auto,
                hooks=self.hooks,
                restrict_to_workdir=self.cfg.restrict_to_workdir,
                session_id=session_id,
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
            # 自愈前先验归属：会话被移到别的项目后不能挂回渠道执行（同 B 族）。
            # 渠道会话的归属项目是「远程连接」，不再随当前项目走。
            if await self.store.get_session_for_project(
                session_id, await self._remote_project_id()
            ) is None:
                return {"error": "这个会话已不在「远程连接」项目里，发送 /new 开一个新的"}
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
        return (
            f"会话：{title}\n"
            f"模型：{self.provider_name or '未配置'} / {self.provider_model or '-'}\n"
            "项目：远程连接（渠道会话的固定项目）\n"
            f"上下文：{self._context_limit():,} tokens"
        )

    async def channel_list_sessions(self) -> list[dict]:
        """渠道端 /sessions：只列「远程连接」项目的会话（审查 P3-14）。

        会话标题默认由首条消息生成，属于桌面用户的输入内容——桌面当前项目
        的会话清单不该透给渠道侧（那里只该看到渠道自己的会话）。
        """
        remote_pid = await self._remote_project_id()
        rows = (
            await self.store.list_sessions(remote_pid)
            if remote_pid is not None
            else []
        )
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
