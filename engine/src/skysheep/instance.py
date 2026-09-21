"""实例身份：让同一台机器上的两份 SkySheep 互不影响。

背景：源码版（开发/测试）与安装版（日常使用）默认共用同一个身份——数据目录
``~/.skysheep``、单实例互斥体 ``Local\\SkySheepDesktopSingleton``、窗口标题
``SkySheep``。共用会导致三方串台：

- 会话库、config.toml、技能、日志、ui.json 全部混在一起，两边互相覆盖；
- 单实例互斥体同名，两个版本不能同时打开（后开的去唤前开的窗口）；
- 权限档位存在 ``ui.json`` 的 ``accept_edits``，一边放宽另一边跟着放宽。

用 ``SKYSHEEP_INSTANCE`` 环境变量给一份安装起一个身份名（如 ``dev``），
身份名会同时影响数据目录、互斥体名、窗口标题与托盘文案，且三处口径一致，
所以两个身份之间既不共享数据，也能同时运行、互相识别。

身份名限制为 ``[a-z0-9][a-z0-9_-]{0,23}``：它会进文件名、互斥体名和窗口标题，
不放行路径分隔符、空格与大小写变体（大小写变体会让 ``Local\\`` 前缀的互斥体
判定出现两个"不同"的名字，隔离失效）。

优先级：``SKYSHEEP_HOME`` > ``SKYSHEEP_INSTANCE`` > 默认。显式指定数据目录时
完全按它走，不再叠加实例后缀——测试与临时环境都依赖这个语义。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 默认身份：安装版用它，保持与历史版本完全一致的对外表现（升级无感知）
DEFAULT_HOME_NAME = ".skysheep"
DEFAULT_TITLE = "SkySheep"
INSTANCE_ENV = "SKYSHEEP_INSTANCE"

# 身份名白名单：小写字母数字开头，后可跟 -/_，总长不超过 24
_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def instance_name() -> str | None:
    """当前实例身份名；未设置或不合规时返回 None（等同默认身份）。

    不合规一律按"未设置"处理而不是抛错：身份名只影响隔离，不该让应用起不来。
    真正的写法错误由 :func:`instance_problem` 暴露给启动日志与设置页。
    """
    raw = (os.environ.get(INSTANCE_ENV) or "").strip()
    return raw if _INSTANCE_RE.match(raw) else None


def instance_problem() -> str | None:
    """身份名写了但格式不合规时返回可读原因，否则 None。"""
    raw = (os.environ.get(INSTANCE_ENV) or "").strip()
    if not raw or _INSTANCE_RE.match(raw):
        return None
    return (
        f"{INSTANCE_ENV} 取值不合法：{raw!r}。"
        "只允许小写字母、数字、下划线、连字符，且以字母或数字开头（最长 24 字符）。"
        "本次按默认身份启动，未做隔离。"
    )


def data_home() -> Path:
    """数据目录：``SKYSHEEP_HOME`` 优先，其次按实例身份加后缀，最后是默认目录。"""
    env = os.environ.get("SKYSHEEP_HOME")
    if env:
        return Path(env).expanduser()
    name = instance_name()
    base = Path.home() / (DEFAULT_HOME_NAME if name is None else f"{DEFAULT_HOME_NAME}-{name}")
    return base


def app_title() -> str:
    """窗口标题 / 报错弹窗标题；带身份后缀，便于用户与自己识别。"""
    name = instance_name()
    return DEFAULT_TITLE if name is None else f"{DEFAULT_TITLE} [{name}]"


def mutex_name() -> str:
    """单实例互斥体名（``Local\\`` 前缀 = 每登录会话一个）。

    按身份区分，两个身份才能各开一个实例；同一身份仍然只允许一个实例。
    """
    name = instance_name()
    suffix = "" if name is None else "-" + name
    return f"Local\\{DEFAULT_TITLE}DesktopSingleton{suffix}"


def autostart_value_name() -> str:
    """开机自启的 HKCU\\Run 值名（按身份区分，两个身份各自独立开关）。"""
    name = instance_name()
    return DEFAULT_TITLE if name is None else f"{DEFAULT_TITLE}-{name}"


__all__ = [
    "DEFAULT_HOME_NAME",
    "DEFAULT_TITLE",
    "INSTANCE_ENV",
    "app_title",
    "autostart_value_name",
    "data_home",
    "instance_name",
    "instance_problem",
    "mutex_name",
]
