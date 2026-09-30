"""测试共享夹具。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from skysheep.models.fake import FakeProvider  # noqa: F401  (re-exported for tests)
from skysheep.session.store import SessionStore

__all__ = ["FakeProvider", "store", "home", "read_app_bundle"]

# 用例可能从任意 cwd 启动（仓库根 / engine/），静态资源一律按本文件定位成
# 绝对路径；与 server/app.py:299 及 test_settings_extras.py 的写法同源。
_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "skysheep" / "server" / "static"


def read_app_bundle() -> str:
    """按 index.html 引入顺序拼接全部手写前端 JS（app.js + app-*.js + app-tools.js）。

    app.js 分区搬迁（拆出 app-<区名>.js，普通 script 零构建）后，前端源码锚定
    测试读这份拼接文本：它是原 app.js 的重排超集（搬走的块原样出现在新文件
    段落里），`... in js` 子串断言语义保持。文件清单以 index.html 的 script
    标签为单一事实来源，新分区文件登记标签即自动并入；尚未创建的文件跳过。
    """
    html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    parts = []
    for name in re.findall(r'src="/static/(app[^"]*\.js)"', html):
        path = _STATIC_DIR / name
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """隔离的 SkySheep home + 临时项目目录（proj/）。"""
    monkeypatch.setenv("SKYSHEEP_HOME", str(tmp_path / "home"))
    (tmp_path / "proj").mkdir()
    return tmp_path


@pytest.fixture
async def store(tmp_path):
    s = await SessionStore(tmp_path / "test.db").connect()
    yield s
    await s.close()
