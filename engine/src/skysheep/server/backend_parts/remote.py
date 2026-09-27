"""远程访问与系统维护：局域网绑定、Tailscale、一键重启、更新检查、主题联动。

从 backend.py 按职责注释整段搬入：令牌节流与来源判定（client_origin）、
Tailscale 网段、安装包下载校验等模块级小件也随职责区落在本模块，
由 backend 引用（保持 `skysheep.server.backend` 命名空间里的旧名字可见）。
方法体逐字保留，逻辑不变。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ... import __version__
from ...bgtasks import spawn_bg
from ...config import load_config, update_config_section
from ...core.uptodate import fetch_latest_release as check_latest_release
from ...core.uptodate import is_newer_version

logger = logging.getLogger("skysheep.security")

# 用户自定义局域网令牌的最小长度：默认令牌是 secrets.token_urlsafe(16)（22 字符），
# 这里只挡住明显过弱的自定义值，不强制复杂度（令牌要方便输入与扫码）。
MIN_LAN_TOKEN_CHARS = 12


class TokenThrottle:
    """对令牌验证失败的来源做有界的即时节流。

    目标不是防住分布式爆破（默认令牌 128 bit 随机，本就不怕枚举），而是把
    「同一来源连续试错」变成得不偿失：连续失败达阈值后，该来源的后续请求
    一律立即拒绝（不再做比对），冷却时长指数递增、封顶 10 分钟；验证成功
    （含本机回环直连）即清零。状态只在内存里，重启即清空。不 sleep——
    中间件里阻塞事件循环的代价远大于省下的这一次比对。
    """

    def __init__(
        self, threshold: int = 5, base_delay: float = 30.0, max_delay: float = 600.0
    ) -> None:
        self.threshold = max(1, threshold)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._fails: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked(self, ip: str, now: float | None = None) -> bool:
        until = self._blocked_until.get(ip)
        if until is None:
            return False
        current = time.monotonic() if now is None else now
        if current >= until:
            self._blocked_until.pop(ip, None)
            return False
        return True

    def note_failure(self, ip: str, now: float | None = None) -> float:
        """记录一次失败；触发封锁时返回本次封锁的时长（秒），未触发返回 0。"""
        n = self._fails.get(ip, 0) + 1
        self._fails[ip] = n
        if n < self.threshold:
            return 0.0
        delay = min(self.base_delay * (2 ** (n - self.threshold)), self.max_delay)
        self._blocked_until[ip] = (time.monotonic() if now is None else now) + delay
        return delay

    def note_success(self, ip: str) -> None:
        self._fails.pop(ip, None)
        self._blocked_until.pop(ip, None)

    def reset_failures(self, *, keep_blocks: bool = True) -> None:
        """清空失败计数；keep_blocks=True 时保留已生效的封锁。

        令牌轮换用（安全审查低危项）：旧实现直接换一个新 TokenThrottle，
        正在被封锁的爆破来源跟着一起解封——换了锁不等于要放人进来，
        已生效的封锁按原冷却时间走完更安全。
        """
        self._fails.clear()
        if not keep_blocks:
            self._blocked_until.clear()


# 令牌验证失败的留痕上限：设置页只需要「最近谁在敲门」，总次数单独计数。
TOKEN_FAILURE_LOG_MAX = 20

# Tailscale 分配的虚拟网段（IPv4 CGNAT 100.64.0.0/10 + 其 IPv6 ULA）。远程访问模式
# 靠它区分「tailnet 里的设备」与「物理局域网里的陌生设备」：后者连 IP 段都进不来。
TAILSCALE_NETS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)

# ui.json 的 theme 键值域：auto = 跟随系统（浅色默认落纸墨、深色默认落夜墨）；
# light / dark 是旧版两档值，读取时由前端与首帧注入分别按 paper / night 处理；
# 其余是主题 id——浅色：纸墨 paper、青瓷 celadon、秋柿 kaki；深色：夜墨 night、
# 黛夜 indigo、松烟 pine。与 app.js 的 THEMES 表、app.css 的 [data-theme=…] 段、
# index.html 设置页的主题卡片一一对应，改主题列表要四处同步。
THEME_PREFS = ("auto", "light", "dark", "paper", "celadon", "kaki", "night", "indigo", "pine")
# 「跟随系统」时的落点映射（设置页主题卡下方两个下拉）：浅色/深色各落一个具体主题，
# 与 wintheme.read_ui_theme、前端 themeAutoLight/themeAutoDark、首帧注入共用同一值域。
THEME_AUTO_LIGHT = ("paper", "celadon", "kaki")
THEME_AUTO_DARK = ("night", "indigo", "pine")


def client_origin(client) -> str:
    """把连接来源分成三类：local（本机回环）/ tailscale（tailnet 网段）/ other。

    远程访问（Tailscale）模式的 HTTP 守卫与 WS 验签都以此为准：other 一律拒绝，
    tailscale 必须验令牌，local 免令牌。入参兼容 ws.client 的 (host, port) 元组；
    TestClient 的 host 是 "testclient"，按本机对待（与既有测试约定一致）。
    """
    if not client:
        return "other"
    host = client[0] if isinstance(client, (tuple, list)) else str(client)
    host = str(host).split("%")[0]  # IPv6 zone id（fe80::1%eth0）先去掉
    if not host:
        return "other"
    if host in ("testclient", "localhost"):
        return "local"
    try:
        obj = ipaddress.ip_address(host)
    except ValueError:
        return "other"
    if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
        obj = obj.ipv4_mapped  # ::ffff:127.0.0.1 / ::ffff:100.64.x.x 这类映射地址
    if obj.is_loopback:
        return "local"
    if any(obj in net for net in TAILSCALE_NETS):
        return "tailscale"
    return "other"


class RemoteMixin:
    """局域网访问、Tailscale 远程、一键重启、更新检查与标题栏主题联动。

    方法自 backend.py 按职责注释逐字搬入。
    """

    async def _check_update_quietly(self) -> None:
        # 用户关掉了自动检查（关于页开关）就不再请求：内网/离线用户不必等超时
        try:
            if self._read_ui_prefs().get("update_check", 1) != 1:
                return
        except Exception:
            pass
        try:
            info = await check_latest_release()
            if is_newer_version(info["version"], __version__):
                self.update_info = info
        except Exception:  # noqa: BLE001 - 离线/仓库不存在：完全静默
            pass


    # ---- 局域网访问：绑定开关 + 令牌（重启服务后生效；前端拼 URL 与二维码） ----

    def note_token_failure(self, ip: str, where: str) -> None:
        """留痕一次令牌验证失败（HTTP 守卫与 WS 握手共用）。"""
        self._token_failure_total += 1
        self._token_failures.append({"ts": time.time(), "ip": ip, "where": where})
        del self._token_failures[:-TOKEN_FAILURE_LOG_MAX]

    def token_failure_summary(self) -> dict:
        """给设置页的失败概览：总次数 + 最近几条（ip / 时间）。"""
        recent = [
            {"ip": str(f.get("ip", "")), "ts": float(f.get("ts", 0) or 0)}
            for f in self._token_failures[-5:]
        ]
        return {"total": self._token_failure_total, "recent": recent}

    async def lan_status(self, include_token: bool = True) -> dict:
        """局域网访问状态；include_token=False 时不回传令牌（安全审查 A9）。

        令牌就是远程访问凭据本身：手机端设置页只需要知道「已设置」，
        不需要（也不应该）拿到明文。
        """
        server = self.cfg.server
        return {
            "enabled": bool(server.lan),
            "token": server.token if include_token else "",
            "has_token": bool(server.token),
            "ips": self._lan_ips(),
            "token_failures": self.token_failure_summary(),
            "note": "" if server.lan else "局域网访问当前关闭：服务只监听本机 127.0.0.1。",
        }

    async def lan_enable(self, params: dict) -> dict:
        server = self.cfg.server
        supplied = str(params.get("token") or "").strip()
        if supplied:
            self._check_lan_token(supplied)
        token = supplied or server.token or secrets.token_urlsafe(16)
        update_config_section("server", {"lan": True, "token": token})
        self.cfg = load_config()
        return {
            **await self.lan_status(),
            "note": "已开启局域网访问：重启 SkySheep 后生效（服务会监听全部网卡，"
                    "同一 Wi-Fi 下的设备凭令牌访问）。手机连不上时先看 Windows 防火墙——"
                    "首次弹出的「是否允许访问网络」要点允许；已错过的话在防火墙设置里"
                    "放行 SkySheep 后再试。",
        }

    async def lan_rotate_token(self) -> dict:
        """重新生成访问令牌，立即生效（守卫每次请求都读最新配置，不用重启）。

        旧令牌与已种下的 cookie 随即作废：之前发给手机的地址、二维码全部失效，
        手机要用新地址重新打开。失败计数与节流也一并清零——换了锁，旧的敲门
        记录不再有意义。
        """
        token = secrets.token_urlsafe(16)
        update_config_section("server", {"token": token})
        self.cfg = load_config()
        # 只清失败计数，已生效的封锁保留（见 reset_failures 的说明）
        self.token_throttle.reset_failures(keep_blocks=True)
        return {
            **await self.lan_status(),
            "note": "已生成新令牌并立即生效：之前的地址与二维码作废，"
                    "手机需要用下方新地址重新打开。",
        }

    @staticmethod
    def _check_lan_token(token: str) -> None:
        """自定义令牌的最小强度校验。

        默认令牌由 secrets.token_urlsafe(16) 生成（22 字符 / 128 bit 熵），无需校验。
        但 lan_enable 允许调用方传入自定义 token——这是降低整道防护强度的入口，
        弱令牌（"123456"、"password"）配上传二维码分享等于把服务开给同网段所有人。
        这里只查长度与字符多样性，不强制复杂度规则：令牌要能方便地输入/扫码。
        """
        if len(token) < MIN_LAN_TOKEN_CHARS:
            raise RuntimeError(
                f"令牌太短（{len(token)} 位）：至少 {MIN_LAN_TOKEN_CHARS} 位，"
                "建议直接留空由系统生成随机令牌"
            )
        if len(set(token)) < 4:
            raise RuntimeError(
                "令牌字符重复度过高（几乎全是同一个字符）：请换一个更随机的令牌，"
                "或留空由系统生成"
            )

    async def lan_disable(self) -> dict:
        update_config_section("server", {"lan": False})
        self.cfg = load_config()
        return {
            **await self.lan_status(),
            "note": "已关闭局域网访问：重启 SkySheep 后恢复仅本机监听。",
        }

    # ---- 远程访问（Tailscale）：手机不在同一网络也能连回这台电脑 ----
    # 与局域网访问共用令牌；绑定同样要等重启生效。守卫逻辑在 server/app.py：
    # 物理局域网等非 tailnet 来源直接拒绝，tailnet 来源必须验令牌。

    async def remote_status(self, include_token: bool = True) -> dict:
        """远程访问状态；include_token=False 时不回传令牌（同 lan_status，A9）。"""
        server = self.cfg.server
        return {
            "enabled": bool(server.tailscale),
            "token": server.token if include_token else "",
            "has_token": bool(server.token),
            "ips": self._tailscale_ips(),
            "token_failures": self.token_failure_summary(),
            "note": "" if server.tailscale else "远程访问当前关闭。",
        }

    async def remote_enable(self, params: dict) -> dict:
        server = self.cfg.server
        supplied = str(params.get("token") or "").strip()
        if supplied:
            self._check_lan_token(supplied)  # 与局域网访问同一把令牌、同一强度要求
        token = supplied or server.token or secrets.token_urlsafe(16)
        update_config_section("server", {"tailscale": True, "token": token})
        self.cfg = load_config()
        return {
            **await self.remote_status(),
            "note": "已开启远程访问：重启 SkySheep 后生效。手机需安装 Tailscale 并登录同一账号，"
                    "之后在任意网络（含手机流量）都能用下面的地址访问。",
        }

    async def remote_disable(self) -> dict:
        update_config_section("server", {"tailscale": False})
        self.cfg = load_config()
        return {
            **await self.remote_status(),
            "note": "已关闭远程访问：重启 SkySheep 后恢复仅本机监听。",
        }

    # ---- 一键重启：拉起等价的新进程，再请求当前进程优雅退出 ----
    # 新的桌面实例会先等旧进程的互斥体释放（desktop.py 的 _wait_mutex_free），
    # 所以这里不用掐着点：先发人再退场。退出走正常收尾（lifespan shutdown、
    # crash.flag 清除、窗口几何保存），不用 os._exit——那会留下「上次未正常
    # 关闭」的崩溃标记，下次启动吓用户一跳。

    @staticmethod
    def _relaunch_command() -> list[str] | None:
        """构造与当前进程等价的重启命令；无法确定时返回 None。

        * 打包态（PyInstaller）：重跑当前 exe，原样带上参数；
        * 脚本态（.py / .pyw 直接跑，如 SkySheep.pyw）：同一解释器重跑同一脚本；
        * 开发态（console script / -m，argv[0] 形态不定）：统一用 ``-c`` 调
          ``skysheep.cli.app.main()``，命令行参数原样透传。
        """
        if getattr(sys, "frozen", False):
            return [sys.executable, *sys.argv[1:]]
        arg0 = sys.argv[0] or ""
        if arg0.lower().endswith((".py", ".pyw")) and os.path.isfile(arg0):
            return [sys.executable, arg0, *sys.argv[1:]]
        return [
            sys.executable,
            "-c",
            "import sys; from skysheep.cli.app import main; sys.exit(main())",
            *sys.argv[1:],
        ]

    async def app_restart(self) -> dict:
        """重启应用。仅本机可调（dispatch 层 LOCAL_ONLY 门禁）。"""
        cmd = self._relaunch_command()
        if not cmd:
            raise RuntimeError("无法确定重启命令，请手动关闭后重新打开 SkySheep")
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "cwd": os.getcwd(),
        }
        if os.name == "nt":
            # DETACHED_PROCESS 脱离当前控制台；CREATE_NEW_PROCESS_GROUP 隔离
            # Ctrl+C/信号传播——新实例必须活得比当前进程久。不弹新窗口。
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | (  # type: ignore[attr-defined]
                subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            )
        else:
            kwargs["start_new_session"] = True
        try:
            subprocess.Popen(cmd, **kwargs)  # noqa: S603 - 命令由 _relaunch_command 构造
        except OSError as e:
            raise RuntimeError(f"重启失败：{e}") from None
        logger.info("应用重启：新进程已拉起（%s），当前进程即将退出", cmd[0])
        spawn_bg(self._shutdown_soon())
        return {"ok": True, "note": "正在重启 SkySheep，窗口会自动恢复。"}

    async def _shutdown_soon(self) -> None:
        """稍等回包发出、前端有机会提示后，再走退出路径。"""
        await asyncio.sleep(0.6)
        hook = self.restart_hook
        if hook is not None:
            try:
                hook()
            except Exception:  # noqa: BLE001 - 桌面钩子失败还有服务级兜底
                logger.warning("重启钩子（桌面收尾）执行失败", exc_info=True)
        shutdown = self.request_shutdown
        if shutdown is not None:
            try:
                shutdown()
            except Exception:  # noqa: BLE001
                logger.warning("请求服务退出失败", exc_info=True)


    @staticmethod
    def _hostname_ipv4s() -> list[str]:
        """本机所有非回环网卡的 IPv4（getaddrinfo 枚举，含 Tailscale 虚拟网卡）。"""
        ips: list[str] = []
        try:
            hostname = socket.gethostname()
            for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                ip = info[4][0]
                obj = ipaddress.ip_address(ip)
                if not obj.is_loopback and not obj.is_link_local and ip not in ips:
                    ips.append(ip)
        except OSError:
            pass
        return ips

    @classmethod
    def _lan_ips(cls) -> list[str]:
        """本机在物理局域网里的 IPv4（排除 Tailscale 虚拟网段；UDP connect 技巧兜底）。"""

        def keep(ip: str) -> bool:
            obj = ipaddress.ip_address(ip)
            if isinstance(obj, ipaddress.IPv6Address) and obj.ipv4_mapped:
                obj = obj.ipv4_mapped
            return isinstance(obj, ipaddress.IPv4Address) and obj not in TAILSCALE_NETS[0]

        ips = [ip for ip in cls._hostname_ipv4s() if keep(ip)]
        if not ips:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.connect(("10.255.255.255", 1))
                    ip = s.getsockname()[0]
                if ip and not ipaddress.ip_address(ip).is_loopback and keep(ip):
                    ips.append(ip)
            except OSError:
                pass
        return ips

    @classmethod
    def _tailscale_ips(cls) -> list[str]:
        """本机 Tailscale 虚拟网卡的 IPv4（100.64.0.0/10；没装/没运行则为空）。"""
        return [ip for ip in cls._hostname_ipv4s() if ipaddress.ip_address(ip) in TAILSCALE_NETS[0]]


    # ---- 更新检查（设置 · 关于可手动触发；启动时后台已查过一次） ----

    async def check_update(self) -> dict:
        try:
            info = await check_latest_release()
        except Exception as e:  # noqa: BLE001 - 手动检查要把失败原因说清楚
            self.update_error = str(e)
            self.update_info = None
            return {"available": False, "current": __version__, "frozen": self._is_frozen,
                    "error": str(e)}
        self.update_error = None
        if is_newer_version(info["version"], __version__):
            self.update_info = info
            return {"available": True, "current": __version__, "frozen": self._is_frozen, **info}
        self.update_info = None
        return {"available": False, "current": __version__, "frozen": self._is_frozen,
                "latest": info["version"]}

    async def install_update(self) -> dict:
        """应用内一键更新（仅安装版）：下载最新版安装包到临时目录。

        源码版直接拒绝——源码的更新方式是 git pull，静默装安装包反而会把
        运行中的源码目录搅乱。下载完成后并不自动安装，等 apply_update 确认。
        """
        if not self._is_frozen:
            raise RuntimeError("源码版不支持应用内更新：请在仓库里执行 git pull 后重启")
        info = await check_latest_release()
        if not is_newer_version(info["version"], __version__):
            return {"update_available": False, "current": __version__, "latest": info["version"]}
        # setup_url 是按发布约定拼的（releases/download/<tag>/SkySheep-<版本>-setup.exe），
        # 附件真缺要到下载时才以 404 暴露
        url = info["setup_url"]
        # 落在随机名的私有临时目录里，不用可预测的固定文件名（同机同用户抢置）
        dest = _update_dir() / f"SkySheep-{info['version']}-setup.exe"
        import httpx

        expected = await asyncio.to_thread(_fetch_setup_sha256, url)
        try:
            await asyncio.to_thread(_download_setup, url, dest, expected)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                raise RuntimeError(
                    "最新版没有 Windows 安装包附件，请到 GitHub Releases 手动下载") from e
            raise
        self._pending_update = str(dest)
        return {"update_available": True, "version": info["version"], "path": str(dest),
                "verified": bool(expected),
                # 本机安装登记的侧别决定装的时候要不要提权（HKLM=所有用户=弹 UAC，
                # HKCU=当前用户=静默直装）。前端据此在退出前就把「留意系统弹窗」
                # 讲清楚——应用退出后提示文本就看不见了。
                "uac": _setup_privilege_override() != "/CURRENTUSER"}

    async def apply_update(self) -> dict:
        """退出本应用并静默运行已下载的安装包。安装包声明了与本应用相同的
        单实例互斥体，且批处理延迟 2 秒再拉起它——届时本进程已退出、互斥体已释放。

        执行走 apply-update.cmd（见 _write_update_helper 的注释：直接拼命令串
        会被 Popen 的参数转义弄坏，安装器根本起不来）。
        """
        pending = self._pending_update or ""
        if not pending or not Path(pending).is_file():
            raise RuntimeError("还没有下载好的更新包，请先执行「下载更新」")
        log_path = _prepare_update_log(pending)
        override = _setup_privilege_override()
        script = _write_update_helper(pending, log_path, override)
        if script is None:
            raise RuntimeError("无法写出更新脚本（临时目录不可写），请到 GitHub Releases 手动下载安装")
        flags = 0
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(
                ["cmd", "/c", script.name],
                creationflags=flags, close_fds=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                cwd=str(script.parent),  # 相对名执行，彻底避开路径引号问题
            )
        except OSError as e:
            raise RuntimeError(f"无法启动安装程序：{e}") from e

        async def _quit_soon() -> None:
            await asyncio.sleep(1.0)  # 给 WS 回复留出送达时间
            try:
                await self.shutdown()
            except Exception:  # noqa: BLE001 - 退出路径不再抛
                pass
            os._exit(0)

        spawn_bg(_quit_soon())
        return {"quitting": True, "installer": pending, "log": str(log_path) if log_path else "",
                "uac": override != "/CURRENTUSER"}

    # ---- 主题：标题栏联动（前端把解析后的主题回传给桌面壳） ----

    async def apply_theme(self, params: dict) -> dict:
        from ... import wintheme

        # 前端把解析后的主题回传：优先用主题 id（标题栏 / 窗口底色逐主题精确一致），
        # 旧版字段 resolved（light/dark 两档）兜底；未知值回退 paper。
        theme = str(params.get("theme") or "").strip().lower()
        if theme in wintheme.THEME_PALETTE:
            wintheme.set_theme_mode(theme)
        else:
            mode = "dark" if str(params.get("resolved", "light")) == "dark" else "light"
            wintheme.set_theme_mode(mode)
        # 事件驱动重刷标题栏：前端 CSS 变量是瞬时变色，而桌面壳看板线程每秒才
        # 轮询一次——不等这里推一把，页面和系统标题栏的变色时间肉眼可见地不一致。
        try:
            wintheme.refresh_now()
        except Exception:  # noqa: BLE001 - 浏览器模式无窗口 / 老系统无 DWM：静默
            pass
        return {"mode": wintheme.current_theme_mode()}


# 安装包体积上限：本项目安装包在百 MB 量级，超出说明下载被换成了别的东西。
# 限流式边下边校，不先全量进内存（否则上限形同虚设）。
MAX_SETUP_BYTES = 300 << 20


def _download_setup(url: str, dest: Path, expected_sha256: str = "") -> None:
    """把安装包下载到 dest（.part 暂存、完成后改名）。同步阻塞，须在线程里跑。

    follow_redirects 必开：browser_download_url 会 302 到 objects.githubusercontent.com。
    trust_env=False 与 web_fetch 同口径：不让环境变量里的代理/netrc 插手更新链。
    """
    import httpx

    part = dest.with_suffix(dest.suffix + ".part")
    try:
        with httpx.Client(timeout=60.0, follow_redirects=True, trust_env=False) as client:
            with client.stream("GET", url) as resp:
                resp.raise_for_status()
                digest = hashlib.sha256()
                total = 0
                with open(part, "wb") as f:
                    for chunk in resp.iter_bytes(1 << 16):
                        total += len(chunk)
                        if total > MAX_SETUP_BYTES:
                            raise RuntimeError(
                                f"安装包超过 {MAX_SETUP_BYTES // (1 << 20)}MB 上限，已中止下载")
                        digest.update(chunk)
                        f.write(chunk)
        # 校验是 PE 可执行文件（MZ 头）再落正式名：防中途断流留下半截文件被误装
        with open(part, "rb") as f:
            if f.read(2) != b"MZ":
                raise RuntimeError("下载的内容不是 Windows 安装包（头部校验失败）")
        # 发布方附带了 .sha256 就按它校验；没附带时上面两道（MZ 头 + 体积上限）兜底
        if expected_sha256 and digest.hexdigest().lower() != expected_sha256.lower():
            raise RuntimeError("安装包校验和不匹配，已丢弃（下载可能被篡改）")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)


def _fetch_setup_sha256(url: str) -> str:
    """取同名 `.sha256` 附件；没有就返回空串（不阻断更新）。

    本项目的更新检查走 releases/latest 页面跳转而不走 api.github.com（后者
    匿名限流 60 次/小时/IP，国内共享出口很容易被耗尽），所以拿不到附件清单，
    只能按发布约定试探同目录下的 .sha256。因此这项是「有则必校」：发布方
    上传了校验和就能防住篡改，没上传就退回 MZ 头 + 体积上限两道。
    """
    import httpx

    try:
        with httpx.Client(timeout=15.0, follow_redirects=True, trust_env=False) as client:
            resp = client.get(url + ".sha256")
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    # 兼容 `<hex>` 与 `<hex>  <filename>` 两种常见写法
    m = re.fullmatch(r"([0-9a-fA-F]{64}).*", resp.text.strip(), re.S)
    return m.group(1).lower() if m else ""


def _update_dir() -> Path:
    """为本轮更新开一个随机名的私有临时目录，并顺手清掉旧目录。

    不能落到固定可预测的 `%TEMP%\\SkySheep-<版本>-setup.exe`：那个路径同机同
    用户的任何进程都能提前占位（写一个恶意 exe 在那里），下载层只会看到
    「文件已存在」而照装不误。随机目录名使抢置需要先猜中 128 位随机串。
    旧目录按修改时间清（>24h），避免每轮更新都往 %TEMP% 里堆几十 MB。
    """
    tmp = Path(tempfile.gettempdir())
    for old in tmp.glob("skysheep-update-*"):
        try:
            if old.is_dir() and time.time() - old.stat().st_mtime > 86400:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            continue
    return Path(tempfile.mkdtemp(prefix="skysheep-update-"))


_SETUP_APPID = "{7C1B6E9A-52C4-4B7D-9A34-A1B2C3D4E5F6}"  # 与 tools/installer.iss 的 AppId 保持一致


def _setup_privilege_override() -> str:
    """按现有安装登记的侧别决定要不要给安装器传权限覆盖。

    安装包默认要管理员权限（PrivilegesRequired=admin），标准用户下静默安装
    会卡在 UAC 等确认——程序内更新的窗口期用户往往没注意到弹窗，表现成
    「更新了但版本没变」。装在用户目录（登记在 HKCU）的安装完全不需要提权：
    传 /CURRENTUSER 即可静默直装；登记在 HKLM（装时选了「为所有用户安装」）
    的必须提权，维持默认行为；查询失败或无登记也不传，不改变现状。
    """
    try:
        import winreg
    except ImportError:  # 非 Windows：apply_update 走不到这里
        return ""
    key = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{_SETUP_APPID}_is1"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key):
            return "/CURRENTUSER"
    except OSError:
        return ""


def _prepare_update_log(pending: str) -> Path | None:
    """在用户日志目录准备一份更新安装日志（固定名，每次覆盖只留最近一次）。

    安装器从此不再是黑箱：/LOG 让 Inno 记录自己的安装过程，本函数先写入一行
    头部（何时、装哪个包、走哪条权限路径），安装结束后 cmd 侧再追加退出码。
    目录/文件建不出来就返回 None，安装命令里相应省略日志参数——日志失败
    不阻断更新。
    """
    log_path = Path.home() / ".skysheep" / "logs" / "update-setup.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        side = "HKCU，无需 UAC" if _setup_privilege_override() else "需管理员授权，可能弹 UAC"
        with open(log_path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 开始静默安装更新：{pending}"
                    f"（权限：{side}）\n")
    except OSError:
        return None
    return log_path


def _write_update_helper(pending: str, log_path: Path | None, override: str = "") -> Path | None:
    """把「延迟等应用退出 → 静默安装 → 记录退出码」写成 apply-update.cmd。

    以前是把命令串直接交给 `cmd /c`（Popen 传列表）：Python 会把串里的引号转义成
    `\\"`，而 cmd.exe 不认反斜杠转义——安装包路径与 `/LOG=`、退出码重定向整段被
    解析坏（"文件名、目录名或卷标语法不正确"），安装器从未真正启动，日志里也
    只剩一行头部，表现为「点了更新、应用退出后再没下文」。改成先落一个批处理、
    再 `cmd /c apply-update.cmd` 执行：批处理里没有跨层转义，引号按 cmd 的规则
    原样生效，路径含空格/中文也安全。

    延迟用 ping 而不是 timeout：更新以 DETACHED_PROCESS 拉起 cmd，进程没有
    控制台，timeout 的输入重定向检查会立即报错返回，2 秒等待名存实亡——届时
    本应用还没退出、单实例互斥体未释放，安装器会当作已有实例在跑而放弃。
    ping 没有控制台依赖，-n 3 恰好约 2 秒。/RESTARTAPP 让安装器装完自动拉起
    新版（installer.iss 的 [Run] 按此参数决定，手动静默安装不受影响）。

    批处理用 GBK 写（cmd 按 OEM 代码页读 .cmd，中文 Windows 即 cp936），只有
    提示文案受影响，命令本身是 ASCII。写不出文件返回 None（调用方保持原行为）。
    """
    script = Path(pending).parent / "apply-update.cmd"
    args = "/SILENT /CLOSEAPPLICATIONS /RESTARTAPP"
    if override:
        args += f" {override}"
    if log_path is not None:
        args += f' /LOG="{log_path}"'
    lines = [
        "@echo off",
        "rem SkySheep 程序内更新助手（由应用生成；安装完成后可整个目录删除）",
        "ping -n 3 127.0.0.1 >nul",
    ]
    if log_path is not None:
        lines += [
            f'if not exist "{pending}" (',
            f'  >>"{log_path}" echo [更新失败] 安装包不存在：{pending}',
            "  exit /b 1",
            ")",
            f'>>"{log_path}" echo [%DATE% %TIME%] 开始运行安装包',
        ]
    lines.append(f'call "{pending}" {args}')
    if log_path is not None:
        lines.append(f'>>"{log_path}" echo [setup 退出码: %errorlevel%]')
    try:
        script.write_text("\r\n".join(lines) + "\r\n", encoding="gbk", errors="replace")
    except OSError:
        return None
    return script
