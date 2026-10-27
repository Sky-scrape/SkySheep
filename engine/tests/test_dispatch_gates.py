# WS dispatch 门禁补齐（P28）的回归固化。
#
# 审计口径（开发跑道 P28 + 2026-10-25 审查报告 L05/L06/L07）：dispatch 的写/执行
# 类 method 必须设防——远程持令牌客户端调用一律拦下，本机界面照常放行（放行侧
# 的语义由 backend.py 的实现决定：用户显式操作不走 PermissionGate，门禁管的是
# 远程可达性，见 backend.fs_write 与 channels/gate.py 的分域说明）。
#
# 本文件对每个新设防的 method 断言两侧：
# - 未设防侧（remote）：被门禁拦下，且拦的是门禁本身——错误文案与注册表
#   local_error 一致（区别于 backend 自身的能力/参数错误）；
# - 放行侧（local）：handler 真实执行，行为与 backend 语义一致。
#
# 新设防清单：
# - local_only：chat.compact（L05）、model.set_reasoning（写 config.toml 服务配置）、
#   app.open_path（L06，桌面弹资源管理器）、app.apply_theme（本机体验面）
# - handler 内收敛（table 条目 local_only=False）：project.task_list / task_add
#   （剥显式 project_id）、task_update / task_delete（先查任务归属）——审查 L07
import pytest
from starlette.testclient import TestClient

from skysheep.models.fake import FakeProvider
from skysheep.server import app as server_app

# 各新设防 method 的门禁文案：远程被拒时必须精确命中（证明拦它的是门禁，
# 不是 backend 的参数/能力错误；文案改动 = 门禁被动过，测试红让人来看）。
GATE_MESSAGES = {
    "chat.compact": "压缩上下文只能在桌面端本机执行",
    "model.set_reasoning": "该操作涉及配置或本机权限，只能在桌面端本机执行",
    "app.open_path": "该操作涉及配置或本机权限，只能在桌面端本机执行",
    "app.apply_theme": "桌面主题联动只能在桌面端本机触发",
}

NEW_LOCAL_ONLY = tuple(GATE_MESSAGES)

# handler 内收敛的项目任务清单方法（守卫在 handler 源码里，见 _project_task_scoped_params
# 与 _ensure_task_in_current_project；锚定断言在 test_local_only_methods.INLINE_GUARDED）
PROJECT_TASK_METHODS = (
    "project.task_list", "project.task_add", "project.task_update", "project.task_delete",
)


def _make_app(home, monkeypatch, *, remote: bool):
    """隔离 SKYSHEEP_HOME 的真实 FastAPI 应用；remote=True 时整连接按远程对待。"""
    monkeypatch.setattr(
        server_app, "_client_is_local", (lambda ws: False) if remote else (lambda ws: True)
    )
    return server_app.create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: FakeProvider([]),
    )


def _rpc(ws, method, params, mid="r1"):
    """单发单收：跳过事件帧，取回本请求的回复帧。"""
    ws.send_json({"id": mid, "method": method, "params": params})
    while True:
        frame = ws.receive_json()
        if frame.get("id") == mid:
            return frame


def test_new_local_only_entries_registered(home):
    """注册表锚定：四个新设防方法必须以 local_only 在表，文案与本文件一致。"""
    for method, message in GATE_MESSAGES.items():
        entry = server_app._WS_METHODS.get(method)
        assert entry is not None, f"{method} 不在 _WS_METHODS（方法被改名/移除？）"
        assert entry.local_only, f"{method} 丢了 local_only 门禁"
        assert entry.local_error == message, f"{method} 的门禁文案变了：{entry.local_error!r}"


def test_remote_client_blocked_on_new_gates(home, monkeypatch):
    """未确认侧（远程来源）：每个新设防写/执行 method 被门禁拦下，文案可辨。"""
    app = _make_app(home, monkeypatch, remote=True)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        for i, method in enumerate(NEW_LOCAL_ONLY):
            frame = _rpc(ws, method, {}, mid=f"deny-{i}")
            assert frame["ok"] is False, f"远程竟能成功调用 {method}: {frame}"
            assert frame["error"] == GATE_MESSAGES[method], (
                f"{method} 的拒绝不是门禁文案（门禁被绕过或文案漂移）：{frame['error']!r}"
            )


def test_local_client_passes_new_gates(home, monkeypatch):
    """确认侧（本机界面）：门禁放行，handler 真实执行且行为落 backend 语义。"""
    from skysheep import support, wintheme

    opened: list[str] = []
    monkeypatch.setattr(support, "open_folder", lambda path: opened.append(str(path)))

    app = _make_app(home, monkeypatch, remote=False)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        # app.open_path：真走到 backend（open_folder 被调、返回隔离 home 的 logs 目录）
        frame = _rpc(ws, "app.open_path", {"kind": "logs"})
        assert frame["ok"], f"本机调用 app.open_path 被拦：{frame}"
        assert opened, "门禁放行了但 handler 没执行（open_folder 未被调用）"
        assert frame["result"]["path"].endswith("logs")

        # chat.compact：到达 backend（空历史返回 compacted=False，而不是门禁文案）
        frame = _rpc(ws, "chat.compact", {})
        assert frame["ok"], f"本机调用 chat.compact 被拦：{frame}"
        assert "compacted" in frame["result"]

        # model.set_reasoning：门禁放行后由 backend 语义接管——fake provider
        # 不支持思考强度，报的是能力错误而非门禁文案（门禁透明的证据）
        frame = _rpc(ws, "model.set_reasoning", {"effort": "high"})
        assert frame["ok"] is False
        assert "不支持调整思考强度" in frame["error"], (
            f"预期 backend 能力错误，实际：{frame['error']!r}"
        )
        assert frame["error"] != GATE_MESSAGES["model.set_reasoning"]

        # app.apply_theme：本机切换照常（wintheme 纯内存态，无窗口也安全）
        frame = _rpc(ws, "app.apply_theme", {"theme": "paper"})
        assert frame["ok"], f"本机调用 app.apply_theme 被拦：{frame}"
        assert wintheme.current_theme_mode() == "paper"


# ---- project.task_* 的 handler 内收敛（审查 L07） ----


class _StubStore:
    def __init__(self, tasks):
        self._tasks = tasks

    async def get_project_task(self, task_id):
        return self._tasks.get(int(task_id))


class _StubBackend:
    """最小 backend 桩：只暴露 task_* handler 触到的面，记录穿透调用。"""

    def __init__(self, tasks=None, cur_pid=7):
        self.store = _StubStore(tasks or {})
        self.cur_pid = cur_pid
        self.calls = []

    def _cur_project_id(self):
        return self.cur_pid

    async def project_task_list(self, project_id=None):
        self.calls.append(("list", project_id))
        return {"project_id": project_id, "tasks": []}

    async def project_task_add(self, params):
        self.calls.append(("add", params.get("project_id")))
        return {"task": {}}

    async def project_task_update(self, params):
        self.calls.append(("update", params.get("id")))
        return {"task": {}}

    async def project_task_delete(self, params):
        self.calls.append(("delete", params.get("id")))
        return {"removed": params.get("id")}


def _emit(_ev):  # handler 签名要求的 emit；这些方法不推事件
    raise AssertionError("project.task_* 不应发出事件")


async def test_project_task_list_add_scoped_for_remote():
    """task_list / task_add：远程显式传的其他 project_id 被剥掉，收敛当前项目。"""
    from skysheep.server._dispatch_files import _h_project_task_add, _h_project_task_list

    backend = _StubBackend(cur_pid=7)
    # 本机：显式 project_id 原样透传（跨项目管理是本机既有语义）
    await _h_project_task_list(backend, {"project_id": 42}, _emit, True)
    assert backend.calls[-1] == ("list", 42)
    await _h_project_task_add(backend, {"title": "x", "project_id": 42}, _emit, True)
    assert backend.calls[-1] == ("add", 42)
    # 远程：显式 project_id 一律剥掉（落到 backend 的当前项目缺省）
    await _h_project_task_list(backend, {"project_id": 42}, _emit, False)
    assert backend.calls[-1] == ("list", None)
    await _h_project_task_add(backend, {"title": "x", "project_id": 42}, _emit, False)
    assert backend.calls[-1] == ("add", None)
    # 远程未传：本就不带 project_id，原样透传
    await _h_project_task_list(backend, {}, _emit, False)
    assert backend.calls[-1] == ("list", None)


async def test_project_task_update_delete_scoped_for_remote():
    """task_update / task_delete：远程只许动当前项目的任务，其余一律拒绝。"""
    from skysheep.server._dispatch_files import _h_project_task_delete, _h_project_task_update

    backend = _StubBackend(
        tasks={1: {"id": 1, "project_id": 7}, 2: {"id": 2, "project_id": 9}},
        cur_pid=7,
    )
    # 本机：不查归属，任意 id 原样透传（含其他项目——分屏列既有语义）
    await _h_project_task_update(backend, {"id": 2, "done": True}, _emit, True)
    assert backend.calls[-1] == ("update", 2)
    await _h_project_task_delete(backend, {"id": 2}, _emit, True)
    assert backend.calls[-1] == ("delete", 2)
    # 远程：当前项目的任务放行
    await _h_project_task_update(backend, {"id": 1, "done": True}, _emit, False)
    assert backend.calls[-1] == ("update", 1)
    await _h_project_task_delete(backend, {"id": 1}, _emit, False)
    assert backend.calls[-1] == ("delete", 1)
    # 远程：其他项目的任务与查无此任务 → 归属拒绝（不给枚举探测区分信号）
    for method, handler, params in (
        ("project.task_update", _h_project_task_update, {"id": 2}),
        ("project.task_delete", _h_project_task_delete, {"id": 2}),
        ("project.task_update", _h_project_task_update, {"id": 999}),
        ("project.task_delete", _h_project_task_delete, {"id": 999}),
    ):
        backend.calls.clear()
        with pytest.raises(RuntimeError, match="只能操作当前项目的任务清单"):
            await handler(backend, params, _emit, False)
        assert not backend.calls, "拒绝路径不应穿透到 backend"
        # 锚定：同样请求走真实 dispatch（远程）也是这条门禁在拦
        entry = server_app._WS_METHODS[method]
        assert entry.local_only is False and entry.local_gate is None


async def test_remote_scope_survives_dispatch_layer():
    """端到端口径锚定：project.task_* 的收敛发生在 dispatch 层，backend 收到的
    参数已是无越权形态——直接断言 handler 与注册表接线的组合行为。"""
    from skysheep.server._dispatch_files import _h_project_task_add

    backend = _StubBackend(cur_pid=7)
    entry = server_app._WS_METHODS["project.task_add"]
    assert entry.handler is _h_project_task_add
    await entry.handler(backend, {"title": "x", "project_id": 42}, _emit, False)
    assert backend.calls[-1] == ("add", None)
