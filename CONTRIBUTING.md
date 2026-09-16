# 贡献指南

感谢关注 SkySheep！无论是报 bug、提功能提案、改进文档还是直接写代码，都欢迎。动手前请先读完本指南；参与讨论请遵守《贡献者行为准则》（[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)）。

## 环境搭建

Windows 是第一目标平台，以下步骤以 Windows 为准，macOS/Linux 作为兜底路径保留。

1. 安装 [uv](https://docs.astral.sh/uv/)（项目要求 Python >= 3.11，uv 会自动管理解释器）；
2. 克隆仓库并同步依赖：

```bash
git clone https://github.com/Sky-scrape/SkySheep.git
cd SkySheep/engine
uv sync
```

## 本地运行与测试

```bash
cd engine
uv run pytest          # 全量测试
uv run ruff check .    # lint
uv run skysheep app    # 桌面应用；pywebview 环境异常时加 --browser 走浏览器兜底
uv run skysheep chat   # 终端 REPL
```

没有模型 API Key 也能跑通：`engine/examples/demo*.py` 是基于 fake provider 的可运行演示。

## 代码风格

- **Python**：遵循 `engine/pyproject.toml` 中现有的 ruff 配置（行宽 110，启用 E/F/I/UP/B 规则），提交前 `uv run ruff check .` 必须零告警；
- **注释**：密度与周边代码保持一致，解释「为什么」，不复述代码在做什么；
- **UI 文案**：界面提示、按钮、错误信息等一律使用中文；
- **前端零构建**：`engine/src/skysheep/server/static/` 是原生 JS/CSS，直接改文件即可，**禁止**引入 Node/npm 工具链或任何构建步骤。

## 测试要求

- 新功能必须带测试，统一放在 `engine/tests/`，与现有用例保持同一风格；
- 修 bug 时先补一个能复现问题的用例，再修；
- **测试绝不指向真实用户数据**：用户数据只存在于 `~/.skysheep/`，测试一律使用临时目录；
- 全量测试目前有 314 个用例，CI（windows-latest）必须全绿。

## 安全红线

SkySheep 的定位是「普通用户也能安全用的桌面 Agent」，以下红线不可绕过，涉及这些面的 PR 会被重点评审：

- 任何涉及**写文件或执行命令**的新工具、新 WS 方法，必须接入 `PermissionGate`（`engine/src/skysheep/security/gate.py`）确认流程：敏感操作挂起等待用户决策后才能继续；
- `web_fetch` 的 SSRF 防护（仅允许公网地址、逐跳校验）不得放松或绕过；
- 任何代码不得在未经用户确认的情况下读写 `~/.skysheep/`（配置、密钥、会话）。

## 提交与 PR 流程

1. 从 `main` 拉出分支，一个 PR 聚焦一件事；
2. 本地跑通 `uv run pytest` 与 `uv run ruff check .`；
3. 按 `.github/PULL_REQUEST_TEMPLATE.md` 填写改动说明并逐项勾选自测清单；
4. 提交后等 CI（Windows）通过；如被要求修改，在原分支继续提交即可；
5. 用户可见的变化请附截图，UI 改动请在 PR 中给出前后对比。

## 文档要求

- 每次改动都要更新 `SkySheep/CHANGELOG.md`（Keep a Changelog 格式，中文），把条目写进 Unreleased 段；
- 若改动影响 README 中描述的特性或命令，请一并同步。

## 需要帮忙？

- bug 与功能提案请走对应 issue 模板；
- 使用疑问与想法交流请到 [Discussions](https://github.com/Sky-scrape/SkySheep/discussions)；
- 安全漏洞**不要**走公开渠道，见 [SECURITY.md](SECURITY.md)。
