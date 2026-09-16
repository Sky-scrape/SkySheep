# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 SkySheep 桌面版打成不依赖 Python 环境的独立程序。

用法（在 engine/ 目录下）：
    .venv\\Scripts\\pyinstaller.exe --noconfirm --clean SkySheep.spec
产物：dist/SkySheep/SkySheep.exe（双击即用，无终端窗口）
"""

from pathlib import Path

project_root = Path(SPECPATH)
src = project_root / "src"
static = src / "skysheep" / "server" / "static"

hiddenimports = [
    # pywebview 的 Windows 后端（运行时按名字动态选择，静态分析看不到）
    "webview.platforms.winforms",
    "webview.platforms.edgechromium",
    # pythonnet / .NET 桥
    "clr",
    "clr_loader",
    "pythonnet",
    # uvicorn 动态导入的实现
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    # 引擎中按需导入的子模块
    "skysheep.bootpages",  # 启动动画页/失败页（窗口创建前就要用）
    "skysheep.wintheme",  # 标题栏纸墨染色
    "skysheep.server.picker",  # 原生文件选择桥
    "skysheep.mcp",
    "skysheep.mcp.presets",  # 内置常用 MCP 服务预设
    "skysheep.skills",
    "skysheep.session",
    "skysheep.tools",  # tools/__init__ 显式导入各工具模块（含 browser）
    "skysheep.tools.browser",  # 浏览器控制（1.0 新增，显式声明保险）
    "skysheep.startup",  # 开机自启（设置 · 高级，函数内延迟导入）
    "skysheep.tools.computer",  # 电脑控制（截屏/鼠标/键盘/窗口/剪贴板）
    "skysheep.tools.docs",  # read_document（解析库按需导入）
    "skysheep.tools.imagegen",  # generate_image
    "skysheep.tools.memory",  # memory_write
    "skysheep.core.subagent",
    "skysheep.core.hooks",  # 工具钩子（config.toml [hooks]）
    "skysheep.core.uptodate",  # 更新检查
    "skysheep.models.probe",
]

datas = [
    (str(static), "skysheep/server/static"),
]

a = Analysis(
    [str(project_root / "desktop.py")],
    pathex=[str(src)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow：pypdf 初始化会探测 PIL；tools.computer 截屏也 import 它，随 import 自动打包
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PyQt5", "PySide6"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SkySheep",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(static / "skysheep.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SkySheep",
)
