<div align="center">

[![SkySheep](docs/images/banner.png)](https://sky-scrape.github.io/SkySheep/)

**开源桌面 AI Agent 工作台 —— 读写文件、执行命令、操作电脑，每一步先经你确认**

[![CI](https://github.com/Sky-scrape/SkySheep/actions/workflows/ci.yml/badge.svg)](https://github.com/Sky-scrape/SkySheep/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-Windows%20%E4%BC%98%E5%85%88-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-informational)
[![English](https://img.shields.io/badge/docs-English-red)](README.en.md)
[![M8ven Verified](https://m8ven.ai/badge/mcp/sky-scrape/skysheep?variant=verified)](https://m8ven.ai/mcp/sky-scrape/skysheep)

[🌐 官网](https://sky-scrape.github.io/SkySheep/) · [⬇️ 下载安装包](https://github.com/Sky-scrape/SkySheep/releases/latest) · [📖 English Docs](README.en.md)

</div>

SkySheep 是跑在本机的开源 AI Agent 工作台：Python asyncio 引擎 + pywebview 桌面壳 + 零构建前端。
给它一个任务，它会自己读写文件、执行命令、联网检索、操作电脑，并按计划完成多步骤工作；
会话、配置与密钥全部保存在本机，写文件、跑命令等敏感操作默认先弹出确认卡片，每轮文件改动自动快照、可一键回滚。

<div align="center">

![SkySheep 主界面](docs/images/right-panel.png)

*深色「夜墨」主题：侧栏、对话区、右侧实时面板与桌面宠物*

</div>

## ✨ 特色功能

**圆桌多模型 —— 一次提问，多个模型共同作答**

成员模型并行独立作答，主席（主对话模型）将全部草稿融合为一份最终答案。

- 🎭 **身份预设**：批评者、事实核查员、简洁派、实干者等五种成员角色，权衡答案权衡锋利度与全面性
- 💬 **辩论修订**：可开启辩论轮，成员互看草稿后再修订一轮；A/B 模式保留各成员原始回答
- 📜 **草稿留存**：每份草稿随会话落库，可回看、引用、追问；雷同草稿去重、成员轻上下文等七项机制控制 Token 消耗

**任务编排 —— 多任务组成依赖流水线，后台自动执行**

以节点连成 DAG 流水线：上游并行推进，依赖到齐自动接力，直至汇总收尾。

- ⚙️ **依赖调度**：声明 `all` / `any` 依赖与条件分支，并发上限与节点超时可配置
- 🔁 **失败韧性**：失败自动重试并附带上次失败原因，会话续跑遇忙退避重排队，不卡死不丢任务
- 📥 **产出传递**：节点产出自动摘要注入下游，全文落盘备查；定时任务、子代理任务、已有会话均可纳入为节点

## 🧩 功能列表

**模型与对话**

- 内置 10 家模型服务预设（Anthropic / OpenAI / Gemini / xAI / MiniMax / DeepSeek / 智谱 / Kimi / Qwen / 小米 Mimo）+ 自定义中转；本地推理服务自动检测；模型列表与上下文窗口一键探测
- 思考强度按任务自动调档；思考过程流式展示、可折叠回看
- 上下文自动压缩（CJK 感知估算，触发比例可调，支持手动 `/compact`）
- 每日 Token 预算护栏；用量仪表盘按会话 / 服务 / 模型统计，支持费用估算
- 辅助对话可独立设定模型，不占用主会话
- 提示词库（拼音首字母过滤、AI 润色、导入导出）；`~` 提示词、`/` 命令、`@` 文件引用、`&` 对话引用；语音输入

**任务执行**

- 24 个内置工具：文件读写与精确编辑、命令执行（支持后台进程管理）、正则搜索、网页抓取与搜索（多服务商）、PDF / Word / Excel / PPT / CSV 读写、图片理解、AI 画图
- 电脑控制：截屏、鼠标、键盘、窗口、剪贴板（默认关闭，设置内按需开启）
- 子代理：六种内置角色 + 自定义角色（工具范围 / 模型 / 思考强度可配）；后台并行排队；支持 git worktree 隔离派生，独立分支运行、完成自动提交
- 执行 / 规划双模式，任务清单实时同步；任务耗时预估与实测用时记录
- 检查点按轮快照，一键撤销本轮文件改动，重启后仍可回滚

**自动化与记忆**

- 定时任务（周期执行、工具预授权）与日程提醒（周 / 月历视图、提前提醒）
- 项目记忆界面内编辑；全局自动记忆、会话归档自动提炼、定期自动整理（整理前自动备份）
- 记忆地图：项目演化时间线、主题关联图谱、LLM 演化摘要
- 钩子（Hooks）：工具调用前（可阻断）/ 后 / 任务完成三种，附测试器与最近执行面板
- `skysheep run` 无头一次性运行：预授权工具名单、JSON 输出，可脚本化集成

**扩展与连接**

- MCP 客户端：stdio / Streamable HTTP 双传输，兼容 Claude Desktop 配置，内置七个常用预设，断线自动重连
- Skills 技能包：全局 / 项目两级作用域；文件夹 / .zip / GitHub·Gitee 链接导入；扫描导入本机已有技能；二十个官方场景模板随包内置
- 远程访问：局域网令牌 + 二维码，手机可打开完整界面；支持 Tailscale 跨网络连回
- 聊天渠道：飞书（WebSocket 长连接）/ 微信（扫码登录）；默认只读，可开启审批卡片与超时拒绝
- 桌面形态：六套主题 + 跟随系统、系统托盘、开机自启、窗口几何记忆、全局热键、桌面宠物；多会话标签与右侧十页签面板（终端 / 浏览器 / 审查 / 文件 / 任务 / 日程 / 自动化 / 项目记忆 / 记忆地图 / MCP·技能）
- 会话管理：中文全文搜索、会话分支、置顶 / 归档 / 标签、导出 Markdown / HTML、自动滚动备份与可视化恢复

## 🖼 界面展示

| 模型服务设置 | 日程周历 | 用量仪表盘 |
|:---:|:---:|:---:|
| ![模型服务设置](docs/images/providers.png) | ![日程周历](docs/images/agenda.png) | ![用量仪表盘](docs/images/usage.png) |
| 内置预设填入 API Key 即可接入多家服务 | 定时任务与日程排入周历，到点自动执行 | Token 用量与费用估算按会话可查 |

## 🆚 产品对比

SkySheep 的功能集对照 Claude Code / OpenAI Codex CLI / ZCode 逐项补齐（✅ = 已实现）。CLI 工具强在终端生态与 CI 集成；SkySheep 提供完整的桌面图形界面，面向普通用户开箱即用：中文优先、圆桌多模型、任务编排、电脑控制、远程与聊天渠道。当前 Windows 优先，macOS / Linux 在路线图上（`--browser` 模式具备跨平台兜底）。

| 能力 | Claude Code | Codex CLI | ZCode | SkySheep |
|---|---|---|---|---|
| Agent 循环（流式 + 工具调用） | ✅ | ✅ | ✅ | ✅ |
| 多模型 / 多供应商（OpenAI 兼容 + Anthropic + 本地） | — | ✅ | ✅ | ✅ 内置预设 + 自定义中转 + 自动检测模型 |
| MCP 客户端（stdio/HTTP，Claude Desktop 配置兼容） | ✅ | ✅ | ✅ | ✅ 程序内导入/手填/模板 + 内置常用预设一键添加，热生效 |
| Skills 技能包（SKILL.md，渐进披露） | ✅ | ✅ | ✅ | ✅ 独立技能页：全局/项目两级、按项目限定使用范围、正文预览；文件夹/.zip/GitHub·Gitee 链接导入 + 本机现存扫描导入 |
| 子代理（后台任务 + 轮询） | ✅ | ✅ | ✅ | ✅ spawn_agent / check_task + 自定义子代理 |
| 项目记忆（AGENTS.md / CLAUDE.md） | ✅ | ✅ | ✅ | ✅ 界面内编辑 + 全局自动记忆（跨项目） |
| 记忆地图（项目演化可视化） | — | — | — | ✅ 演化时间线 + 主题图谱 + LLM 演化摘要（文件足迹 / 活跃热力图 / 任务与记忆标注） |
| 任务清单（todo） | ✅ | ✅ | ✅ | ✅ 侧栏面板实时同步 |
| 规划模式（先出计划再执行） | ✅ | — | ✅ | ✅ 执行/规划双模式 + 一键按计划执行 |
| 上下文自动压缩 + 手动 /compact | ✅ | ✅ | ✅ | ✅ CJK 感知估算 + 真实用量下限兜底 |
| 权限确认 + 项目白名单 | ✅ | ✅ | ✅ | ✅ 确认制 + 词边界命令前缀白名单（拒绝 shell 拼接绕过）+ 分级权限模式 + 工作区信任（项目自带 MCP/技能需先确认） |
| Headless 一次性运行（脚本 / CI） | ✅ -p | ✅ exec | ✅ -p | ✅ skysheep run（预授权 + JSON 输出 + 审计） |
| 忽略文件 | ✅ | ✅ | ✅ | ✅ .skysheepignore（.gitignore 语义，.env 内建忽略） |
| Slash 命令 | ✅ | ✅ | ✅ | ✅ /help /new /compact /model /status /todos /export |
| 消息排队 / 瞬态错误自动重试 | ✅ | ✅ | ✅ | ✅ 指数退避，在途内容不重放 |
| 联网抓取 + 联网搜索 | ✅ | ✅ | ✅ | ✅ web_fetch（SSRF 防护）+ web_search（博查/Tavily/智谱/自定义 SearXNG） |
| 文档阅读 + 生成 | ✅ | — | ✅ | ✅ read_document（PDF/Word/Excel/PPT）+ write_document（docx/xlsx/csv/PPT） |
| 图片输入（多模态） | ✅ | ✅ | ✅ | ✅ 粘贴/拖拽多模态 |
| 图片生成 | — | — | — | ✅ CogView/Kolors 画图 |
| 电脑控制（Computer Use） | — | — | — | ✅ screenshot/mouse/keyboard/window/clipboard（默认关，确认制 + 动作白名单，键盘与剪贴板按内容固化、关窗口不整类放行） |
| 思考过程可视化 | ✅ | — | — | ✅ ThinkingDelta 流式 + 可折叠回看 + 思考耗时 |
| 任务耗时预估 / 用时记录 | — | — | ✅ | ✅ 接手时预估区间，轮末记录实测用时（随消息落库，刷新后仍在）|
| 检查点 / 撤销本轮文件改动 | ✅ checkpoints | ✅ rollback | ✅ | ✅ 落盘持久化，重启后仍可回滚 |
| 会话搜索 / 管理 | ✅ | ✅ | ✅ | ✅ 跨项目全文搜索 + 备份可视化恢复 + 导出 Markdown/HTML |
| 右侧面板（终端/浏览器/辅助对话/审查/文件/任务/日程） | — | — | ✅ | ✅ 多会话标签并行 |
| Hooks（工具调用前后钩子） | ✅ | — | ✅ | ✅ config.toml [hooks]，pre 可阻断 |
| 亮暗主题 / 桌面形态 | ✅ 终端主题 | ✅ 语法主题 | ✅ | ✅ 六套主题（纸墨/青瓷/秋柿/夜墨/黛夜/松烟）+ 跟随系统 + 托盘 + 开机自启 + 安装包 |
| 局域网远程访问（手机继续聊） | — | — | — | ✅ 令牌 + 二维码，默认仅本机监听 |
| 跨网络远程访问（Tailscale） | — | — | — | ✅ 手机任意网络连回家，与局域网共用令牌，非 tailnet 来源直接拒绝 |
| 聊天机器人渠道（Bot Channel） | — | — | — | ✅ 飞书（App ID/Secret + WebSocket 长连接）+ 微信（扫码登录）；默认关闭，空名单拒绝一切，无人值守只读，可开审批 + 超时自动拒绝 |

> 竞品列按各家 **2026-09 官方文档**核对，仅计第一方内置功能（扩展、插件与云配套不计入）；各产品迭代较快，以官方文档为准。

## 🚀 安装与运行

**安装包（推荐普通用户）**

到 [Releases](https://github.com/Sky-scrape/SkySheep/releases/latest) 下载 `SkySheep-<版本>-setup.exe` 双击安装。首次运行如被 Windows SmartScreen 拦截，点「更多信息 → 仍要运行」（未签名程序的默认待遇，详见 [SmartScreen 放行说明](docs/smartscreen-说明.md)）。

**源码运行（开发者）**

```bash
cd SkySheep/engine
uv sync                          # 安装依赖（Python >= 3.11）
uv run skysheep app              # 桌面应用（--browser 用浏览器打开）
uv run skysheep chat             # 终端交互
uv run skysheep run "任务"       # 无头一次性运行（支持 JSON 输出）
```

**初始配置**

首次启动由配置向导三步完成接入（选服务 → 填 Key → 开始），自动检测本机本地推理服务；没有 API Key 可先进入演示模式，查看一轮真实工具调用的完整过程。

<details>
<summary><b>📦 双击启动 / 打包发布</b>（展开）</summary>

```bash
cd engine
.venv\Scripts\python.exe tools\install_shortcut.py        # 开始菜单/桌面快捷方式（源码版）
.venv\Scripts\python.exe tools\install_shortcut.py --exe  # 指向打包好的 SkySheep.exe
uv pip install pyinstaller
.venv\Scripts\pyinstaller.exe --noconfirm --clean SkySheep.spec   # 产物 dist/SkySheep/SkySheep.exe
```

单文件安装包：安装 [Inno Setup 6](https://jrsoftware.org/isdl.php) 后执行 `ISCC.exe tools\installer.iss`，产物在 `installer/`。

</details>

> **📦 多实例并存**：源码运行默认使用 dev 身份，数据在 `~/.skysheep-dev/`，与安装版的 `~/.skysheep/` 相互隔离、可同时运行；环境变量 `SKYSHEEP_INSTANCE` 可派生更多独立实例。

> **⏰ 定时任务的运行前提**：定时任务与日程提醒由应用内循环触发，仅在 SkySheep 运行期间生效。关窗时选择「缩到系统托盘」即继续后台运行；彻底退出期间到点的任务在下次启动补跑；可在 设置 · 高级 开启「开机自动启动」。

## 🔒 安全设计

安全是默认行为，无需配置：

- **权限确认制**：只读操作自动放行；写文件（附 diff 预览）、执行命令默认逐次确认。三档权限模式可调——安全执行 / 自动编辑（仅放行工作目录内写入）/ 完全访问，放宽档仅限本机界面切换
- **命令白名单**：按完整词前缀匹配，含 shell 拼接或解释器旗标的命令不命中规则、一律重新确认；规则按项目持久化，设置页可管理
- **工作区信任与联网防护**：项目自带的 MCP 与技能配置需先确认信任；网页抓取仅允许公网 http(s)，具备 SSRF 防护，响应超限即中止
- **数据本地化与可回滚**：会话、配置、密钥全部存于本机，无遥测；文件改动按轮快照、可回滚；会话库自动滚动备份；诊断包导出自动打码密钥

<details>
<summary><b>细粒度机制</b>（展开）</summary>

- 命令白名单提供整工具 / 词前缀 / 精确参数 / 通配四类规则，解释器 `-c / -e / --eval` 旗标全文扫描，防止借解释器绕过前缀规则
- 电脑控制按动作收敛：动作白名单只固化确认过的那一次，关闭窗口 / 写剪贴板不整类放行
- `web_fetch` 解析结果固定为连接目标（防 DNS rebinding），重定向逐跳复检
- 检查点每会话保留 50 轮，全项目兜底 500 条；会话备份保留 20 份，可视化恢复
- MCP 工具描述限长，防止挤占上下文或夹带注入内容；系统提示词内置提示注入防线，网页 / 文档内容按数据处理

</details>

## 🏗 系统架构

```
┌────────────────────────────────────────────┐
│ 桌面壳：pywebview 原生窗口（WebView2）        │
│  └─ 浏览器兜底（--browser）                  │
├────────────────────────────────────────────┤
│ 本地服务层：FastAPI + WebSocket             │
│  └─ JSON-RPC 请求 + AgentEvent 事件流        │
│  └─ 零构建前端（原生 HTML/CSS/JS，静态托管）   │
├────────────────────────────────────────────┤
│ SkySheep Engine (Python 3.11+, asyncio)    │
│  ├─ core/      Agent 循环、事件流、子代理、   │
│  │             压缩、检查点                  │
│  ├─ models/    Provider 层（openai/anthropic/fake）│
│  ├─ tools/     内置工具（含 web_fetch）+ Schema 导出│
│  ├─ security/  Permission Gate + 白名单      │
│  ├─ skills/    Skills 发现/开关/注入/安装      │
│  ├─ mcp/       MCP 客户端（stdio/HTTP）       │
│  ├─ session/   SQLite 持久化 + 全文搜索        │
│  ├─ config.py  ~/.skysheep/config.toml      │
│  └─ cli/       终端 REPL / 桌面启动器         │
└────────────────────────────────────────────┘
```

设计要点：**事件流驱动**——Agent 运行全程建模为 `AgentEvent`，CLI、GUI、WebSocket 消费同一套引擎 API；敏感操作由 `PermissionGate` 产出确认事件并挂起，前端决策后恢复执行。

代码可审计：全部源码（Python 引擎与前端三件套）为未混淆的可读代码；`static/vendor/` 下的 mermaid、xterm 等为第三方库的上游构建产物（版本锁定、未经改写）。

## 🤝 开发与贡献

```bash
cd engine
uv run pytest        # 全量测试（-m "not e2e" 跳过真实 socket 用例）
uv run ruff check .  # lint
```

欢迎贡献，见 [CONTRIBUTING.md](CONTRIBUTING.md)；安全漏洞请通过 [SECURITY.md](SECURITY.md) 的私密渠道报告；行为准则见 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。

遇到问题可通过应用内 设置 · 关于 →「💬 反馈问题」导出脱敏诊断包并直达反馈页。

## 🗺 路线图与非目标

- ✅ 当前版本 **v2.2.0**：引擎内核 → MCP / 技能生态 → 桌面应用 → 圆桌多模型与任务编排 → 记忆地图 → 多轮审查加固与结构重构（逐版明细见 [CHANGELOG.md](CHANGELOG.md)）
- 🚧 **后续方向**：macOS / Linux 支持 · 系统级定时调度 · 提示注入纵深防御 · 代码签名分发

**非目标**：

- **原生移动 App** —— 桌面壳是权限确认、检查点等安全交互的载体，现阶段不维护独立的移动端安全模型
- **macOS / Linux（现阶段）** —— 优先在 Windows 单平台完善桌面体验，`--browser` 模式提供跨平台兜底
- **向量记忆 / 多级上下文压缩** —— 现有压缩与归档提炼记忆已覆盖当前场景，不为此扩大安装体积
- **用 ripgrep 替换内置搜索** —— 保持纯 Python 零依赖的「安装即可用」分发形态
- **自建插件 JS API** —— 扩展点收敛在 MCP 与 Skills 两个开放标准上，前端保持零构建
- **运营型技能广场** —— 技能分发走应用内导入（文件夹 / .zip / 链接），不做集中运营的商店

## 📄 许可与赞助

本项目以 [MIT](LICENSE) 协议开源。

接受赞助，渠道见 [.github/FUNDING.yml](.github/FUNDING.yml)（GitHub Sponsors：[Sky-scrape](https://github.com/sponsors/Sky-scrape)）。赞助支出第一优先为代码签名分发，签名后的安装包可去除 Windows SmartScreen 拦截（见 [docs/smartscreen-说明.md](docs/smartscreen-说明.md)）。

---

<div align="center">

☁️ **云朵小羊与你同在** ☁️

</div>
