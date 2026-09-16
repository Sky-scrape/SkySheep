"""服务层：FastAPI 应用、WebSocket 后端、静态前端。

导入走懒加载（PEP 562）：desktop 启动链只需要 picker 就能把窗口和动画画出来，
而 ``.app`` / ``.backend`` 会连带 fastapi、uvicorn 和整个引擎核心（实测 2.4 秒+），
提前导入会把"双击到出窗口"拖慢一倍以上。引擎的加载发生在动画页已经可见之后。
"""

from typing import Any

__all__ = ["ServerBackend", "create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from .app import create_app

        return create_app
    if name == "ServerBackend":
        from .backend import ServerBackend

        return ServerBackend
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
