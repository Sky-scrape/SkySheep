"""浏览器控制（设置 · 浏览器控制）：内置浏览器的证书开关与数据清除。

「内置浏览器」在本项目里是两处，开关与清除都按这两处口径说话：

- **应用窗口 / 右侧预览面板**：pywebview 的 WebView2，profile 固定落在数据目录的
  ``webview/EBWebView``（``--browser`` 模式是 ``webview-app``），Cookie、站点数据与
  缓存都持久化在这里（desktop.py 刻意用固定 profile 关掉私密模式，二次启动才快）。
  它的数据**使用中删不掉**（Cookie 与 Local Storage 是被 WebView2 打开的 SQLite 与
  leveldb），所以「清除全部」只写一个待办标记，真正的删除在下次启动、WebView2 创建
  环境之前由 ``webviewargs.apply_pending_reset`` 落地。
- **无头 Edge（web_page 工具）**：一份临时 profile，随实例关闭/闲置回收自己删掉，
  每次动作现拉起——证书开关对它下次拉起即生效，数据清除顺手扫掉崩溃残留。

安全姿态与电脑/浏览器控制同档：清除是本机管理动作，远程不可调用（app.py 注册表
里 local_only），前端照例过确认弹窗。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from ...config import ConfigError, load_config, set_advanced_settings_in_config, skysheep_home
from ...tools.browser_cdp import sweep_orphan_browser_profiles
from ...webviewargs import PROFILE_DIRS, PROFILE_INNER, RESET_MARKER_NAME

# 「清除缓存」的落点（相对 profile 根）：HTTP 缓存、代码/GPU 缓存、Service Worker
# 的 Cache Storage。Cookie、Local Storage、IndexedDB 这些站点数据不在名单里——
# 那是「清除全部」才动的东西（保住登录态，只是让页面重新从网上取一遍）。
_CACHE_DIRS = (
    "GPUPersistentCache",
    "ShaderCache",
    "GrShaderCache",
    "component_crx_cache",
    "extensions_crx_cache",
    "Default/Cache",
    "Default/Code Cache",
    "Default/GPUCache",
    "Default/DawnGraphiteCache",
    "Default/DawnWebGPUCache",
    "Default/Service Worker/CacheStorage",
)


def browser_profile_roots(home: Path | None = None) -> list[Path]:
    """内置浏览器 profile 根目录（不存在也返回路径，调用方自行判 is_dir）。"""
    base = home or skysheep_home()
    roots: list[Path] = []
    for name in PROFILE_DIRS:
        base_dir = base / name
        inner = base_dir / PROFILE_INNER
        roots.append(inner if inner.is_dir() else base_dir)
    return roots


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _rmtree(path: Path) -> bool:
    """尽力删除；被占用（WebView2 正开着）返回 False 而不抛。"""
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        # 半删状态：把还能删的删掉，剩下的留给下次启动的清理
        shutil.rmtree(path, ignore_errors=True)
        return not path.exists()


def clear_cache_dirs(home: Path | None = None) -> dict:
    """删掉各 profile 的缓存目录；返回 {removed, failed, freed_bytes}。"""
    stats = {"removed": 0, "failed": 0, "freed_bytes": 0}
    for root in browser_profile_roots(home):
        if not root.is_dir():
            continue
        for rel in _CACHE_DIRS:
            target = root / rel
            if not target.exists():
                continue
            size = _dir_size(target) if target.is_dir() else 0
            if _rmtree(target):
                stats["removed"] += 1
                stats["freed_bytes"] += size
            else:
                stats["failed"] += 1
    return stats


def write_reset_marker(home: Path | None = None) -> None:
    """登记「下次启动清空内置浏览器数据」（内容仅诊断用，apply 侧只认文件在不在）。"""
    base = home or skysheep_home()
    try:
        from ...textio import write_text_atomic

        write_text_atomic(
            base / RESET_MARKER_NAME,
            f'{{"mode": "all", "created": {int(time.time())}}}\n',
        )
    except OSError:
        # 标记写不下去不是致命错误：调用方按「未登记」如实回报，用户可再来一次
        pass


def reset_pending(home: Path | None = None) -> bool:
    return ((home or skysheep_home()) / RESET_MARKER_NAME).is_file()


class BrowserDataMixin:
    """设置 · 浏览器控制：状态、开关保存与数据清除。"""

    # ---- 状态 ----

    def browser_status(self) -> dict:
        home = skysheep_home()
        profiles = []
        total = 0
        for name, root in zip(PROFILE_DIRS, browser_profile_roots(home), strict=False):
            size = _dir_size(root) if root.is_dir() else 0
            total += size
            profiles.append(
                {"name": name, "path": str(root), "exists": root.is_dir(), "bytes": size}
            )
        return {
            "browser_control": bool(self.cfg.browser_control),
            "ignore_cert": bool(self.cfg.browser_ignore_cert),
            "profiles": profiles,
            "total_bytes": total,
            "pending_reset": reset_pending(home),
            "home": str(home),
        }

    # ---- 保存（开关） ----

    async def save_browser_settings(self, params: dict) -> dict:
        """保存浏览器控制开关；两项都可单独提交（不传 = 不动）。

        - 总开关 ``browser_control`` 改的是「Agent 能用哪些工具」→ 重建注册表
          （与 advanced.save 的同一套热生效机制）；
        - ``ignore_cert`` 改的是下一次拉起的启动参数 → 关掉在跑的无头 Edge 实例，
          下次动作按新参数重新拉起（WebView2 那份要重启应用，UI 已写明）。
        """
        want_master = params.get("browser_control")
        want_cert = params.get("ignore_cert")
        if want_master is None and want_cert is None:
            return self.browser_status()
        master = None if want_master is None else bool(want_master)
        cert = None if want_cert is None else bool(want_cert)
        master_changed = master is not None and master != bool(self.cfg.browser_control)
        cert_changed = cert is not None and cert != bool(self.cfg.browser_ignore_cert)
        try:
            set_advanced_settings_in_config(
                browser_control=master, browser_ignore_cert=cert
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()
        if master_changed:
            self._apply_registry_to_agents()
        if cert_changed:
            from ...tools.browser_cdp import close_browser

            await close_browser()
        return self.browser_status()

    # ---- 清除数据 ----

    async def clear_browser_data(self, mode: str) -> dict:
        """清除内置浏览器数据。

        ``mode="cache"`` 只删缓存（保住 Cookie 与站点数据，页面重新联网取一遍）；
        ``mode="all"`` 在缓存之外还要清 Cookie 与站点数据——后者使用中删不掉，
        改为登记待办、下次启动落地（见模块 docstring）。
        """
        mode = "all" if str(mode) == "all" else "cache"
        home = skysheep_home()
        stats = clear_cache_dirs(home)
        swept = sweep_orphan_browser_profiles()
        result = {
            "mode": mode,
            "removed": stats["removed"],
            "failed": stats["failed"],
            "freed_bytes": stats["freed_bytes"],
            "temp_profiles_removed": swept,
            "needs_restart": False,
        }
        if mode == "all":
            write_reset_marker(home)
            result["needs_restart"] = reset_pending(home)
        return result
