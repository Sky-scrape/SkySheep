"""用户偏好与通知：ui.json 读写清洗、界面/布局常量、Windows 系统通知（toast）。

从 backend.py 按职责注释整段搬入。方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ...config import skysheep_home
from ...textio import write_text_atomic


class PreferencesMixin:
    """界面偏好（ui.json）、通知中心日志与 Windows 系统通知。

    方法自 backend.py 按职责注释逐字搬入。
    """

    # ---- 界面偏好（手调布局：侧栏宽 / 输入区高，前端拖拽后落盘 ui.json） ----
    # pywebview 默认 private_mode，前端 localStorage 每次启动都会清空，所以存后端文件。

    UI_PREFS_LIMITS = {
        "sidebar_w": (200, 460),
        "composer_h": (74, 520),
        # 分屏列输入区高（独立于主栏 composer_h；null/缺省 = 74）
        "split_composer_h": (74, 520),
        # 分屏宽度（此前漏登记，ui.save 把它当未知键丢弃，宽度从不持久化）
        "split_w": (300, 1100),
        "right_w": (240, 720),
        "right_collapsed": (0, 1),  # 右侧面板是否收起（1=收起，标签列表保留）
        "left_collapsed": (0, 1),  # 左侧栏是否折叠（1=折叠，折叠钮/Ctrl+B 切换）
        "ui_scale": (70, 120),  # 界面缩放百分比（存 70–120 的整数，100 = 默认大小）
        "notify": (0, 1),  # Windows 系统通知开关（1=开，默认开）
        "pet": (0, 1),  # 对话区宠物「云朵小羊」开关（1=显示，默认显示）
        # 空白会话的欢迎卡片（1=显示，默认显示）：设置 · 界面与通知 里可关
        "welcome_card": (0, 1),
        "accept_edits": (0, 2),  # 分级权限模式：0=安全执行 1=自动编辑（写入免确认）2=完全访问
        # 宠物拖放位置（#chat 内布局像素）；上限给足，前端拖拽时已按容器收敛
        "pet_x": (0, 4000),
        "pet_y": (0, 4000),
        # 首次启动配置向导已完成标记（1=完成，不再自动弹出）
        "onboarded": (0, 1),
        # 自动检查更新（1=开，默认开）：关于页开关，关掉后启动不再请求 GitHub
        "update_check": (0, 1),
        # 发送键：1 = Ctrl+Enter 发送、Enter 换行（默认 0 = Enter 发送）
        "ctrl_enter_send": (0, 1),
        # 通知提示音（1=开，默认关）：任务完成 / 等确认时 WebAudio 合成短音
        "notify_sound": (0, 1),
        # 提示音作用域（1=仅窗口失焦时响，默认 1）：前台人已在看，响一声反而吵
        "notify_sound_focus": (0, 1),
        # 通知类型开关（1=开，默认开）：关掉的类型不弹系统通知、不响提示音，
        # 仍进应用内通知中心（那里是「错过提醒」的聚合日志，不该有死角）
        "notify_kind_done": (0, 1),
        "notify_kind_perm": (0, 1),
        # 对话区宠物大小百分比（60–140，100 = 默认 76px 宽；null/缺省 = 默认）
        "pet_scale": (60, 140),
        # 阅读行宽：对话区消息卡最大宽度 px（680–1400；null/缺省 = 主题默认 880）
        "read_width": (680, 1400),
        # 浏览器面板自适应（1=整页缩到面板宽，缺省；0=原始大小，横向滚动）
        "browser_fit": (0, 1),
        # 浏览器页面缩放百分比（50–300，Ctrl+滚轮步进；null/缺省 = 100%）
        "browser_page_zoom": (50, 300),
        # 当前项目 id（0 = 无项目态）：后端在切项目/删项目/首启建项目时写入，
        # 重启后回到同一个状态；上限给足任意合法 SQLite rowid
        "active_project": (0, 2_147_483_647),
    }
    # 右侧面板：打开了哪些标签、激活的是哪个（id 白名单见前端 TAB_META）。
    # 标签分「容器」与「分段」两层：任务是子代理任务/任务清单/项目任务的容器，
    # 自动化是定时任务/任务编排的容器。两层 id 都收——right_tabs 只写容器，但
    # right_active 写的是分段 id，且旧 ui.json 里整份都是分段 id（前端按 TAB_OF
    # 映射回容器）。漏收哪个，升级后面板就会静默少一个标签（ptasks/pipeline
    # 在合并前就漏收过，一并补上）；terminal 是更早的终端标签，留着不碍事。
    RIGHT_TAB_IDS = (
        "aux", "review", "browser", "files", "tasks", "agenda", "auto", "memory", "ext",
        "todo", "ptasks", "cron", "pipeline", "terminal",
    )
    # 会话标签恢复：session.tabs 存 sid 数组（与 tab_order 同款上限），
    # session.active 存激活的 sid。都在前端写（标签开/关/切换时），
    # snapshot() 读出来校验归属后下发给前端恢复。
    SESSION_ACTIVE_KEY = "session_active"
    SESSION_TABS_MAX = 200
    # 新会话默认模型（ui.json 的 default_model）：provider 键用字符串白名单校验
    # （只能是已配置的服务名），模型名随 provider 一起存进 value（"name::model"）
    DEFAULT_MODEL_PROVIDER_KEY = "default_model_provider"

    def _ui_prefs_path(self) -> Path:
        return skysheep_home() / "ui.json"

    @staticmethod
    def _clean_notif_log(val) -> list[dict]:
        """清洗通知中心日志：字段白名单 + 截断，封顶 60 条；无有效条目返回空表。

        前端存的是 [{ts, title, body, kind}]（sid 等跳转元数据只活在内存里，
        跨进程无意义不收）；title 空的条目是脏数据，直接丢弃。
        """
        if not isinstance(val, list):
            return []
        cleaned: list[dict] = []
        for item in val:
            if not isinstance(item, dict):
                continue
            entry = {
                "ts": float(item.get("ts") or 0.0),
                "title": str(item.get("title") or "")[:120],
                "body": str(item.get("body") or "")[:300],
                "kind": str(item.get("kind") or "")[:16],
            }
            if entry["title"]:
                cleaned.append(entry)
            if len(cleaned) >= 60:
                break
        return cleaned

    def _read_ui_prefs(self) -> dict:
        p = self._ui_prefs_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        prefs = {
            k: int(data[k])
            for k in self.UI_PREFS_LIMITS
            if isinstance(data.get(k), int) and not isinstance(data.get(k), bool)
        }
        tabs = data.get("right_tabs")
        if isinstance(tabs, list):
            cleaned: list[str] = []
            for t in tabs:
                if t in self.RIGHT_TAB_IDS and t not in cleaned:
                    cleaned.append(t)
            if cleaned:
                prefs["right_tabs"] = cleaned
        active = data.get("right_active")
        if isinstance(active, str) and active in self.RIGHT_TAB_IDS:
            prefs["right_active"] = active
        order = data.get("project_order")
        if isinstance(order, list) and all(
            isinstance(x, int) and not isinstance(x, bool) and x > 0 for x in order
        ) and order:
            prefs["project_order"] = order
        qpos = data.get("quick_pos")
        if qpos in ("top", "bottom"):
            prefs["quick_pos"] = qpos
        elif isinstance(qpos, dict):
            pid = qpos.get("before")
            if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
                prefs["quick_pos"] = {"before": pid}
        sorder = data.get("session_order")
        if isinstance(sorder, dict) and sorder:
            cleaned_sorder: dict[str, list[str]] = {}
            for gk, ids in sorder.items():
                gks = str(gk)
                if not gks or not isinstance(ids, list):
                    continue
                gseen = [x for x in ids if isinstance(x, str) and x]
                if gseen:
                    cleaned_sorder[gks] = gseen
            if cleaned_sorder:
                prefs["session_order"] = cleaned_sorder
        torder = data.get("tab_order")
        if isinstance(torder, list) and torder and all(
            isinstance(x, str) and x for x in torder
        ):
            seen: set[str] = set()
            deduped = [x for x in torder if not (x in seen or seen.add(x))]
            prefs["tab_order"] = deduped
        nlog = self._clean_notif_log(data.get("notif_log"))
        if nlog:
            prefs["notif_log"] = nlog
        # 自由字符串键（启动恢复 / 新会话默认模型 / 全局热键）：
        # 读取时与写入同一套清洗（session_tabs 去重封顶，其余非空字符串直取）
        stabs = data.get("session_tabs")
        if isinstance(stabs, list) and stabs:
            seen_st: set[str] = set()
            deduped_st = [x for x in stabs
                          if isinstance(x, str) and x and not (x in seen_st or seen_st.add(x))]
            if deduped_st:
                prefs["session_tabs"] = deduped_st[: self.SESSION_TABS_MAX]
        for key in ("session_active", "hotkey", "default_model_name",
                    "default_model_provider", "aux_model_name", "aux_model_provider"):
            v = data.get(key)
            if isinstance(v, str) and v:
                prefs[key] = v
        for key, allowed in self.STRING_PREFS.items():
            val = data.get(key)
            if isinstance(val, str) and val in allowed:
                prefs[key] = val
        return prefs

    # ---- Windows 系统通知（winotify toast；失败静默，非 Windows 平台不可用） ----

    def notify_enabled(self) -> bool:
        try:
            return self._read_ui_prefs().get("notify", 1) == 1
        except Exception:  # noqa: BLE001
            return True

    async def notify(self, params: dict) -> dict:
        """发一条 Windows toast（在独立线程里跑，不阻塞事件循环；失败静默）。"""
        title = str(params.get("title", ""))[:60] or "SkySheep"
        body = str(params.get("body", ""))[:160]
        if not self.notify_enabled():
            return {"sent": False, "reason": "disabled"}
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._toast_blocking, title, body)
            return {"sent": True}
        except Exception:  # noqa: BLE001 - 通知失败不影响主流程
            return {"sent": False}

    @staticmethod
    def _toast_launch_uri() -> str:
        """点击 toast 时的激活 URI（skysheep://focus）；协议未注册返回空串（点击无动作）。

        协议由桌面壳（desktop.py，仅打包版）写进 HKCU\\Software\\Classes\\<scheme>：
        点击通知系统按协议再拉起一次 exe，单实例互斥让第二个进程自动转成
        「聚焦已有窗口」，通知才算从「发得出」变成「收得回」。这里只读注册表
        判断有没有——浏览器兜底模式没有桌面壳，读不到，行为与旧版一致。
        """
        if sys.platform != "win32":
            return ""
        try:
            import winreg

            from ... import instance

            name = instance.instance_name()
            scheme = "skysheep" if name is None else f"skysheep-{name}"
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, rf"Software\Classes\{scheme}"
            ) as key:
                winreg.QueryValueEx(key, "URL Protocol")  # 有这个值才算注册完整
            return f"{scheme}://focus"
        except Exception:  # noqa: BLE001 - 读取失败按未注册处理，不影响弹通知
            return ""

    def ordered_projects(self, projects: list) -> list:
        """按用户拖动保存的顺序排项目（ui.json 的 project_order，见 save_ui_prefs）。

        只把名单里的项目按保存序提前，其余（新增项目、名单外的）仍按原序（
        created_at DESC）排在后面；名单里已不存在的 id 自然忽略。这样旧偏好
        不会把新项目藏到看不见的位置，拖动只固定用户明确排过序的那部分。
        """
        try:
            order = self._read_ui_prefs().get("project_order")
        except Exception:  # noqa: BLE001
            order = None
        if not isinstance(order, list) or not order:
            return projects
        rank = {pid: i for i, pid in enumerate(order)}
        known = [p for p in projects if p.id in rank]
        unknown = [p for p in projects if p.id not in rank]
        return sorted(known, key=lambda p: rank[p.id]) + unknown

    async def get_ui_prefs(self) -> dict:
        return {"prefs": self._frontend_prefs(self._read_ui_prefs())}

    @staticmethod
    def _frontend_prefs(prefs: dict) -> dict:
        """WS prefs 面只暴露前端自己的偏好。active_project 由后端在切项目/
        删项目/首启时自写自读（0 = 无项目态），不给前端看、也不许前端写。"""
        return {k: v for k, v in prefs.items() if k != "active_project"}

    def first_paint_prefs(self) -> dict:
        """首帧外观偏好：由 server/app.py 注入到 index.html 的 <html> 标签。

        项目切换是整页 reload，若等 ui.get 回来才应用偏好，首帧必然是默认外观、
        随后再跳一次；这里提前给服务端用。读失败静默返回空（保持默认外观）。
        """
        try:
            return self._frontend_prefs(self._read_ui_prefs())
        except Exception:  # noqa: BLE001
            return {}

    async def save_ui_prefs(self, prefs: dict) -> dict:
        """WS 入口：前端偏好合并保存。active_project 后端独占，前端传了也忽略。"""
        cleaned = {k: v for k, v in (prefs or {}).items() if k != "active_project"}
        return await self._write_ui_prefs(cleaned)

    async def _write_ui_prefs(self, prefs: dict) -> dict:
        """合并保存；某项传 null 表示恢复默认（删除该项）。未知键忽略、越界值收敛到合法区间。

        后端自己也走这里写偏好（如 _bind_project 落 active_project），因此
        不在这里过滤键——过滤只发生在 save_ui_prefs 入口与读取面（_frontend_prefs）。
        """
        current = self._read_ui_prefs()
        for key, val in (prefs or {}).items():
            known = ("right_tabs", "right_active", "project_order", "quick_pos",
                     "session_order", "tab_order", "session_tabs", "session_active",
                     "hotkey", "default_model_name", "default_model_provider",
                     "aux_model_name", "aux_model_provider",
                     "notif_log")
            if key not in self.UI_PREFS_LIMITS and key not in known \
                    and key not in self.STRING_PREFS:
                continue
            if val is None:
                current.pop(key, None)
                continue
            if key in ("hotkey", "default_model_name", "default_model_provider",
                       "aux_model_name", "aux_model_provider"):
                # 自由字符串键（热键组合串 / 新会话默认模型 / 辅助对话模型）：
                # 非空收、空串删。值域校验在各自消费方
                # （desktop._parse_hotkey / set 时校验服务名）
                if isinstance(val, str) and val.strip():
                    current[key] = val.strip()
                else:
                    current.pop(key, None)
                continue
            if key in self.STRING_PREFS:
                if isinstance(val, str) and val in self.STRING_PREFS[key]:
                    current[key] = val
                else:
                    current.pop(key, None)
                continue
            if key == "right_tabs":
                if isinstance(val, list):
                    cleaned: list[str] = []
                    for t in val:
                        if t in self.RIGHT_TAB_IDS and t not in cleaned:
                            cleaned.append(t)
                    if cleaned:
                        current["right_tabs"] = cleaned
                    else:
                        current.pop("right_tabs", None)
                continue
            if key == "right_active":
                if isinstance(val, str) and val in self.RIGHT_TAB_IDS:
                    current["right_active"] = val
                else:
                    current.pop("right_active", None)
                continue
            if key == "project_order":
                # 分组视图拖动排序的项目 id 顺序（项目列表的展示序，随 ui.json 持久化）。
                # 只收正整数；去重防同一 id 重复占位，封顶防异常超大载荷；
                # 一个有效 id 都没有时删键（空数组与脏数据都视为「未自定义排序」）
                if isinstance(val, list):
                    seen: list[int] = []
                    for x in val:
                        if isinstance(x, int) and not isinstance(x, bool) and x > 0 and x not in seen:
                            seen.append(x)
                            if len(seen) >= 200:
                                break
                    if seen:
                        current["project_order"] = seen
                    else:
                        current.pop("project_order", None)
                else:
                    current.pop("project_order", None)
                continue
            if key == "quick_pos":
                # 快聊分组的锚点位置（经典/分组两视图共用）：固定项 top/bottom，
                # 或插在某个项目前 {"before": 项目 id}；锚点项目不在了前端回落
                # bottom（垫底），脏值一律删键（= 恢复默认垫底）
                if val in ("top", "bottom"):
                    current["quick_pos"] = val
                elif isinstance(val, dict) and isinstance(val.get("before"), int) \
                        and not isinstance(val.get("before"), bool) and val["before"] > 0:
                    current["quick_pos"] = {"before": val["before"]}
                else:
                    current.pop("quick_pos", None)
                continue
            if key == "session_order":
                # 分组视图组内会话的拖动序：{ 项目 key: [会话 id, ...] }。项目 key 用
                # 字符串（数字项目 id 与 quick/loose 伪组同构）；id 是非空字符串，
                # 每组去重封顶 200、整体封顶 100 组——一个组都没有时删键
                if isinstance(val, dict):
                    cleaned_order: dict[str, list[str]] = {}
                    for gk, ids in val.items():
                        gks = str(gk)
                        if not gks or not isinstance(ids, list):
                            continue
                        gseen: list[str] = []
                        for x in ids:
                            if isinstance(x, str) and x and x not in gseen:
                                gseen.append(x)
                                if len(gseen) >= 200:
                                    break
                        if gseen:
                            cleaned_order[gks] = gseen
                        if len(cleaned_order) >= 100:
                            break
                    if cleaned_order:
                        current["session_order"] = cleaned_order
                    else:
                        current.pop("session_order", None)
                else:
                    current.pop("session_order", None)
                continue
            if key == "tab_order":
                # 标签栏页签的拖动序：sid 字符串数组（无 sid 的空标签不进序——
                # 它没有稳定 id，始终排在最后）。去重封顶 200；一个都没有时删键。
                if isinstance(val, list):
                    seen_tab: list[str] = []
                    for x in val:
                        if isinstance(x, str) and x and x not in seen_tab:
                            seen_tab.append(x)
                            if len(seen_tab) >= 200:
                                break
                    if seen_tab:
                        current["tab_order"] = seen_tab
                    else:
                        current.pop("tab_order", None)
                else:
                    current.pop("tab_order", None)
                continue
            if key == "session_tabs":
                # 启动恢复：上次开着的会话标签（sid 数组，序即标签序）。去重封顶同
                # tab_order；空数组 = 全关了，删键（下次启动回到欢迎页）
                if isinstance(val, list):
                    seen_st: list[str] = []
                    for x in val:
                        if isinstance(x, str) and x and x not in seen_st:
                            seen_st.append(x)
                            if len(seen_st) >= self.SESSION_TABS_MAX:
                                break
                    if seen_st:
                        current["session_tabs"] = seen_st
                    else:
                        current.pop("session_tabs", None)
                else:
                    current.pop("session_tabs", None)
                continue
            if key == "session_active":
                # 启动恢复：激活的是哪张标签（sid）。空串/非法删键
                if isinstance(val, str) and val:
                    current["session_active"] = val
                else:
                    current.pop("session_active", None)
                continue
            if key == "notif_log":
                # 通知中心的持久化日志：同一套清洗；清空数组 = 删键（下次启动从零开始）
                cleaned_nl = self._clean_notif_log(val)
                if cleaned_nl:
                    current["notif_log"] = cleaned_nl
                else:
                    current.pop("notif_log", None)
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                continue
            lo, hi = self.UI_PREFS_LIMITS[key]
            current[key] = int(min(hi, max(lo, round(val))))
        # 原子写（M13 同族）：ui.json 写一半会让启动读偏好直接失败。
        # 合并结果与盘上内容语义一致（JSON 解析后相等，键序无关）时跳过落盘：
        # 偏好保存是高频路径（拖拽/切签/通知日志、启动落 active_project 都会走
        # 这里），同值重写只会搅动 mtime，让「这个文件最近被谁动过」类排查失真。
        # 损坏/读不到按「有变化」处理，照常原子写覆盖
        path = self._ui_prefs_path()
        try:
            unchanged = json.loads(path.read_text(encoding="utf-8")) == current
        except (OSError, ValueError):
            unchanged = False
        if not unchanged:
            write_text_atomic(path, json.dumps(current, ensure_ascii=False))
        return {"prefs": self._frontend_prefs(current)}
