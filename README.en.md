<div align="center">

[![SkySheep](docs/images/banner.png)](https://sky-scrape.github.io/SkySheep/)

**An open-source AI Agent workbench that runs on your computer — it can see, it can act, and it asks you before every step.**

[![CI](https://github.com/Sky-scrape/SkySheep/actions/workflows/ci.yml/badge.svg)](https://github.com/Sky-scrape/SkySheep/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-Windows--first-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-informational)
[![中文](https://img.shields.io/badge/docs-中文-red)](README.md)
[![M8ven Verified](https://m8ven.ai/badge/mcp/sky-scrape/skysheep?variant=verified)](https://m8ven.ai/mcp/sky-scrape/skysheep)

[🌐 Website](https://sky-scrape.github.io/SkySheep/) · [⬇️ Download](https://github.com/Sky-scrape/SkySheep/releases/latest) · [📖 中文文档](README.md)

</div>

SkySheep is more than a chat window: it can read and write your files, run commands, operate your computer, and work on a schedule. Every sensitive action — writing files, running commands, moving the mouse — **pops up a confirmation card asking for your consent first**; every round of file changes is automatically snapshotted, so you can undo any of it with one click, anytime.

<div align="center">

![SkySheep main window](docs/images/right-panel.png)

The full "Night-ink" dark theme: sidebar + conversation + live right panel + the desktop pet sheep

</div>

## ✨ Why SkySheep

Most AI desktop clients out there are just chat windows — the model can talk but can't act. SkySheep is a true **Agent workbench**: hand it a task and it reads the files, runs the commands, and writes out the results on its own. For a desktop Agent built for everyday users, safety comes first, so we made three things the default behavior:

| Triple safety promise | What it means |
|---|---|
| 🔐 **Sensitive actions confirmed first** | File writes show a real diff preview and every command passes your eyes one by one; read-only actions run through automatically, no interruptions |
| 📦 **All data stays on your machine** | Sessions, config, and API keys are stored only on your own computer (SQLite) — zero telemetry, zero uploads |
| ↩️ **One-click undo for mistakes** | Every round of changes is auto-snapshotted to disk; roll back to any round even after a restart |

On top of that sits a complete Agent capability stack:

- 🔌 **Multi-provider models**: Zhipu / DeepSeek / Kimi / OpenRouter / SiliconFlow / native Anthropic, one-click setup; local models via [Ollama](https://ollama.com) (no key required); custom relay endpoints + automatic model detection
- 🧩 **MCP and skill extensions**: MCP client (stdio/HTTP, Claude Desktop config compatible + one-click add for common presets); [Skill packages](skills-gallery/) (global/project scopes, install from a folder, a .zip, or a GitHub / Gitee URL + "scan this computer" to pick up skills already installed by Claude Code and other tools; a dedicated skills page with per-project scope, search, and bulk delete)
- 🖱️ **Computer control**: screenshots straight into the conversation; mouse / keyboard / window / clipboard (off by default; confirmation-gated, with an action-level whitelist)
- ⏰ **Scheduled tasks and agenda**: recurring tasks, due-time reminders, weekly calendar view; continue the chat from your phone over LAN (token + QR code)
- 👥 **Roundtable multi-model**: several models answer independently in parallel, and a chairman model merges them into one better answer — with debate rounds (members see each other's drafts and revise), member role presets (critic / fact-checker / concision / pragmatist), a token-saver trio (light member context, duplicate-draft dedupe, fusion budget), and every draft kept on the message for later review
- 🔗 **Task pipelines**: chain tasks by dependency — upstream nodes run in parallel, downstream starts automatically, a review node closes the loop; per-node timeouts, auto-retry with the last failure reason, PASS/FAIL gates, and the same unattended permission gating as scheduled tasks
- 📄 **Document I/O**: reads PDF / Word / Excel, writes Word / Excel / CSV
- 🀄 **Chinese-first**: the UI, built-in help, and prompt templates are all designed for Chinese-language scenarios

## 🖼 Interface Tour

| Model provider settings | Agenda weekly view | Usage dashboard |
|:---:|:---:|:---:|
| ![Model provider settings](docs/images/providers.png) | ![Agenda weekly view](docs/images/agenda.png) | ![Usage dashboard](docs/images/usage.png) |
| Light "paper-ink" theme — paste an API key and connect to any provider | Dark "night-ink" theme — scheduled tasks land on the weekly calendar and fire on time | Token stats and cost estimates in real time — no more mystery bills |

## 🚀 Quick Start

**Option 1: Download the installer (recommended for everyday users)**

Download `SkySheep-<version>-setup.exe` from [Releases](https://github.com/Sky-scrape/SkySheep/releases/latest) and double-click to install. If Windows SmartScreen blocks the first launch, click "More info → Run anyway" (standard treatment for unsigned programs; see the [SmartScreen walkthrough](docs/smartscreen-说明.md)).

**Option 2: Run from source (developers)**

```bash
cd SkySheep/engine
uv sync                          # Install dependencies (Python >= 3.11)

uv run skysheep app              # 🖥 Desktop app (native window; --browser for a browser)
uv run skysheep app <project-dir>  # Open a specific project directory
uv run skysheep chat             # Terminal mode
uv run skysheep run "task"       # One-shot headless run (JSON output supported)
```

No API key on first launch? The setup wizard walks you through three steps (pick a provider → paste the key → go), and auto-detects a local Ollama install; you can also click "Demo mode" first to watch a round of real tool calls. Every provider offers a free or low-cost tier — for users in China, Zhipu or DeepSeek is the recommended starting point. Key-free demo:

```bash
uv run python examples/demo.py   # FakeProvider multi-step coding task (write → crash → fix → re-test)
```

In the desktop app: type `~` to bring up the prompt-template menu (built-in examples included, manage your own under Settings · Prompts), `/` for the command menu, `@` to reference project files/folders, "Add file" to attach files from anywhere on disk, `Ctrl+F` to search within a conversation, `Ctrl+Shift+F` to search across sessions, and `Ctrl+Alt+Space` to summon the window globally. New here? Click the "?" in the top bar for built-in help.

<details>
<summary><b>📦 Double-click launch / packaging</b> (expand)</summary>

```bash
cd engine
.venv\Scripts\python.exe tools\install_shortcut.py        # Start-menu/desktop shortcuts (source build)
.venv\Scripts\python.exe tools\install_shortcut.py --exe  # Point at the packaged SkySheep.exe
uv pip install pyinstaller
.venv\Scripts\pyinstaller.exe --noconfirm --clean SkySheep.spec   # Output: dist/SkySheep/SkySheep.exe
```

Single-file installer: install [Inno Setup 6](https://jrsoftware.org/isdl.php), then run `ISCC.exe tools\installer.iss`; the output lands in `installer/`.

</details>

### 🛡 Safety mechanisms

- Read-only tools (read/grep/glob/list/web_fetch/web_search/read_document) run without confirmation
- File writes, image generation, and command execution prompt for confirmation by default: `Allow once / Always allow for this project / Deny`; the permission button in the composer cycles three modes — **Safe execution** (confirm everything) → **Auto-edit** (auto-approves writes inside the working directory only) → **Full access** (writes and commands run without prompts, shown in warning red); relaxation modes can only be switched from the local UI, never over LAN/remote
- The command whitelist matches by **command prefix**, with rules persisted per project
- `web_fetch` only allows public http(s): requests resolving to non-public IPs are rejected outright (SSRF protection), and redirects are re-checked hop by hop
- Checkpoints are persisted per project (last 50 rounds kept), with one-click "undo this round's changes" right in the conversation; changes made by run_command are not tracked
- Session data is backed up automatically on a rolling basis (20 copies kept), restorable visually under Settings · About
- The system prompt ships with a built-in **prompt-injection defense**: web/document content is treated as data, and instructions embedded in it are never executed directly
- Unattended scenarios such as scheduled tasks can only call pre-authorized tools; all other writes/executions are rejected automatically
- All user data lives in `~/.skysheep/`; exported diagnostic bundles automatically redact all secrets

> **⏰ Requirements for scheduled tasks and agenda reminders**: scheduled tasks and agenda reminders are triggered by the app's internal loop, so they **only work while SkySheep is running**: choose "Minimize to system tray" when closing the window to keep it running in the background; tasks that come due while the app has fully exited are caught up on the next launch; to have it on duty from boot, enable "Launch at startup" under Settings · Advanced.

## 🆚 Feature Panorama: Benchmarked Against Mainstream Agents

SkySheep's feature set was built item by item against Claude Code / OpenAI Codex CLI / ZCode (✅ = implemented):

<details>
<summary><b>Expand the 25-capability comparison table</b></summary>

| Capability | Claude Code | Codex CLI | ZCode | SkySheep |
|---|---|---|---|---|
| Agent loop (streaming + tool calls) | ✅ | ✅ | ✅ | ✅ |
| Multi-model / multi-provider (OpenAI-compatible + Anthropic + local) | — | ✅ | ✅ | ✅ built-in presets + custom relay endpoints + auto model detection |
| MCP client (stdio/HTTP, Claude Desktop config compatible) | ✅ | ✅ | ✅ | ✅ in-app import / manual entry / templates + one-click built-in presets, hot-applied |
| Skill packages (SKILL.md, progressive disclosure) | ✅ | — | ✅ | ✅ global/project scopes, install from folder/zip/URL |
| Sub-agents (background tasks + polling) | ✅ | ✅ | ✅ | ✅ spawn_agent / check_task + custom sub-agents |
| Project memory (AGENTS.md / CLAUDE.md) | ✅ | ✅ | ✅ | ✅ edited in-app + global auto memory (across projects) |
| Task lists (todos) | ✅ | ✅ | ✅ | ✅ sidebar panel synced in real time |
| Plan mode (plan first, then execute) | ✅ | ✅ | ✅ | ✅ dual execute/plan modes + one-click execute-the-plan |
| Automatic context compaction + manual /compact | ✅ | ✅ | ✅ | ✅ CJK-aware estimation + real-usage floor as a fallback |
| Permission prompts + project whitelist | ✅ | ✅ | ✅ | ✅ confirmation flow + command-prefix whitelist + tiered permission modes |
| Headless one-shot runs (scripts / CI) | ✅ -p | ✅ exec | ✅ -p | ✅ skysheep run (pre-authorized tools + JSON output + audit) |
| Ignore files | ✅ | ✅ | ✅ | ✅ .skysheepignore (.gitignore semantics, .env ignored by default) |
| Slash commands | ✅ | ✅ | ✅ | ✅ /help /new /compact /model /status /todos /export |
| Message queuing / auto-retry on transient errors | ✅ | ✅ | ✅ | ✅ exponential backoff; in-flight content is never replayed |
| Web fetch + web search | ✅ | ✅ | ✅ | ✅ web_fetch (SSRF protection) + web_search (Bocha/Tavily/Zhipu) |
| Document reading + generation | ✅ | — | ✅ | ✅ read_document (PDF/Word/Excel) + write_document (docx/xlsx/csv) |
| Image input / image generation | ✅ | ✅ | ✅ | ✅ paste/drag-and-drop multimodal input + CogView/Kolors image generation |
| Computer use | — | — | — | ✅ screenshot/mouse/keyboard/window/clipboard (off by default; confirmation-gated + action whitelist) |
| Thinking visualization | — | — | — | ✅ ThinkingDelta streaming + collapsible replay |
| Checkpoints / undo this round's file changes | ✅ checkpoints | ✅ rollback | ✅ | ✅ persisted to disk; rollbacks survive restarts |
| Session search / management | ✅ | ✅ | ✅ | ✅ cross-project full-text search + visual backup restore + Markdown/HTML export |
| Right-side panel (terminal / browser / side chat / review / files / tasks / agenda) | — | — | ✅ | ✅ multiple session tabs in parallel |
| Hooks (pre/post tool-call hooks) | ✅ | — | ✅ | ✅ [hooks] in config.toml; pre-hooks can block |
| Light/dark themes / desktop form factor | — | — | ✅ | ✅ Paper-ink / Night-ink / follow system + system tray + launch at startup + installer |
| LAN remote access (continue on your phone) | — | — | — | ✅ token + QR code; listens on localhost only by default |

</details>

Where they differ: the big three CLIs are stronger in terminal ecosystem (plugin markets, CI integration); SkySheep puts "safe for everyday desktop users" first — confirmation-gated permissions, purely local storage, no crash without an API key, one-click checkpoint rollback, and computer control tucked away by default. The current release is **Windows-first** (pywebview/WebView2); macOS/Linux are on the roadmap (the `--browser` mode already provides a cross-platform fallback).

## 🏗 Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ Desktop shell: pywebview native window (WebView2)                │
│  └─ Browser fallback (--browser)                                 │
├──────────────────────────────────────────────────────────────────┤
│ Local server layer: FastAPI + WebSocket                          │
│  └─ JSON-RPC requests + AgentEvent event stream                  │
│  └─ Zero-build frontend (vanilla HTML/CSS/JS, served statically) │
├──────────────────────────────────────────────────────────────────┤
│ SkySheep Engine (Python 3.11+, asyncio)                          │
│  ├─ core/      Agent loop, event stream, sub-agents,             │
│  │             compaction, checkpoints                           │
│  ├─ models/    Provider layer (openai/anthropic/fake)            │
│  ├─ tools/     Built-in tools (incl. web_fetch) + schema export  │
│  ├─ security/  Permission Gate + whitelist                       │
│  ├─ skills/    Skill discovery / toggle / injection / install    │
│  ├─ mcp/       MCP client (stdio/HTTP)                           │
│  ├─ session/   SQLite persistence + full-text search             │
│  ├─ config/    ~/.skysheep/config.toml                           │
│  └─ cli/       Terminal REPL / desktop launcher                  │
└──────────────────────────────────────────────────────────────────┘
```

Design keynote: **event-stream driven** — the entire agent run is modeled as `AgentEvent`s, and the CLI, GUI, and WebSocket server all consume the same engine API; sensitive operations go through the `PermissionGate`, which emits a `PermissionRequest` event and suspends; once the frontend decides, execution resumes.

Auditability: all project source (the Python engine and the frontend trio) ships as readable, unobfuscated code; the mermaid/xterm bundles under `server/static/vendor/` are upstream minified build artifacts of third-party libraries (versions pinned, never modified) — published assets, not project source. Built-in tools export standard MCP annotations (readOnlyHint / destructiveHint / idempotentHint / openWorldHint) for MCP hosts.

## 🤝 Development

```bash
cd engine
uv run pytest        # Full test suite (incl. real MCP stdio integration + end-to-end WebSocket protocol tests)
uv run ruff check .  # lint
```

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md) (environment setup, coding conventions, PR self-check checklist). To report a security vulnerability, use the private channel in [SECURITY.md](SECURITY.md) — please don't open a public issue. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for the code of conduct.

Running into a problem? In the app, go to Settings · About → "💬 Report an issue" to auto-package a redacted diagnostic bundle and open the feedback page.

## 🗺 Roadmap

- **M1-M4 (✅)**: engine core → extension ecosystem (MCP/Skills/sub-agents) → desktop app → feature-parity pass
- **M5 (✅ shipped)**: open-source release — repo live at [github.com/Sky-scrape/SkySheep](https://github.com/Sky-scrape/SkySheep):
  CI ✅, community docs ✅, installer scripts ✅, update check ✅,
  SmartScreen guide ✅, website page (`index.html`, live on GitHub Pages) ✅; remaining: code-signed distribution
- **M6 (✅ 0.8.0)**: usability hardening for everyday users (see the [CHANGELOG](CHANGELOG.md) for details)
- **v1.0 (✅)**: first stable release — browser control, phone control (in-app toggle),
  instant title-bar theme sync, keyless activation paths (registration guide / demo mode / one-click Ollama),
  daily token budget guardrails, prompt-injection defense, feedback loop (diagnostic bundle + one-click report),
  crash sentinel with friendly messaging, and the full website + open-source kit
- **v1.2 (✅)**: real terminal (multi-tab PowerShell via ConPTY + xterm.js), bot
  channels (Telegram / WeChat QR login), task time estimates, voice input, session archive & tags,
  cross-session references, live sub-agent streaming with true cancellation, round-table multi-turn
  debate, three-layer hardening from an external security review, safe non-UTF-8 text I/O (GBK etc.),
  PPTX read/write, and provider presets expanded to 10 vendors
- **v1.3 (✅)**: task pipelines (chain tasks by dependency — parallel dev work feeds an automatic review stage), archive-time memory digest (distills long-term user memory when a session is archived), roundtable token saver trio (light member context / duplicate-draft dedupe / member role presets) plus robustness fixes, installer static-asset hardening
- **v1.4 (✅)**: "quick commands" renamed to "prompts" with a `~` trigger (split from `/` slash commands), three-mode permission button (safe execution / auto-edit / full access), built-in example prompts seeded as editable records on first boot, Skill Market grown to 20 official Chinese skills, prompt-dialog fix
- **v1.5 (✅)**: root fix for the launcher "double-click does nothing" hang chain (non-blocking focus / stale-instance takeover / window-creation watchdog) plus a persistent WebView2 profile, drag-to-reorder projects & sessions (shared by grouped and classic views, fork families stay together), thinking & elapsed time persisted with messages (survives refresh; surfaced in exports and Telegram / WeChat channels), cua computer-driver preset, and two performance passes (Anthropic cache breakpoints / SQLite WAL / runtime-pool LRU / on-demand history images / merged terminal output / lazy mermaid·xterm)
- **v1.6 (✅)**: multi-instance support (source tree runs a `dev` identity isolated from the installed app), structured logging (per-session turn / tool / permission timings), bounded auto-reconnect for MCP (no replay of the failed call), shell-chaining detection per actual shell (plugs the cmd.exe single-quote & `%VAR%` bypasses), full MCP annotations on all 31 built-in tools (listed in the M8ven Trust Index), merged streaming deltas, chunked long-history rendering, and a real-socket end-to-end test layer
- **v1.7 (✅)**: projects may be deleted down to zero — the no-project state (quick chat) is now a first-class, reboot-persistent mode (current project recorded backend-side in ui.json), a permanent quick-chat section in the classic view, schedule time spans, a per-project task list, six built-in sub-agents all editable, one-click in-app update for the installed build, one-click context-window detection for model services, default context limit raised to 1M tokens, plus a batch of fixes (quick-chat session ops, channel first-config deadlock, WeChat QR rendering)
- **v1.8 (✅)**: isolated sub-agent spawns on a dedicated git worktree & branch (writes never touch the main workspace; engine auto-commits, merging stays under the main session's control) and write leases for parallel tasks (same-project sessions coordinate file writes, conflicts are annotated); the chat channel switches from Telegram to Feishu (official SDK, remote chats pinned to a dedicated project); eight Settings → Advanced additions (hook tester / recent-run panel / enable toggles for hooks & whitelist rules / whitelist hit stats / stop hooks / compaction trigger ratio / restore-default run params / trusted-projects list); the second security-review pass fixed 2 high + 16 medium findings (skill-name path traversal, post-compaction persistence slicing, and more); plus a UI polish batch (prompt page sort/stats/import-export, renamable & draggable session tabs)
- **v1.9 (✅ current release)**: five audit batches (session operations, memory & information management, skill management, MCP management/usage, and the right panel's 12 tabs) + two root-cause fixes for WeChat QR login + desktop start/exit lifecycle cleanup (no more false "last exit was abnormal" reports); plus a UX consistency batch: every "new session" click creates one, archiving closes its tab, a fixed-size archive dialog with bulk select, and the skills page now fills the viewport (Skill Market fully retired — install skills via "Import")
- **Next up**: macOS/Linux support, system-level scheduling, defense-in-depth against prompt injection

[CHANGELOG.md](CHANGELOG.md) is the single source of truth for versions and changes.

## 📄 License

[MIT](LICENSE)

---

<div align="center">

☁️ **The cloud sheep is with you** ☁️

</div>
