"""实例身份：源码版与安装版并存互不干扰（见 src/skysheep/instance.py）。

这里守四件事：
  1. 身份名解析是白名单的——不合法一律退回默认身份，不能让应用起不来；
  2. 数据目录 / 标题 / 互斥体名 / 自启值名四处口径一致，且都随身份变化；
  3. ``SKYSHEEP_HOME`` 优先级最高（显式指定目录时不再叠加实例后缀）；
  4. desktop.py 为提速镜像了一份规则，两份必须逐字符一致——否则启动器认
     一份身份、引擎认另一份，隔离会半途而废（数据分开了但互斥体还是一个）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from skysheep import instance

_ENGINE = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("skysheep_desktop_instance", _ENGINE / "desktop.py")
desktop = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(desktop)


@pytest.fixture
def clean_env(monkeypatch):
    """清掉两个身份相关环境变量，避免开发机上的真实取值影响断言。"""
    monkeypatch.delenv(instance.INSTANCE_ENV, raising=False)
    monkeypatch.delenv("SKYSHEEP_HOME", raising=False)


# ---- 默认身份：与历史版本表现完全一致 ----


def test_default_identity_matches_legacy(clean_env):
    """不设环境变量时，四处取值必须与 2.1 及以前完全相同（升级零感知）。"""
    assert instance.instance_name() is None
    assert instance.data_home() == Path.home() / ".skysheep"
    assert instance.app_title() == "SkySheep"
    assert instance.mutex_name() == "Local\\SkySheepDesktopSingleton"
    assert instance.autostart_value_name() == "SkySheep"


def test_default_has_no_problem(clean_env):
    assert instance.instance_problem() is None


# ---- 指定身份：四处同时改口径 ----


def test_named_instance_changes_all_four(clean_env, monkeypatch):
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    assert instance.instance_name() == "dev"
    assert instance.data_home() == Path.home() / ".skysheep-dev"
    assert instance.app_title() == "SkySheep [dev]"
    assert instance.mutex_name() == "Local\\SkySheepDesktopSingleton-dev"
    assert instance.autostart_value_name() == "SkySheep-dev"


def test_distinct_instances_do_not_collide(clean_env, monkeypatch):
    """两个身份在数据目录与互斥体上都必须不同——这是隔离的全部意义。"""
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    dev_home, dev_mutex = instance.data_home(), instance.mutex_name()
    monkeypatch.setenv(instance.INSTANCE_ENV, "beta")
    assert instance.data_home() != dev_home
    assert instance.mutex_name() != dev_mutex


@pytest.mark.parametrize("name", ["dev", "test1", "a", "my-instance", "my_instance", "x" * 24])
def test_valid_instance_names(clean_env, monkeypatch, name):
    monkeypatch.setenv(instance.INSTANCE_ENV, name)
    assert instance.instance_name() == name
    assert instance.instance_problem() is None


@pytest.mark.parametrize(
    "bad",
    [
        "Dev",              # 大写：Local\ 前缀下会变成另一个互斥体，隔离失效
        "de v",             # 空格
        "../escape",        # 路径穿越：会进数据目录名
        "a/b",              # 路径分隔符
        "x" * 25,           # 超长
        "-lead",            # 不能以连字符开头
        "_lead",            # 不能以下划线开头
        "",                 # 空串等同未设置
    ],
)
def test_invalid_instance_names_fall_back_with_reason(clean_env, monkeypatch, bad):
    monkeypatch.setenv(instance.INSTANCE_ENV, bad)
    assert instance.instance_name() is None
    assert instance.data_home() == Path.home() / ".skysheep"
    if bad:
        assert instance.instance_problem() is not None
    else:
        assert instance.instance_problem() is None


def test_whitespace_padded_name_is_treated_as_set(clean_env, monkeypatch):
    """前后空白先 strip 再判定：'  dev  ' 是明确的 dev 意图，不该退回默认。"""
    monkeypatch.setenv(instance.INSTANCE_ENV, "  dev  ")
    assert instance.instance_name() == "dev"


# ---- SKYSHEEP_HOME 优先级最高 ----


def test_home_env_wins_over_instance(clean_env, monkeypatch, tmp_path):
    """显式目录完全按它走：测试与临时环境依赖这个语义，不能叠加后缀。"""
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "custom"))
    assert instance.data_home() == tmp_path / "custom"


def test_home_env_alone_works(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "custom"))
    assert instance.data_home() == tmp_path / "custom"


def test_config_skysheep_home_uses_instance(clean_env, monkeypatch):
    """引擎侧入口（config.skysheep_home）必须与 instance 同源。"""
    from skysheep.config import skysheep_home

    assert skysheep_home() == Path.home() / ".skysheep"
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    assert skysheep_home() == Path.home() / ".skysheep-dev"


# ---- desktop.py 的镜像实现必须与引擎逐字符一致 ----


def test_desktop_mirrors_engine_default(clean_env):
    assert desktop.instance_name() is None
    assert desktop.app_title() == instance.app_title()
    assert desktop.mutex_name() == instance.mutex_name()
    assert desktop._home_dir() == instance.data_home()


def test_desktop_mirrors_engine_named(clean_env, monkeypatch):
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    assert desktop.instance_name() == instance.instance_name()
    assert desktop.app_title() == instance.app_title()
    assert desktop.mutex_name() == instance.mutex_name()
    assert desktop._home_dir() == instance.data_home()


def test_desktop_mirrors_engine_home_priority(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "custom"))
    assert desktop._home_dir() == instance.data_home() == tmp_path / "custom"


@pytest.mark.parametrize("bad", ["Dev", "a/b", "x" * 25, "-x"])
def test_desktop_rejects_same_names_as_engine(clean_env, monkeypatch, bad):
    """两边白名单必须同口径：desktop 放行而引擎拒绝会出现"半隔离"状态。"""
    monkeypatch.setenv(instance.INSTANCE_ENV, bad)
    assert desktop.instance_name() == instance.instance_name() is None
    assert desktop.mutex_name() == instance.mutex_name()
    assert desktop.app_title() == instance.app_title()


def test_desktop_identity_used_by_single_instance_logic(clean_env, monkeypatch):
    """单实例判定必须真的用上实例化标题（改了名字但匹配仍用常量 = 白改）。"""
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    # 窗口标题在创建时就固定了（真实进程不会因为别的进程改变环境变量而改名）
    window_title = desktop.app_title()

    class FakeUser32:
        def GetWindowTextLengthW(self, hwnd):
            return len(window_title) + 1

        def GetWindowTextW(self, hwnd, buf, max_len):
            buf.value = window_title
            return len(window_title)

        def IsWindowVisible(self, hwnd):
            return True

        def EnumWindows(self, cb, _lp):
            cb(111, None)
            return True

    monkeypatch.setattr(desktop.ctypes, "windll", type("W", (), {"user32": FakeUser32()})())
    monkeypatch.setattr(desktop, "_is_hung", lambda hwnd: False)
    # 本进程身份与窗口标题一致 → 认作自己人
    assert desktop._find_main_hwnd() == 111
    # 本进程换成另一个身份后，那个窗口不再被认作自己（这正是两份能各自
    # 开一个实例、且不会把对方当卡死实例接管的原因）
    monkeypatch.setenv(instance.INSTANCE_ENV, "beta")
    assert desktop._find_main_hwnd() == 0


# ---- 自启值名 ----


def test_autostart_value_name_follows_instance(clean_env, monkeypatch):
    from skysheep import startup

    assert startup.value_name() == "SkySheep"
    monkeypatch.setenv(instance.INSTANCE_ENV, "dev")
    assert startup.value_name() == "SkySheep-dev"
    # 该值名必须真的进入注册表调用（换名但读写仍用常量 = 白改）
    calls: list[tuple] = []

    class FakeWinreg:
        HKEY_CURRENT_USER = object()
        REG_SZ = 1
        KEY_SET_VALUE = 2

        class _Key:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        @staticmethod
        def CreateKeyEx(root, path):
            return FakeWinreg._Key()

        @staticmethod
        def SetValueEx(key, name, reserved, typ, value):
            calls.append((name, value))

    reg = startup._WindowsRegistry()
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg)
    reg.set("cmd")
    assert calls == [("SkySheep-dev", "cmd")]


def test_instance_module_not_imported_by_desktop_at_module_level():
    """desktop.py 不能在模块级导入 skysheep——引擎导入约 2.4s，必须等窗口可见。"""
    source = (_ENGINE / "desktop.py").read_text(encoding="utf-8")
    head = source.split("def app_dir", 1)[0]
    assert "import skysheep" not in head
    assert "from skysheep" not in head
