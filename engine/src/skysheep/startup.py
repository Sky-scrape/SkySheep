"""开机自启（Windows 当前用户级 Run 项）。

定时任务、日程提醒、cron 结果通知都只在 SkySheep 运行时才会触发；关窗默认缩到
托盘，可一旦「彻底退出」，到点的任务就只能等下次打开应用才补跑（详见 README 的
定时任务说明）。给一个一键自启开关，"无人值守"才是真的无人值守。

只写 HKCU\\...\\Run（当前用户，不需要管理员权限，也不会影响别的用户）。
注册表读写收敛在 ``_WindowsRegistry`` 里并可注入替身，测试不会真去动注册表；
非 Windows 平台一律返回可读的"不支持"，不抛异常。
"""

from __future__ import annotations

import sys
from pathlib import Path

from . import instance

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def is_supported() -> bool:
    return sys.platform == "win32"


def value_name() -> str:
    """自启项的注册表值名：按实例身份区分。

    两个身份各自开着自启时，少一个后缀就会互相覆盖——后写的那个把先写的顶掉，
    而用户看到的开关状态还是“已开启”。
    """
    return instance.autostart_value_name()


def launch_command() -> str:
    """开机自启要执行的命令行。

    打包态指向 exe 自己；开发态指向 SkySheep.pyw（pythonw 启动，不弹终端窗口）。
    """
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    runner = pyw if pyw.exists() else Path(sys.executable)
    app = Path(__file__).resolve().parents[2] / "SkySheep.pyw"
    if app.exists():
        return f'"{runner}" "{app}"'
    return f'"{runner}" -m skysheep.cli.app app'


class _WindowsRegistry:
    """真机注册表后端：三个方法就是全部接口，便于测试替换。"""

    def get(self) -> str | None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, _ = winreg.QueryValueEx(key, value_name())
                return str(value)
        except FileNotFoundError:
            return None
        except OSError:
            return None

    def set(self, command: str) -> None:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, value_name(), 0, winreg.REG_SZ, command)

    def delete(self) -> None:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, value_name())
        except FileNotFoundError:
            pass


def status(reg=None) -> dict:
    """当前自启状态：{supported, enabled, command, current}。"""
    if not is_supported():
        return {"supported": False, "enabled": False, "command": "", "current": ""}
    current = launch_command()
    reg = reg or _WindowsRegistry()
    try:
        value = reg.get()
    except OSError:
        value = None
    return {
        "supported": True,
        "enabled": bool(value),
        "command": current,
        "current": value or "",
        # 自启项指向的路径和当前运行方式不一致（比如换了安装目录、从打包版换成源码版），
        # 提示用户重新保存一次即可刷新
        "stale": bool(value) and value != current,
    }


def set_enabled(enabled: bool, reg=None) -> dict:
    """打开 / 关闭开机自启，返回写入后的状态。"""
    if not is_supported():
        raise RuntimeError("开机自启目前只支持 Windows")
    reg = reg or _WindowsRegistry()
    if enabled:
        reg.set(launch_command())
    else:
        reg.delete()
    return status(reg=reg)


__all__ = [
    "RUN_KEY",
    "is_supported",
    "launch_command",
    "set_enabled",
    "status",
    "value_name",
]
