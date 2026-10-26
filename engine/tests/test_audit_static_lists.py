"""审计 M18 + L29（同族：静态资源缓存清单漂移）修复的细节用例。

app.js 按分区拆出的模块（app-about / app-mods / app-advanced / app-relay /
app-archive / app-browser）此前从未补进 app.py 的 no_store 清单，脚本标签又无
版本参数——升级后 WebView 按启发式缓存继续供旧脚本，与新 backend / 新 app.js
混跑；sw.js 的 SHELL_URLS 同样缺 app-archive.js / app-browser.js，SW_VERSION
也不随前端变更递增。这里按三条线锁死：

1. no_store 集合含全部点名模块（含工厂 WIP 新拆的三个，同属 index.html 顶层
   加载清单）；
2. SW_VERSION 不低于本次修复落点 v75（只设下限，后续正常递增不受牵制）；
3. 真实 HTTP 响应头实测 Cache-Control: no-store，vendor 仍走 ETag 协商。

三份清单的整体对齐（index.html ⊆ no_store ∩ SHELL_URLS）由
test_frontend_wiring.py::test_static_lists_stay_aligned_with_index_html 守住，
这里不重复，只补各自的细节契约。
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from skysheep.models.fake import FakeProvider
from skysheep.server import create_app
from skysheep.server.app import STATIC_DIR

ROOT = Path(__file__).resolve().parents[1]
APP_PY = ROOT / "src" / "skysheep" / "server" / "app.py"

# M18 点名的 6 个拆分模块 + L29 补充的工厂 WIP 三个（同为 index.html 顶层模块）
AUDIT_MODULES = [
    "app-about.js",
    "app-mods.js",
    "app-advanced.js",
    "app-relay.js",
    "app-archive.js",
    "app-browser.js",
    "app-factory-scene.js",
    "app-factory-drawer.js",
    "app-factory-draw.js",
]


def _no_store_paths() -> set[str]:
    """从 app.py 源码抽 _no_store_static 的 no_store 集合字面量。

    app.py import 即装配整台 server，这里按源码正则解析（与
    test_frontend_wiring._app_py_no_store_paths 同法，两处共用一条口径）。
    """
    src = APP_PY.read_text(encoding="utf-8")
    m = re.search(r"def _no_store_static\(.*?no_store = \{(.*?)\n    \}", src, re.S)
    assert m, "app.py 必须保留 _no_store_static 的 no_store 集合字面量"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def test_no_store_covers_all_audit_modules():
    """no_store 集合必须含 M18 点名的 6 个拆分模块与工厂 WIP 的 3 个新模块。"""
    no_store = _no_store_paths()
    missing = [f"/static/{n}" for n in AUDIT_MODULES if f"/static/{n}" not in no_store]
    assert not missing, (
        "no_store 缺审计点名的拆分模块（升级后旧脚本与新 app.js 混跑）：\n  "
        + "\n  ".join(missing))


def test_sw_version_incremented_past_audit_baseline():
    """SW_VERSION 必须不低于本次修复落点 v75。

    审计发现时是 v65（提交态）；工厂 WIP 已递增到 v74，本次修复随清单变更
    递增到 v75。这里只设下限防回退：之后每次改前端清单照常递增即可。
    """
    src = (Path(STATIC_DIR) / "sw.js").read_text(encoding="utf-8")
    m = re.search(r'const SW_VERSION = "skysheep-shell-v(\d+)"', src)
    assert m, "sw.js 必须保留 SW_VERSION 版本常量"
    assert int(m.group(1)) >= 75, (
        f"SW_VERSION=v{m.group(1)} 低于修复基线 v75，像是被回退了——"
        "改前端壳清单必须递增 SW_VERSION（M18/L29 的失效双保险之一）")


def test_no_store_headers_on_real_http(home):
    """真实起 app 实测：点名模块的响应头带 Cache-Control: no-store，
    主线文件与 PWA 入口不缺席，vendor 仍走 ETag 协商不禁缓存。"""
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: FakeProvider([]),
    )
    with TestClient(app) as client:
        for name in AUDIT_MODULES:
            r = client.get(f"/static/{name}")
            assert r.status_code == 200, name
            assert "no-store" in r.headers.get("cache-control", ""), (
                f"{name} 未带 Cache-Control: no-store（启发式缓存会让旧脚本混跑）")
        # 修复前已在清单内的主线文件 / PWA 入口不得被顺手挤掉
        for path in ("/", "/static/index.html", "/static/app.js", "/static/app-tools.js",
                     "/static/app.css", "/manifest.webmanifest", "/sw.js"):
            r = client.get(path)
            assert r.status_code == 200, path
            assert "no-store" in r.headers.get("cache-control", ""), path
        # vendor 依旧可缓存（mermaid 单文件 3.3MB，靠 ETag 协商足够）
        v = client.get("/static/vendor/qrcode.min.js")
        assert v.status_code == 200
        assert "no-store" not in v.headers.get("cache-control", "")
