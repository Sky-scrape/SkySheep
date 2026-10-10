"""安装器契约（Inno Setup 脚本）的机械校验。

真实安装/卸载（含「卸载时应用运行」）需要 Windows 安装器执行环境，pytest
跑不了整条链路；但脚本里承载关键用户契约的配置项可以逐条锁定，防止被
无意改坏：

- 卸载拦截：运行中的实例必须让卸载失败（S-18 审查：静默卸载中止 + 交互
  卸载提示），互斥体名与桌面单实例一致；
- 覆盖安装防呆：AppMutex 防止应用运行时被覆盖出「半新半旧」目录；
- 静默部署开关：/CURRENTUSER 降权、/RESTARTAPP 由程序内更新链路使用；
- 用户数据边界：卸载清理只针对安装目录，绝不触碰 ~/.skysheep；
- 版本号与 pyproject 一致（发版五处一致的一部分，另见 check_versions）。

另有「安装后首次启动」契约落在 test_upgrade_scenarios.py（全新
SKYSHEEP_HOME 首启链路）与 test_desktop_launcher.py（启动兜底）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[1]
_ISS = _ENGINE / "tools" / "installer.iss"


@pytest.fixture(scope="module")
def iss_text() -> str:
    assert _ISS.is_file(), f"installer.iss 必须存在：{_ISS}"
    return _ISS.read_text(encoding="utf-8")


def test_app_mutex_matches_desktop_singleton(iss_text, monkeypatch):
    """安装器与桌面启动器共用 default 身份的单实例互斥体。"""
    from skysheep.instance import mutex_name

    # 固定默认身份：环境里若无 SKYSHEEP_INSTANCE 即 default；有则由用例剥掉
    monkeypatch.delenv("SKYSHEEP_INSTANCE", raising=False)
    m = re.search(r"^AppMutex=(.+)$", iss_text, re.MULTILINE)
    assert m, "installer.iss 必须声明 AppMutex"
    assert m.group(1).strip() == mutex_name(), \
        "AppMutex 必须与 skysheep.instance.mutex_name()（default 身份）完全一致"


def test_uninstall_blocks_running_app_both_modes(iss_text):
    """卸载拦截（S-18）：静默模式中止返回非零；交互模式提示退出后重试。"""
    assert "InitializeUninstall" in iss_text, "必须有卸载初始化检查"
    assert "CheckForMutexes" in iss_text, "卸载必须探测运行中的实例互斥体"
    assert "WizardSilent()" in iss_text, "静默卸载路径必须单独处理（不弹窗、直接失败）"
    assert "Result := False" in iss_text, "运行中卸载必须失败"
    # 互斥体列表覆盖 default 与 dev 两个身份（源码版并存不误拦、安装版必须拦）
    assert "SkySheepDesktopSingleton" in iss_text


def test_silent_deploy_switches_allowed(iss_text):
    """/CURRENTUSER 降权开关保留（静默安装/沙箱验收依赖，P3-18）。"""
    assert "PrivilegesRequiredOverridesAllowed" in iss_text
    assert "commandline" in iss_text


def test_restart_app_for_inplace_update(iss_text):
    """程序内更新的 /RESTARTAPP：装完以原用户身份重新拉起新版。"""
    assert "RestartAppRequested" in iss_text
    assert "/RESTARTAPP" in iss_text
    assert "runasoriginaluser" in iss_text, \
        "重启必须降回原用户身份（避免继承安装器管理员权限）"


def test_uninstall_never_touches_user_data(iss_text):
    """卸载清理只限安装目录：用户数据 ~/.skysheep 不在卸载范围内。"""
    section = iss_text[iss_text.index("[UninstallDelete]"):]
    # 只看实际指令行（以 Type:/Name: 等键开头），注释行里提到 ~/.skysheep 是说明
    directives = [
        line.strip() for line in section.splitlines()
        if line.strip() and not line.strip().startswith((";", "//", "["))
    ]
    joined = "\n".join(directives)
    assert ".skysheep" not in joined, "UninstallDelete 绝不能触及 ~/.skysheep"
    assert "{app}" in joined


def test_output_name_and_icon(iss_text):
    """产物名固定 SkySheep-<版本>-setup.exe；图标文件真实存在。"""
    assert "OutputBaseFilename=SkySheep-{#MyAppVersion}-setup" in iss_text
    m = re.search(r"SetupIconFile=(.+)$", iss_text, re.MULTILINE)
    assert m
    icon = (_ENGINE / "tools" / m.group(1).strip()).resolve()
    assert icon.is_file(), f"安装器图标不存在：{icon}"


def test_installer_version_matches_pyproject(iss_text):
    """MyAppVersion 与 pyproject.toml 一致（发版五处一致之一）。"""
    import tomllib

    with open(_ENGINE / "pyproject.toml", "rb") as f:
        pyver = tomllib.load(f)["project"]["version"]
    m = re.search(r'#define MyAppVersion "([^"]+)"', iss_text)
    assert m and m.group(1) == pyver, \
        f"installer.iss 版本 {m and m.group(1)} != pyproject {pyver}"
