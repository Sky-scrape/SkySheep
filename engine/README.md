# skysheep-engine

SkySheep 的 Python 引擎内核：Agent 循环、多协议模型接入、内置工具、权限门控、会话持久化。

## 模块地图

| 模块 | 职责 |
|---|---|
| `skysheep.messages` | 跨 Provider 归一化的消息/内容块模型 |
| `skysheep.events` | Agent 运行过程的统一事件流 |
| `skysheep.core` | Agent 核心循环（流式、工具调用、权限交互协议），附任务耗时预估（`estimate.py`）与思考强度自动估档（`effort.py`）；检查点带字节限额，取消轮次会修复断裂的 tool_use 历史 |
| `skysheep.models` | 模型适配层：OpenAI 兼容 / Anthropic 原生 |
| `skysheep.tools` | 内置工具（文件读写/移动删除、搜索、命令、文档、图片、联网、电脑控制）+ Schema 导出；grep 按探测编码匹配（GB18030 也能搜到），`run_command` 子进程剥密钥类环境变量，画图与联网同一套 SSRF 防护（公网校验 + 连接固定 + 流式限长）|
| `skysheep.security` | Permission Gate：工具分级、白名单、确认协议（决策值过白名单，认不出来按拒绝）。命令拼接检测按实际 shell 取（Windows 的 `cmd.exe` 单引号不是引号、`%VAR%` 会展开）；工作区信任 `refresh(touched=...)` 只延续用户本次操作涉及的来源 |
| `skysheep.session` | SQLite 持久化：项目 / 会话 / 消息 / 白名单规则 / 演化摘要（`map_digests`，记忆地图用），含 `messages_fts` 全文索引（索引写失败会留痕并在下次启动查漏补齐）|
| `skysheep.obs` | 结构化日志：既有文本行格式不变，尾部追加 JSON，供按会话检索轮次/工具/权限耗时 |
| `skysheep.textio` | 文本文件的编码（UTF-8 / GB18030 / BOM）与行尾符探测与安全写回；`write_text_atomic` / `write_bytes_atomic` 供引擎自有状态文件（config.toml、任务簿、mcp.json、ui.json、检查点）原子落盘 |
| `skysheep.channels` | 聊天机器人渠道：飞书 / 微信遥控端（默认关闭，允许名单为空即拒绝一切；无人值守时写与执行自动拒绝，预授权写/执行类工具会显式告警）|
| `skysheep.mcp` | MCP 客户端（stdio / Streamable HTTP，支持自定义鉴权请求头）。工具不固持会话，断线后有界自动重连（不重放失败的调用）；keeper 内握手与工具列表各带超时，导入 stdio 定义需显式确认 |
| `skysheep.skills` | SKILL.md 发现 / 开关 / 注入 / 安装 |
| `skysheep.config` | `~/.skysheep/config.toml` 配置与 Provider 预设 |
| `skysheep.cli` | 终端 REPL + `skysheep app` 桌面启动；渲染前剥终端控制序列 |
| `skysheep.server` | 桌面端服务层：FastAPI + WebSocket 协议 + 静态前端（Host 守卫防 DNS rebinding，安全响应头防点击劫持）|
| `skysheep.bgtasks` | `spawn_bg`：后台任务的强引用登记（asyncio 只持弱引用，不登记的任务可能被 GC 掉）|
| `skysheep.windowstate` | 窗口几何记忆：退出时保存大小/位置/最大化状态，启动恢复（显示器配置变化自动回退默认居中） |
| `desktop.py` | 无终端启动器（双击入口）：单实例、失败弹框、日志兜底 |

## 快速开始

```bash
uv sync
uv run skysheep config init   # 生成 ~/.skysheep/config.toml，填入 API Key
uv run skysheep chat          # 在当前目录启动
uv run skysheep app           # 桌面窗口（--browser 改用系统浏览器）
uv run pytest                 # 测试
```

## 桌面启动与打包

双击启动走 `SkySheep.pyw`（由 `.venv\Scripts\pythonw.exe` 运行，不出控制台窗口），
实际逻辑在 `desktop.py`：

- **单实例**：命名互斥体，重复双击只把已有窗口唤到前台，不会开出第二个服务端口。
  窗口缩在托盘时再双击，也能把隐藏的窗口唤回来。
- **退出选择**：点窗口 × 会弹出三选——**是** 彻底退出、**否** 缩到系统托盘
  （托盘小羊常驻：双击/左键恢复窗口，右键菜单可打开或彻底退出）、**取消** 留在当前窗口。
- **窗口定位**：启动时按主屏逻辑尺寸夹住窗口大小（不许比屏幕大）并显式传 x/y 居中、
  纵向略偏上；不指定位置时 WinForms 的 CenterScreen 在 DPI 缩放下会把窗口推向右下，
  窗口比屏幕大时则四周溢出被裁。
- **失败可见**：无控制台时异常无处可看，所以启动失败先在窗口里画出失败页（原因+日志路径），
  关窗后再弹 Windows 消息框，完整堆栈写进 `~/.skysheep/logs/desktop.log`。
- **标准流兜底**：windowed 模式下 `sys.stdout` / `sys.stderr` 可能是 `None`，
  uvicorn 与 rich 一调 `isatty()` 就崩。这里把它们指向日志文件——必须用**真实文件对象**，
  换成自定义包装器会让 pywebview 窗口静默创建失败（踩过这个坑）。
- **启动动画**：主窗口第一页就是动画页（`skysheep/bootpages.py`，纯标准库构建），
  引擎在动画可见之后才导入（`server/__init__` 懒加载是前提），就绪后 `load_url` 原地切换。

### 双击会弹出黑框终端？跑一次修复工具

**`uv` 创建的 venv 里 `Scripts\pythonw.exe` 不是真正的无终端程序**：它与同目录的
`python.exe` 是同一个文件（SHA256 相同），PE 子系统是 CONSOLE，而且硬编码拉起基础
解释器的 `python.exe`。用它双击启动，Windows 必然分配一个控制台窗口——这就是"打开
还是先跳出终端"的原因。

修法是换成 CPython 自带的 GUI 版 venv 启动器（`<base>\Lib\venv\scripts\nt\pythonw.exe`，
PE 子系统为 GUI，会以基础解释器的 `pythonw.exe` 运行）：

```bash
.venv\Scripts\python.exe tools\fix_venv_pythonw.py           # 修复 + 启动自检
.venv\Scripts\python.exe tools\fix_venv_pythonw.py --check   # 只看现状，不改文件
```

工具会实测子进程是否拿到**可见的**控制台窗口来判定成败（不是只查文件大小）。
**删掉 `.venv` 重新 `uv sync` 之后要再跑一次**；`tools\install_shortcut.py` 建快捷方式时
也会自动调用它。

在桌面创建快捷方式（用 `SHGetFolderPathW` 取真实桌面，桌面可能被重定向到别的盘）：

```bash
.venv\Scripts\python.exe tools\install_shortcut.py           # 指向源码版
.venv\Scripts\python.exe tools\install_shortcut.py --exe     # 指向打包版
```

打包成不依赖 Python 环境的独立程序：

```bash
uv pip install pyinstaller
.venv\Scripts\pyinstaller.exe --noconfirm --clean SkySheep.spec
```

产物 `dist/SkySheep/SkySheep.exe`（约 62 MB，onedir）。两个要点：入口用 `desktop.py`；
前端资源靠 `sys._MEIPASS` 定位（`server/app.py` 的 `_static_dir`）。

安装包用 `tools/installer.iss`（Inno Setup 6+，`ISCC.exe tools\installer.iss`）。静默部署要点：
安装器默认要求管理员权限，无人值守场景加 `/CURRENTUSER`（落到用户目录），
例如 `SkySheep-1.9-setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /CURRENTUSER /DIR="D:\SkySheep"`；
卸载（含静默）在应用运行中会被拒绝（退出码非零），请先退出应用。
