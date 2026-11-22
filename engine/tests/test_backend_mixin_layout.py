"""backend.py Mixin 拆分的结构契约（批 1 起，后续各批沿用）。

守的是「纯搬移」不变量：方法集合、继承关系与 MRO 相对顺序不随拆分漂移。
拆分本身行为等价，这里锚结构契约——防漏搬、防重名遮蔽（返工防线 #1：
并行清单必须同时加契约测试）。
"""

from skysheep.server.backend import BUILTIN_SNIPPETS, ServerBackend
from skysheep.server.backend_parts.advanced import SystemMixin
from skysheep.server.backend_parts.relay import RelaySettingsMixin
from skysheep.server.backend_parts.workspace_panel import WorkspacePanelMixin

# 拆分前 MRO 上既有的重名（主类 respond_permission 遮蔽 TerminalPanelMixin 的同名
# 方法，主类优先——既有行为，不是拆分引入的）。本契约禁止新增重名。
_PREEXISTING_SHADOWED = {"respond_permission"}


def _mro_names() -> list[str]:
    return [c.__name__ for c in ServerBackend.__mro__]


def test_system_mixin_decoupled_from_relay():
    """批 1 解耦：SystemMixin 不再继承 RelaySettingsMixin；后者改挂主类显式基类。

    解耦前 RelaySettingsMixin 经 SystemMixin 传入 MRO（SystemMixin 紧后）；
    解耦后必须仍是直接可达且相对顺序不变（属性查找语义零变化）。
    """
    assert RelaySettingsMixin not in SystemMixin.__mro__
    names = _mro_names()
    assert "RelaySettingsMixin" in names
    assert "SystemMixin" in names
    assert names.index("SystemMixin") < names.index("RelaySettingsMixin")


def test_workspace_panel_mixin_is_direct_base_and_members_resolve_there():
    """外迁块的方法/类属性全部由 WorkspacePanelMixin 提供，主类不再自带。"""
    assert "WorkspacePanelMixin" in _mro_names()
    moved_members = [
        "_workspace_path", "_validate_rel_path", "fs_read", "fs_write",
        "_file_text_meta", "workspace_files", "_walk_workspace_files",
        "_seed_builtin_snippets", "restore_builtin_snippets", "snippet_initials",
        "snippets_polish", "export_snippets", "import_snippets",
        "_BAD_NAME_CHARS", "_RESERVED_NAMES", "SKIP_DIRS", "MAX_FILES",
    ]
    for name in moved_members:
        assert name in WorkspacePanelMixin.__dict__, name
        assert name not in ServerBackend.__dict__, name
        assert hasattr(ServerBackend, name), name  # 经 MRO 仍可达


def test_builtin_snippets_reexported_from_backend():
    """BUILTIN_SNIPPETS 落位 workspace_panel，backend 模块名继续可引用。"""
    import skysheep.server.backend as bm
    from skysheep.server.backend_parts import workspace_panel as wp

    assert bm.BUILTIN_SNIPPETS is wp.BUILTIN_SNIPPETS
    assert isinstance(BUILTIN_SNIPPETS, list) and BUILTIN_SNIPPETS


def test_no_new_attribute_shadowing_across_mro():
    """MRO 链上不得新增重名遮蔽（同名属性只允许既有那一处）。"""
    seen: dict[str, str] = {}
    dups: set[str] = set()
    for cls in ServerBackend.__mro__:
        for name in vars(cls):
            if name.startswith("__"):
                continue
            if name in seen:
                dups.add(name)
            seen.setdefault(name, cls.__name__)
    assert dups == _PREEXISTING_SHADOWED, dups


def test_mixins_still_have_no_init():
    """施工纪律：Mixin 一律不带 __init__，状态由主类一次铺齐（设计 §2.0/§4.0）。"""
    for cls in ServerBackend.__mro__:
        if cls in (ServerBackend, object):
            continue
        assert "__init__" not in cls.__dict__, cls.__name__
