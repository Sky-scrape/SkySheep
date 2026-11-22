"""backend.py Mixin 拆分的结构契约（批 1 起，后续各批沿用）。

守的是「纯搬移」不变量：方法集合、继承关系与 MRO 相对顺序不随拆分漂移。
拆分本身行为等价，这里锚结构契约——防漏搬、防重名遮蔽（返工防线 #1：
并行清单必须同时加契约测试）。
"""

from skysheep.server.backend import BUILTIN_SNIPPETS, ServerBackend
from skysheep.server.backend_parts.advanced import SystemMixin
from skysheep.server.backend_parts.automation import AutomationMixin
from skysheep.server.backend_parts.channels import ChannelsMixin
from skysheep.server.backend_parts.factory import FactoryMixin
from skysheep.server.backend_parts.relay import RelaySettingsMixin
from skysheep.server.backend_parts.remote import RemoteMixin
from skysheep.server.backend_parts.settings_io import SettingsIOMixin
from skysheep.server.backend_parts.skills import SkillsMixin
from skysheep.server.backend_parts.spectate import SpectateMixin
from skysheep.server.backend_parts.subagent import SubagentMixin
from skysheep.server.backend_parts.trust import TrustMixin
from skysheep.server.backend_parts.whitelist import WhitelistMixin
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


def test_batch2_mixins_are_direct_bases_and_members_resolve_there():
    """批 2 外迁块（D6 基础设施与迁移）：方法/类属性全部由五个新 Mixin 提供，
    主类不再自带；经 MRO 仍可达（subagent_store 字段所有权划 D6）。"""
    for mixin in (SkillsMixin, WhitelistMixin, SubagentMixin, TrustMixin,
                  SettingsIOMixin):
        assert mixin in ServerBackend.__mro__, mixin.__name__
    moved_members = {
        SkillsMixin: [
            "toggle_skill", "set_skill_scope", "skill_body", "_skill_root",
            "preview_skill_install", "install_skill", "scan_local_skills",
            "delete_skill", "_skill_safety_lookup", "save_skill_from_session",
            "save_skill_draft", "_project_skills_dir",
        ],
        WhitelistMixin: [
            "list_whitelist_rules", "_enriched_rules", "_stale_fields",
            "_validate_whitelist_rule", "add_whitelist_rule",
            "clear_whitelist_rules", "remove_whitelist_rule",
            "set_whitelist_rule_enabled", "check_whitelist_rule",
            "export_whitelist", "import_whitelist",
        ],
        SubagentMixin: [
            "subagent_settings", "save_subagent_settings", "_subagent_provider",
            "_subagent_registry", "subagents_detail", "save_subagent_builtin",
            "save_subagent_custom", "delete_subagent_custom",
        ],
        TrustMixin: [
            "trust", "_project_mcp_path_if_trusted",
            "_project_skills_dir_if_trusted", "_mcp_reconnect_gate",
            "trust_status", "trust_list", "trust_revoke_path", "trust_grant",
            "trust_revoke", "_reload_project_skills", "recheck_trust_before_turn",
        ],
        SettingsIOMixin: [
            "settings_export", "export_full_data", "settings_preview_import",
            "settings_import", "_strip_accept_edits_from_ui_export",
            "_full_import_zip_path", "_validate_staged_db", "_stage_full_import",
            "_snapshot_config_files", "_restore_config_files_from",
            "_rollback_config_files", "_replace_store_db",
            "_undo_store_db_replace", "preview_import_full", "import_full_data",
            "SETTINGS_EXPORT_FILES", "FULL_IMPORT_KNOWN_TOP",
        ],
    }
    for mixin, members in moved_members.items():
        for name in members:
            assert name in mixin.__dict__, (mixin.__name__, name)
            assert name not in ServerBackend.__dict__, name
            assert hasattr(ServerBackend, name), name  # 经 MRO 仍可达


def test_batch2_settings_io_static_helpers_no_longer_reference_host_class():
    """搬移注记：settings_io 内原 `ServerBackend._xxx` 静态自引用已改为
    SettingsIOMixin 自身——同一实现，MRO 上仅此一份；防止新文件反向依赖宿主。"""
    import inspect

    for meth in (SettingsIOMixin._restore_config_files_from,
                 SettingsIOMixin.import_full_data):
        src = inspect.getsource(meth)
        assert "ServerBackend." not in src
        assert "SettingsIOMixin._" in src


def test_batch3_d4_mixins_are_direct_bases_and_remote_helpers_stay_in_kernel():
    """批 3（D4 渠道与远端）：既有 ChannelsMixin/RemoteMixin 归位=挂域注记，
    零方法搬移（设计 §2.1 D4 表 + §4.1 批 3 行）；_remote_workdir/
    _remote_project_id/is_remote_project 三个小方法按批内决策留内核——设计
    §2.0 把 is_remote_project 列进前门契约、§3.2 把 _remote_project_id_cache
    列进内核字段，三者被内核 _session_project/setup/history_sync 消费，随
    RemoteMixin 外迁会把内核工作目录解析倒挂成对 D4 的依赖。"""
    for mixin in (ChannelsMixin, RemoteMixin):
        assert mixin in ServerBackend.__mro__, mixin.__name__
    for name in ("_remote_workdir", "_remote_project_id", "is_remote_project"):
        assert name in ServerBackend.__dict__, name          # 留在内核主类
        assert name not in ChannelsMixin.__dict__, name      # 未随 D4 外迁
        assert name not in RemoteMixin.__dict__, name
        assert hasattr(ServerBackend, name), name            # 经 MRO 仍可达


def test_batch4_d3_mixins_are_direct_bases_and_emit_moved_to_factory():
    """批 4（D3 自动化与任务）：AutomationMixin/FactoryMixin/SpectateMixin
    归位=挂域注记，零方法搬移（设计 §2.1 D3 表 + §4.1 批 4 行）；唯二例外：
    ① _emit_factory_run 自主类并入 FactoryMixin（factory_run 事件出口，设计
    §4.1 批 4①）——backend.py setup 构造 RunLedger 与 automation 派发
    run_cron_task_core 时经 MRO 照常取用；② 雷区收敛① automation_busy()
    accessor 落 AutomationMixin（_cron_running/_pipeline_running 合判，设计
    §3.3）。"""
    for mixin in (AutomationMixin, FactoryMixin, SpectateMixin):
        assert mixin in ServerBackend.__mro__, mixin.__name__
    assert "_emit_factory_run" in FactoryMixin.__dict__
    assert "_emit_factory_run" not in ServerBackend.__dict__
    assert hasattr(ServerBackend, "_emit_factory_run")       # 经 MRO 仍可达
    assert "automation_busy" in AutomationMixin.__dict__
    assert "automation_busy" not in ServerBackend.__dict__
    assert hasattr(ServerBackend, "automation_busy")         # 经 MRO 仍可达


def test_batch4_lifecycle_busy_check_goes_through_accessor():
    """批 4 雷区收敛①：lifecycle 的备份/恢复预检经 automation_busy() 合判
    cron/流水线在飞，不再裸读 _cron_running/_pipeline_running（防裸读回潮）。"""
    import inspect

    from skysheep.server.backend_parts.lifecycle import LifecycleMixin

    src = inspect.getsource(LifecycleMixin._backup_busy_message)
    assert "self.automation_busy()" in src
    assert "self._cron_running" not in src
    assert "self._pipeline_running" not in src
