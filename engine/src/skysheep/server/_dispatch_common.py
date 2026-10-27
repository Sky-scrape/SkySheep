"""WS dispatch 共用底座（P28 拆分）：注册表条目类、本机专属默认文案、
连接级会话作用域。自 app.py 逐字迁入，行为零变化。app.py 经此模块取
_CONN_SESSION（ws_endpoint 每请求设置作用域）。"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

# 当前请求所属连接正在交互的会话（由 ws_endpoint 在每个请求分发前设置）。
# 远程查询/操作一律以它为授权依据，不读全局 backend.session——后者会被
# 另一个连接或本机窗口切走，导致远程连接的边界随全局状态漂移（安全审查
# B 族：runs/tasks 查询曾因此可跨会话读取）。本机连接不受限（多标签由前端路由）。
_CONN_SESSION: ContextVar[str | None] = ContextVar("skysheep_conn_session", default=None)


def _conn_session() -> str | None:
    return _CONN_SESSION.get()


def _remote_scope(local: bool) -> str | None:
    """远程请求的会话作用域：未绑定会话时返回空串（调用方据此 fail closed）。

    返回 None 表示不限制（本机）。返回空串表示「远程但尚未绑定会话」——
    此时不能回退到全局活动会话，否则拿令牌的客户端能读到本机当前会话。
    """
    if local:
        return None
    return _conn_session() or ""


logger = logging.getLogger("skysheep.security")

# 全部 WS 方法的分支体按域拆在同目录 _dispatch_*.py（表驱动分发的唯一事实来源
# 是 app.py 合并出的 _WS_METHODS）；本模块提供条目类与默认文案。
# local_only / local_gate 的判定与拒绝文案，同原 LOCAL_ONLY_METHODS 表与原内联
# 「if … and not local: raise」逐字一致。

_LOCAL_ONLY_MSG = "该操作涉及配置或本机权限，只能在桌面端本机执行"


@dataclasses.dataclass(frozen=True)
class _WsMethod:
    """单个 WS 方法的注册表条目。

    handler 统一签名 ``async def _h_xxx(backend, params, emit, local) -> dict``，
    分支体自 dispatch 逐字迁入。local_only=True 的方法在 local=False 时由分发
    入口以 local_error 拒绝（默认即原 LOCAL_ONLY_METHODS 表的统一文案）；
    local_gate 是按入参判定的条件门禁（返回错误文案或 None），同样只在
    local=False 时由分发入口调用——等价于旧分支内的 ``if <条件> and not local: raise``。
    """

    handler: Callable[..., Awaitable[dict]]
    local_only: bool = False
    local_gate: Callable[[dict], str | None] | None = None
    local_error: str = _LOCAL_ONLY_MSG
