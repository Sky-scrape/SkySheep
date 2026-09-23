"""面向用户的杂项支持能力：用系统程序打开文件 / 定位文件夹 / 导出诊断包。

三件事都属于「用户点了界面上的按钮」而不是 Agent 自己发起的动作，所以不走
PermissionGate；但路径一律先做白名单校验（工作目录内、不是危险可执行文件），
避免这个通道被当成"任意执行"的后门。

Windows 上统一走 shell32.ShellExecuteW（与托盘/标题栏一样用 ctypes 直连，
不经过 subprocess，也就没有命令拼接的空间）；其他平台返回可读的"不支持"。
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys
import time
import zipfile
from pathlib import Path

# 调用系统默认程序打开时，允许"直接启动"的扩展名。
# 其余一律退化成"在文件管理器里定位"——工作目录里放个 .exe/.bat/.ps1 很正常，
# 点一下文件名就把脚本跑起来不是我们想要的语义。
LAUNCHABLE_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".toml", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
    ".py", ".rb", ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".php", ".sh", ".sql", ".r", ".m", ".swift", ".lua", ".pl",
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".ico",
    ".mp4", ".mp3", ".wav", ".webm", ".mkv",
}
MAX_LAUNCH_BYTES = 80 * 1024 * 1024  # 超大文件交给资源管理器定位，别让默认程序卡死

SW_SHOWNORMAL = 1
_SHELL_OK = 32  # ShellExecuteW 返回值 > 32 才算成功

# 进程启动时间戳（性能日志用）：server 就绪、首个 WS boot 等节点据此输出耗时
PROCESS_START = time.perf_counter()


def open_external(target: str) -> None:
    """用系统默认程序打开 http(s) 链接（反馈页 / 下载页 / 服务商注册页）。

    与 open_with_default_app 的差异：接受的是 URL 字符串而不是本地文件，
    不做扩展名白名单（URL 没有可执行语义）；非 Windows 走 webbrowser 兜底。
    """
    target = str(target or "").strip()
    if not target.startswith(("http://", "https://")):
        raise RuntimeError("只允许打开 http(s) 链接：" + target)
    if sys.platform == "win32":
        # ShellExecuteW 的 lpFile 参数天然接受 URL，交给默认浏览器
        shell_open(Path(target))
    else:
        import webbrowser

        webbrowser.open(target)


def shell_open(target: Path, params: str = "") -> None:
    """用 Windows shell 打开目标（文件 / 文件夹），不经过命令行解释器。"""
    if sys.platform != "win32":
        raise RuntimeError("该功能目前仅在 Windows 桌面可用")
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
        ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_int,
    ]
    result = shell32.ShellExecuteW(
        None, "open", str(target), params or None, None, SW_SHOWNORMAL
    )
    code = int(result or 0)
    if code <= _SHELL_OK:
        raise RuntimeError(f"系统拒绝打开（错误码 {code}）：{target}")


def is_launchable(path: Path) -> bool:
    """该文件能否放心交给系统默认程序打开。"""
    if path.suffix.lower() not in LAUNCHABLE_EXTS:
        return False
    try:
        return path.stat().st_size <= MAX_LAUNCH_BYTES
    except OSError:
        return False


def reveal(path: Path) -> None:
    """在文件管理器里定位路径（文件夹直接打开，文件选中它）。"""
    path = Path(path)
    if path.is_dir():
        shell_open(path)
        return
    if sys.platform == "win32":
        # explorer 的 /select 参数走 shell 的"参数"通道，不做字符串拼命令
        shell_open(Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe",
                   params=f'/select,"{path}"')
    else:
        shell_open(path.parent)


def open_with_default_app(path: Path) -> str:
    """用系统默认程序打开文件；危险扩展名退化为在文件管理器里定位。

    返回实际发生的动作（"opened" / "revealed"），供界面提示用。
    """
    path = Path(path)
    if not path.exists():
        raise RuntimeError("文件不存在：" + str(path))
    if path.is_dir() or not is_launchable(path):
        reveal(path)
        return "revealed"
    shell_open(path)
    return "opened"


def open_folder(path: Path) -> None:
    """打开一个文件夹（设置里「打开日志/数据目录」用）。"""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    shell_open(path)


# ---- 诊断包 ----

# 打码按「键名包含」匹配，宁可多打不可漏打：诊断包是要发给外部开发者的，
# 里面的明文密钥一旦外发就等同泄露（渠道的 app_secret / client_secret 就落在
# config.toml 里，旧清单只看 api_key/token/env_key/key 拦不住它们）。
SECRET_KEYS = (
    "api_key", "token", "env_key", "key",
    "secret", "password", "passwd", "credential", "private",
)


def _redact_toml(path: Path) -> str:
    """读取 config.toml 并把含密钥的行打码（保结构、不留值）。"""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"# 无法读取 {path}: {e}"
    out = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip().strip('"').lower()
            if any(s in key for s in SECRET_KEYS):
                indent = line[: len(line) - len(line.lstrip())]
                name = stripped.split("=", 1)[0].strip()
                has_value = stripped.split("=", 1)[1].strip().strip('"') != ""
                out.append(f'{indent}{name} = ' + ('"***已打码***"' if has_value else '""'))
                continue
        out.append(line)
    return "\n".join(out)


def _log_tail(path: Path, limit: int = 200_000) -> str:
    try:
        data = path.read_bytes()
    except OSError as e:
        return f"(无法读取 {path}: {e})"
    text = data[-limit:].decode("utf-8", errors="replace")
    return text if len(data) <= limit else "...(更早的日志已省略)\n" + text


def build_diagnostic_zip(home: Path, dest_dir: Path | None = None) -> Path:
    """打包一份可发给开发者的诊断 zip：版本/环境/日志/配置（密钥打码）。

    不含会话数据库与全局记忆正文——排障用不到，少一份泄漏面。
    """
    home = Path(home)
    out_dir = Path(dest_dir) if dest_dir else home / "diagnostics"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"skysheep-diag-{time.strftime('%Y%m%d-%H%M%S')}.zip"

    from . import __version__

    info = [
        f"SkySheep: {__version__}",
        f"Python: {sys.version.split()[0]} ({sys.executable})",
        f"Platform: {platform.platform()}",
        f"Frozen: {bool(getattr(sys, 'frozen', False))}",
        f"Home: {home}",
        f"cwd: {os.getcwd()}",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("env.txt", "\n".join(info) + "\n")
        cfg = home / "config.toml"
        if cfg.exists():
            zf.writestr("config.redacted.toml", _redact_toml(cfg))
        log = home / "logs" / "desktop.log"
        if log.exists():
            zf.writestr("logs/desktop.log", _log_tail(log))
        for rotated in sorted((home / "logs").glob("desktop.log.*")):
            zf.writestr(f"logs/{rotated.name}", _log_tail(rotated, 100_000))
        # 目录清单（不含内容）：能看出有没有异常文件占了空间
        listing = []
        for sub in ("", "sessions", "skills", "backups", "logs", "screenshots"):
            d = home / sub if sub else home
            if not d.is_dir():
                continue
            for item in sorted(d.iterdir()):
                try:
                    size = item.stat().st_size
                except OSError:
                    size = -1
                listing.append(f"{sub + '/' if sub else ''}{item.name}\t{size}")
        zf.writestr("files.txt", "\n".join(listing) + "\n")
    return target


__all__ = [
    "LAUNCHABLE_EXTS",
    "build_diagnostic_zip",
    "is_launchable",
    "open_folder",
    "open_with_default_app",
    "reveal",
    "shell_open",
]
