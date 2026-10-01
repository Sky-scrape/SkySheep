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

# static/ 整目录拷贝会把开发期工具残留（.mimosa 之类点开头目录）一起带进安装包，
# 且该目录被 StaticFiles 挂载、未认证可读——逐文件收集并跳过点开头路径，从源头挡住
static_datas = [
    (
        str(p),
        str((Path("skysheep") / "server" / "static" / p.relative_to(static).parent)),
    )
    for p in sorted(static.rglob("*"))
    if p.is_file()
    and not any(part.startswith(".") for part in p.relative_to(static).parts)
]

datas = static_datas
# 场景模板清单（skysheep.skills.gallery 运行时按模块同目录读取）：
# 不在 static/ 收集范围内，单独点名带进包，落位保持包内相对路径不变
gallery_manifest = src / "skysheep" / "skills" / "gallery_manifest.json"
datas = datas + [(str(gallery_manifest), str(Path("skysheep") / "skills"))]

# 场景模板技能本体（README「随包内置」的实物）：从仓库根 skills-gallery/ 逐文件
# 收进包内 skysheep/skills/gallery/<dir>/，gallery 安装官方来源时优先本地拷贝
# （skills.gallery.bundled_dir_for），GitHub 链接只作包内缺失时的更新回退。
# datas 元组的第二项是「目标目录」而非文件全路径（static_datas 同款 .parent
# 取法）：带文件名会把同名目录与文件嵌套成 SKILL.md/SKILL.md。与 static_datas
# 同一防线跳过点开头路径；仓库根没有该目录（罕见）时不挡构建
gallery_root = project_root.parent / "skills-gallery"
if gallery_root.is_dir():
    datas = datas + [
        (
            str(p),
            str(
                Path("skysheep")
                / "skills"
                / "gallery"
                / p.relative_to(gallery_root).parent
            ),
        )
        for p in sorted(gallery_root.rglob("*"))
        if p.is_file()
        and not any(part.startswith(".") for part in p.relative_to(gallery_root).parts)
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
