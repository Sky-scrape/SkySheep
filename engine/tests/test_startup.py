# 开机自启（startup.py）回归：注册表经注入替身隔离，绝不碰真机 HKCU。
from skysheep import instance, startup


class FakeRegistry:
    """startup._WindowsRegistry 的替身：内存字典模拟 HKCU Run 值。"""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self) -> str | None:
        return self.values.get(startup.value_name())

    def set(self, command: str) -> None:
        self.values[startup.value_name()] = command

    def delete(self) -> None:
        self.values.pop(startup.value_name(), None)


def test_value_name_follows_instance_identity(monkeypatch):
    """两个实例身份的开机项名字必须不同，否则互相覆盖（开关状态显示已开启但被顶掉）。"""
    monkeypatch.delenv("SKYSHEEP_INSTANCE", raising=False)
    plain = startup.value_name()
    monkeypatch.setenv("SKYSHEEP_INSTANCE", "dev")
    dev = startup.value_name()
    assert plain != dev
    assert "dev" in dev


def test_set_enabled_round_trip(monkeypatch):
    monkeypatch.setattr(instance, "autostart_value_name", lambda: "SkySheep")
    reg = FakeRegistry()
    st = startup.set_enabled(True, reg=reg)
    assert st["enabled"] is True
    assert st["command"] == reg.get()
    st2 = startup.set_enabled(False, reg=reg)
    assert st2["enabled"] is False
    assert reg.get() is None


def test_status_reports_stale_command(monkeypatch):
    """注册表里的命令与当前启动方式不一致时，status 要标 stale 提示重新保存。"""
    monkeypatch.setattr(instance, "autostart_value_name", lambda: "SkySheep")
    reg = FakeRegistry()
    reg.set('"C:\\old\\path\\SkySheep.exe"')
    st = startup.status(reg=reg)
    assert st["supported"] is True
    assert st["enabled"] is True
    assert st["stale"] is True
    assert st["current"] == '"C:\\old\\path\\SkySheep.exe"'
    assert st["command"] != st["current"]


def test_status_fresh_command_not_stale(monkeypatch):
    monkeypatch.setattr(instance, "autostart_value_name", lambda: "SkySheep")
    reg = FakeRegistry()
    startup.set_enabled(True, reg=reg)
    st = startup.status(reg=reg)
    assert st["stale"] is False


def test_launch_command_is_quoted_executable(monkeypatch, tmp_path):
    """开发态命令行指向 pythonw + SkySheep.pyw（或 -m 兜底），整段可执行路径都带引号。"""
    monkeypatch.chdir(tmp_path)
    cmd = startup.launch_command()
    assert cmd.startswith('"')
    assert "SkySheep.pyw" in cmd or "-m skysheep.cli.app app" in cmd


def test_unsupported_platform(monkeypatch):
    """非 Windows 平台：status 返回不支持、set_enabled 抛可读错误。"""
    monkeypatch.setattr(startup.sys, "platform", "linux")
    st = startup.status()
    assert st["supported"] is False
    assert st["enabled"] is False
    import pytest
    with pytest.raises(RuntimeError, match="只支持 Windows"):
        startup.set_enabled(True, reg=FakeRegistry())
