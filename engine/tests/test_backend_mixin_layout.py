"""backend.py Mixin 拆分的结构契约（批 1 起，后续各批沿用）。

守的是「纯搬移」不变量：方法集合、继承关系与 MRO 相对顺序不随拆分漂移。
拆分本身行为等价，这里锚结构契约——防漏搬、防重名遮蔽（返工防线 #1：
并行清单必须同时加契约测试）。
"""

import types

from skysheep.server.backend import BUILTIN_SNIPPETS, ServerBackend
from skysheep.server.backend_parts.advanced import SystemMixin
from skysheep.server.backend_parts.automation import AutomationMixin
from skysheep.server.backend_parts.channels import ChannelsMixin
from skysheep.server.backend_parts.chat_pipeline import ChatPipelineMixin
from skysheep.server.backend_parts.checkpoints import CheckpointsMixin
from skysheep.server.backend_parts.collab_body import CollabBodyMixin
from skysheep.server.backend_parts.factory import FactoryMixin
from skysheep.server.backend_parts.memory import MemoryMixin
from skysheep.server.backend_parts.permission_modes import PermissionModesMixin
from skysheep.server.backend_parts.project_mgmt import ProjectMgmtMixin
from skysheep.server.backend_parts.relay import RelaySettingsMixin
from skysheep.server.backend_parts.remote import RemoteMixin
from skysheep.server.backend_parts.session_model import SessionModelMixin
from skysheep.server.backend_parts.settings_io import SettingsIOMixin
from skysheep.server.backend_parts.skills import SkillsMixin
from skysheep.server.backend_parts.spectate import SpectateMixin
from skysheep.server.backend_parts.subagent import SubagentMixin
from skysheep.server.backend_parts.trust import TrustMixin
from skysheep.server.backend_parts.usage import UsageMixin
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


def test_batch5a_chat_pipeline_and_checkpoints_members_resolve_there():
    """批 5a（D1 会话与运行时前半）：ChatPipelineMixin（send / _run_turn_pipeline
    等 11 个方法 + 5 个不可变常量）与 CheckpointsMixin（4 个方法 + checkpoint_diff）
    自主类整簇搬入；主类不再自带，经 MRO 仍可达。设计 §4.1 批 5 行（5a = ChatPipeline
    + Checkpoints）。"""
    for mixin in (ChatPipelineMixin, CheckpointsMixin):
        assert mixin in ServerBackend.__mro__, mixin.__name__
    moved_members = {
        ChatPipelineMixin: [
            "_sanitize_images", "_sanitize_refs", "_recent_turn_seconds",
            "_build_refs_context", "send", "_auto_title", "_reload_agent_history",
            "_persist_turn", "_run_turn_pipeline", "_run_queued", "cancel_run",
            "IMAGE_MEDIA_TYPES", "MAX_IMAGES_PER_TURN", "MAX_IMAGE_B64_CHARS",
            "REF_MAX_SESSIONS", "REF_MAX_CHARS",
        ],
        CheckpointsMixin: [
            "_checkpoint_store_for_session", "list_checkpoints",
            "restore_checkpoint", "_check_checkpoint_ownership", "checkpoint_diff",
        ],
    }
    for mixin, members in moved_members.items():
        for name in members:
            assert name in mixin.__dict__, (mixin.__name__, name)
            assert name not in ServerBackend.__dict__, name
            assert hasattr(ServerBackend, name), name  # 经 MRO 仍可达
    # 方法解析到新 Mixin（不是主类里的同名残留）
    assert ServerBackend.send is ChatPipelineMixin.send
    assert ServerBackend._run_turn_pipeline is ChatPipelineMixin._run_turn_pipeline
    assert ServerBackend.checkpoint_diff is CheckpointsMixin.checkpoint_diff
    # 内核事件广播 `_broadcast_ws` 留在主类（设计 §2.0 内核清单），未随迁
    assert "_broadcast_ws" in ServerBackend.__dict__
    assert "_broadcast_ws" not in ChatPipelineMixin.__dict__


def test_batch5a_stream_merger_stays_in_backend_kernel_and_is_injected():
    """红线（设计 §2.0/§6 + 本批 ask 第 4 条）：StreamDeltaMerger 与
    _MERGEABLE_DELTA_KINDS 是模块级内核底座，必须留在 backend.py，不随
    ChatPipelineMixin 迁移。Mixin 运行期用的必须是内核同一个对象——由 backend
    在模块末尾单向注入（backend_parts 不得 import backend，会成环）。

    tests/test_stream_merge.py 与 tests/test_multi_session.py 都从
    `skysheep.server.backend` 取 StreamDeltaMerger，定义位置不能漂。
    """
    import inspect

    import skysheep.server.backend as backend_module
    from skysheep.server.backend_parts import chat_pipeline as chat_module

    assert backend_module.StreamDeltaMerger.__module__ == "skysheep.server.backend"
    assert backend_module._MERGEABLE_DELTA_KINDS
    # 注入的是同一对象（不是第二份定义）
    assert chat_module.StreamDeltaMerger is backend_module.StreamDeltaMerger
    assert chat_module.QueuedTurn is backend_module.QueuedTurn
    assert chat_module._stamp_turn_estimate is backend_module._stamp_turn_estimate
    assert chat_module._PERSIST_HARVEST_TIMEOUT == backend_module._PERSIST_HARVEST_TIMEOUT
    # 新文件里没有第二份定义/常量表
    chat_src = inspect.getsource(chat_module)
    assert "class StreamDeltaMerger" not in chat_src
    assert "_MERGEABLE_DELTA_KINDS = (" not in chat_src


def test_batch5a_checkpoint_diff_helpers_moved_with_checkpoints_mixin():
    """检查点的模块级 diff 助手（MAX_DIFF_CHARS / _decode_bytes / _unified_diff /
    _checkpoint_diff_files）随 CheckpointsMixin 落位 checkpoints.py（设计 §2.1 D1
    表）；主类 checkpoint_diff 的全局作用域里必须是同一份实现，backend 模块名不再
    暴露这些助手（防两份实现漂移）。"""
    import skysheep.server.backend as backend_module
    from skysheep.server.backend_parts import checkpoints as checkpoints_module

    used = ServerBackend.checkpoint_diff.__globals__["_checkpoint_diff_files"]
    assert used is checkpoints_module._checkpoint_diff_files
    assert checkpoints_module.MAX_DIFF_CHARS > 0
    for name in ("MAX_DIFF_CHARS", "_decode_bytes", "_unified_diff", "_checkpoint_diff_files"):
        assert not hasattr(backend_module, name), name


def test_batch6_chat_pipeline_member_checks_converged_to_team_active():
    """批 6 收敛（设计 §3.3 雷区 `_teams`）：批 5a 逐字搬来的 4 处成员判定
    （send 1 处、_run_turn_pipeline 3 处，含「建队轮已开跑、_teams 尚未登记」
    窗口期重验）全部改走 D2 CollabBodyMixin 的 team_active(sid)；send 处的
    `target_sid is not None` 前置留在调用侧。裸访问归零。"""
    import inspect

    src = inspect.getsource(ChatPipelineMixin)
    assert "self._teams" not in src
    assert src.count("self.team_active(") == 4
    assert "in self._teams" not in src
    # 窗口期重验注释原意保留（「建队轮已开跑、_teams 尚未登记」）
    assert "建队轮已开跑、_teams 尚未登记" in src



def test_batch5b_mixins_are_direct_bases_and_members_resolve_there():
    """批 5b（D1 会话与运行时后半）：SessionModelMixin / PermissionModesMixin /
    UsageMixin / ProjectMgmtMixin 自主类整簇搬入；主类不再自带，经 MRO 仍可达且
    解析到新 Mixin（主类残留同名副本会遮蔽），设计 §4.1 批 5 行（5b = SessionModel/
    PermissionModes/Usage/ProjectMgmt）。"""
    for mixin in (SessionModelMixin, PermissionModesMixin, UsageMixin, ProjectMgmtMixin):
        assert mixin in ServerBackend.__mro__, mixin.__name__
    moved_members = {
        SessionModelMixin: [
            "fork_session", "resume_session", "session_model_switch", "session_model_get",
            "session_reasoning_get", "session_reasoning_set", "switch_model",
            "set_default_model", "default_model_state", "_default_model_pref",
            "_aux_model_pref", "aux_model_state", "set_aux_model", "reasoning_state",
            "set_reasoning_effort", "_model_private_sessions",
            "_session_effective_model", "_runtime_provider", "_apply_model_routing",
            "_release_turn_providers", "_close_provider_after", "compact_now",
            "_context_detail", "status", "_estimate_session_context",
        ],
        PermissionModesMixin: [
            "permission_mode", "_apply_gate_accept_pref",
            "_sync_default_accept_to_runtimes", "set_permission_mode",
            "_accept_mode_of", "session_accept_get", "session_accept_set",
        ],
        UsageMixin: [
            "usage_stats", "_fold_usage_cost", "usage_collab_report",
            "usage_pricing_set", "turn_breakdown", "_MARK_ADVERSARIAL",
            "_MARK_ROUNDTABLE", "_MARK_TEAM",
        ],
        ProjectMgmtMixin: [
            "_bind_project", "switch_project", "delete_project", "remove_project",
            "restore_project", "project_admin_list", "_toast_blocking",
        ],
    }
    for mixin, members in moved_members.items():
        for name in members:
            assert name in mixin.__dict__, (mixin.__name__, name)
            assert name not in ServerBackend.__dict__, name
            assert hasattr(ServerBackend, name), name  # 经 MRO 仍可达
            assert getattr(ServerBackend, name) is getattr(mixin, name), name


def test_batch5b_kernel_helpers_injected_or_forwarded():
    """内核共用件不随迁（设计 §2.0/§6）：
    ① 历史窗口口径（HISTORY_SYNC_LIMIT）与历史 brief 组装（_msg_brief /
    _window_briefs）留 backend.py，由内核在模块末尾单向注入 SessionModelMixin；
    ② 轮次日志件随 turn_breakdown 落 usage.py（形参默认值在导入期求值，必须与
    定义同模块），backend 模块名继续转发——tests/test_turn_breakdown.py 与
    tests/test_app_file_logging.py 的既有导入锚点不变。"""
    import skysheep.server.backend as backend_module
    from skysheep.server.backend_parts import session_model as sm
    from skysheep.server.backend_parts import usage as us

    assert sm.HISTORY_SYNC_LIMIT is backend_module.HISTORY_SYNC_LIMIT
    assert sm._msg_brief is backend_module._msg_brief
    assert sm._window_briefs is backend_module._window_briefs
    assert sm._msg_brief.__module__ == "skysheep.server.backend"
    for name in ("TURN_LOG_LIMIT_DEFAULT", "TURN_LOG_LIMIT_MAX",
                 "_load_turn_rows", "_turn_row", "_log_line_ts"):
        assert getattr(backend_module, name) is getattr(us, name), name
    assert UsageMixin.turn_breakdown.__defaults__ == ("", us.TURN_LOG_LIMIT_DEFAULT)


def test_batch5b_memory_helpers_merged_into_memory_mixin():
    """_memory_retrieval_query / compose_system / compose_system_for 并入
    MemoryMixin（设计 §2.1 D1 记忆类小件）：系统提示词组装单处实现，主类不再自带。"""

    for name in ("_memory_retrieval_query", "compose_system", "compose_system_for"):
        assert name in MemoryMixin.__dict__, name
        assert name not in ServerBackend.__dict__, name
        assert getattr(ServerBackend, name) is getattr(MemoryMixin, name), name


def test_batch6_project_switch_converged_to_write_wrapper():
    """批 6 收敛（设计 §3.3 雷区 `_teams`）：ProjectMgmtMixin 的 _bind_project
    切项目中断块（own-ctx 分屏列豁免 + interrupt_reason 注记 + 条件 persist）
    整块收在 D2 CollabBodyMixin.team_interrupt_for_project_switch()；本文件只
    调封装，不再碰 `_teams` / `_teams_interrupted`。"""
    import inspect

    proj_src = inspect.getsource(ProjectMgmtMixin)
    assert "self._teams" not in proj_src
    assert "self.team_interrupt_for_project_switch()" in proj_src
    # 团队块的标记随块进了 D2 封装（rt.ctx.own 另有 runtime 失效循环在用，
    # 故只锚团队块独有的三处标记）
    assert "self._teams.pop(sid)" not in proj_src
    assert "self._teams_interrupted[sid]" not in proj_src
    assert "项目已切换，团队已中断" not in proj_src
    assert "_persist_team_state()" not in proj_src


def test_batch5b_session_history_window_stays_in_kernel():
    """设计 §2.1 D1 外迁清单未列 peek_session_messages / session_messages_earlier：
    会话只读历史窗口（分屏 peek 与 messages.earlier）随内核留 backend.py。"""
    for name in ("peek_session_messages", "session_messages_earlier"):
        assert name in ServerBackend.__dict__, name
        assert name not in SessionModelMixin.__dict__, name
        assert hasattr(ServerBackend, name), name

# ---- 批 6（D2 多模型协作）：CollabBodyMixin 外迁与 _teams 跨域收敛 ----

_BATCH6_MOVED_MEMBERS = [
    "_build_member_provider", "_auto_pool_rank", "_auto_pool_services", "_resolve_members",
    "roundtable_detail", "roundtable_save",
    "_adversarial_body", "adversarial_detail", "adversarial_save", "adversarial_export",
    "adversarial_fix_prompt", "adversarial_compare",
    "_load_team_state", "_persist_team_state", "_release_team_providers",
    "_team_provider_context_limit", "_resolve_team_members", "_template_member_specs",
    "_expand_auto_pool", "_template_team_specs", "_template_allowed_tools",
    "_team_registry_for_template", "_team_budget_check", "_team_message_sink",
    "_team_create", "_team_meta", "_team_body", "_flush_team_usage", "_team_for",
    "_cancel_team_turn_and_settle", "team_get", "team_room", "_push_team_task_event",
    "team_task_add", "adversarial_to_team_tasks", "roundtable_to_team_task",
    "team_task_update", "team_stop", "team_deliver", "team_takeover",
    "team_config_detail", "team_config_save", "team_template_list", "team_template_save",
    "team_template_remove", "team_resident_set", "team_resident_clear",
    "team_resident_get", "team_log",
    # 类属性（frozenset，不可变）：_auto_pool_rank 的坏服务降权表
    "_BAD_POOL_CATEGORIES",
]


def test_batch6_collab_body_mixin_is_direct_base_and_members_resolve_there():
    """批 6（D2）：CollabBodyMixin 为直接基类，49 个方法 + _BAD_POOL_CATEGORIES
    自主类整段搬入；主类不再自带，经 MRO 仍可达且解析到新 Mixin（设计 §4.1 批 6）。"""
    assert CollabBodyMixin in ServerBackend.__bases__
    names = _mro_names()
    assert names.index("CollabBodyMixin") == len(names) - 2  # 紧邻 object，既有顺序未扰动
    for name in _BATCH6_MOVED_MEMBERS:
        assert name in CollabBodyMixin.__dict__, name
        assert name not in ServerBackend.__dict__, name
        assert getattr(ServerBackend, name) is getattr(CollabBodyMixin, name), name
    assert len(_BATCH6_MOVED_MEMBERS) == 50  # 49 方法 + 1 类属性


def test_batch6_mixin_has_no_init_and_no_mutable_class_attrs():
    """纪律：新 Mixin 无 __init__、无类级可变属性（状态由主类 __init__ 一次铺齐）。"""
    assert "__init__" not in CollabBodyMixin.__dict__
    class_attrs = {
        k: v for k, v in CollabBodyMixin.__dict__.items()
        if not callable(v) and not k.startswith("__")
    }
    assert set(class_attrs) == {"_BAD_POOL_CATEGORIES"}
    assert isinstance(class_attrs["_BAD_POOL_CATEGORIES"], frozenset)


def _fake_collab_body_host():
    """最小宿主：只铺五个跨域入口用到的状态与两个被调依赖。"""

    class Fake(CollabBodyMixin):
        def __init__(self):
            self._teams = {}
            self._teams_interrupted = {}
            self._team_state_path = None
            self.runtimes = {}
            self.persisted = 0
            self.released = []

        def _persist_team_state(self):
            self.persisted += 1

        def _release_team_providers(self, orch):
            self.released.append(orch)

    return Fake


class _FakeOrch:
    def __init__(self, team_id):
        self.team_id = team_id
        self.roster = [types.SimpleNamespace(name="队员甲")]
        self.director_mode = "user"


def test_batch6_teams_accessors_are_read_only_dict_forms():
    """三个只读 accessor 按形态对号（设计 §3.3）：成员判定 / 键式取 / dict 本体遍历。"""
    be = _fake_collab_body_host()()
    o1, o2 = object(), object()
    be._teams = {"s1": o1, "s2": o2}
    # 成员判定：sid in self._teams
    assert be.team_active("s1") is True
    assert be.team_active("s3") is False
    # 键式取：.get(sid)
    assert be.team_orchestrator("s2") is o2
    assert be.team_orchestrator("s3") is None
    # 返回 dict 本体（不是 values 视图、不是副本）：values 遍历与 items 键遍历都可用
    assert be.team_orchestrators() is be._teams
    assert list(be.team_orchestrators().keys()) == ["s1", "s2"]
    assert list(be.team_orchestrators().values()) == [o1, o2]
    # getattr 防御沿既有写法：setup 早期尚未铺字段时按空表处理
    assert CollabBodyMixin.team_orchestrators(object.__new__(CollabBodyMixin)) == {}


def test_batch6_team_write_wrappers_semantics():
    """两个写封装与原块逐字等价：摘队（返回编排器、条件 persist）与切项目中断
    （own-ctx 分屏列豁免、释放 provider、interrupt_reason 注记、条件 persist）。"""
    Fake = _fake_collab_body_host()

    # ① team_detach_for_delete ≡ pop 编排器 → pop 中断记录 → 条件 _persist_team_state
    be = Fake()
    orch = _FakeOrch("t1")
    be._teams = {"sid": orch}
    be._teams_interrupted = {"sid": {"session_id": "sid"}}
    assert be.team_detach_for_delete("sid") is orch
    assert be._teams == {}
    assert be._teams_interrupted == {}
    assert be.persisted == 1  # 有摘除 → 回写
    assert be.team_detach_for_delete("missing") is None
    assert be.persisted == 1  # 无摘除且 _team_state_path 为 None → 不回写

    # ② team_interrupt_for_project_switch：own-ctx 分屏列的团队不随主栏切换失效
    be = Fake()
    own, alien = _FakeOrch("t-own"), _FakeOrch("t-alien")
    be.runtimes = {
        "own": types.SimpleNamespace(ctx=types.SimpleNamespace(own=True)),
        "main": types.SimpleNamespace(ctx=types.SimpleNamespace(own=False)),
    }
    be._teams = {"own": own, "main": alien}
    be.team_interrupt_for_project_switch()
    assert list(be._teams) == ["own"]
    assert be.released == [alien]
    rec = be._teams_interrupted["main"]
    assert rec["team_id"] == "t-alien"
    assert rec["status"] == "interrupted"
    assert rec["interrupt_reason"] == "项目已切换，团队已中断"
    assert rec["roster"] == ["队员甲"]
    assert be.persisted == 1

    # 源码级：豁免过滤与中断原因确实在封装里（不是靠调用方拼）
    import inspect

    src = inspect.getsource(CollabBodyMixin.team_interrupt_for_project_switch)
    for token in ("rt.ctx.own", "项目已切换，团队已中断", "_release_team_providers",
                  "_persist_team_state"):
        assert token in src, token
    src = inspect.getsource(CollabBodyMixin.team_detach_for_delete)
    for token in ("self._teams.pop(session_id, None)",
                  "self._teams_interrupted.pop(session_id, None)",
                  "_persist_team_state()"):
        assert token in src, token


def test_batch6_teams_bare_access_only_in_collab_body():
    """收敛后不再有跨域裸读：collab_body.py 之外只剩主类 __init__ 的字段声明。

    15 处跨域调用点（2 写 + 13 读）按域点名锚定形态；分发层第 16 处
    （_dispatch_chat.py 的 _h_permission_queue）按降级条款保留裸读 + 注记。"""
    import inspect
    import pathlib
    import re

    import skysheep.server as server_pkg
    import skysheep.server.backend as backend_module
    from skysheep.server.backend_parts import (
        advanced,
        automation,
        channels,
        lifecycle,
        migration,
        mods,
        project_mgmt,
        spectate,
    )

    root = pathlib.Path(server_pkg.__file__).parent
    files = sorted(root.glob("*.py")) + sorted((root / "backend_parts").glob("*.py"))
    hits = []
    for path in files:
        if path.name == "collab_body.py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"self\._teams\b", line):  # 排除 _teams_interrupted
                hits.append((path.name, lineno, line.strip()))
    # 唯一例外：主类 __init__ 的字段声明（状态仍由主类一次铺齐）
    assert len(hits) == 1, hits
    assert hits[0][0] == "backend.py"
    assert hits[0][2].startswith("self._teams: dict[str, TeamOrchestrator] = {}")

    # 15 处调用点逐域锚形态
    assert inspect.getsource(backend_module.ServerBackend.respond_permission).count(
        "self.team_orchestrators().values()") == 1
    assert inspect.getsource(advanced).count("self.team_orchestrators().values()") == 1
    assert inspect.getsource(mods).count("self.team_orchestrators().values()") == 2
    assert inspect.getsource(spectate).count("self.team_orchestrators().values()") == 1
    assert inspect.getsource(migration).count("self.team_orchestrators().items()") == 1
    assert inspect.getsource(channels).count("self.team_orchestrator(session_id)") == 1
    assert inspect.getsource(automation).count("self.team_orchestrator(resident.session_id)") == 2
    assert inspect.getsource(lifecycle).count(
        "self.team_detach_for_delete(session_id)") == 1
    assert inspect.getsource(project_mgmt).count(
        "self.team_interrupt_for_project_switch()") == 1


def test_batch6_collab_body_helpers_moved_and_kernel_injections():
    """模块级 helper 随迁、两个对抗助手只转发同一对象、_PERSIST_HARVEST_TIMEOUT
    留内核单向注入（设计 §4.0/批 6 helper 清单）。"""
    import inspect

    import skysheep.server.backend as backend_module
    from skysheep.server.backend_parts import collab_body

    # 两个对抗助手：tests/test_adversarial.py 从 backend 模块 import，必须同一对象
    assert backend_module._adversarial_fingerprints is collab_body._adversarial_fingerprints
    assert backend_module._adversarial_report_markdown is collab_body._adversarial_report_markdown
    assert collab_body._adversarial_fingerprints.__module__ == (
        "skysheep.server.backend_parts.collab_body"
    )
    assert "def _adversarial_fingerprints" not in inspect.getsource(backend_module)

    # 域内 helper/常量随迁：backend 模块名不再暴露（防第二份定义漂移）
    for name in ("_TEAM_INTERRUPTED_KEEP", "TEAM_DIGEST_EXCERPT_CHARS", "_team_digest",
                 "_ROOM_TASK_STATUSES", "_room_empty_stats", "_room_stats",
                 "_ADV_VERDICT_LABELS", "_ADV_SEV_LABELS", "_ADV_ROLE_LABELS"):
        assert hasattr(collab_body, name), name
        assert not hasattr(backend_module, name), name

    # 内核常量留 backend.py，运行期单向注入 D2 模块（同 chat_pipeline 先例）
    assert backend_module._PERSIST_HARVEST_TIMEOUT == 10.0
    assert collab_body._PERSIST_HARVEST_TIMEOUT == backend_module._PERSIST_HARVEST_TIMEOUT
