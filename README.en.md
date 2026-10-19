<div align="center">

[![SkySheep](docs/images/banner.png)](https://sheepsky.com/)

**An open-source desktop AI Agent workbench — roundtable, team, and adversarial collaboration plus a separate relay workbench for files, commands, and your computer.**

[![CI](https://github.com/Sky-scrape/SkySheep/actions/workflows/ci.yml/badge.svg)](https://github.com/Sky-scrape/SkySheep/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-Windows--first-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.11%2B-informational)
[![中文](https://img.shields.io/badge/docs-中文-red)](README.md)
[![M8ven Verified](https://m8ven.ai/badge/mcp/sky-scrape/skysheep?variant=verified)](https://m8ven.ai/mcp/sky-scrape/skysheep)

[🌐 Website](https://sheepsky.com/) · [⬇️ Download](https://github.com/Sky-scrape/SkySheep/releases/latest) · [📖 中文文档](README.md)

</div>

SkySheep is an open-source AI Agent workbench that runs entirely on your machine: a Python asyncio engine, a pywebview desktop shell, and a zero-build frontend. Hand it a task and it reads and writes files, runs commands, searches the web, operates your computer, and carries multi-step work through on its own. For the work that matters, you can put several models on it — roundtable answers in parallel and fuses the drafts, team mode splits the job by roles, and adversarial review hunts for what's wrong. These three composer modes are joined by a separate right-side relay workbench that alternates work between the main and peer sessions. Sensitive actions such as file writes and command execution still pop up a confirmation card by default, every round of file changes is automatically snapshotted for one-click rollback, and sessions, configuration, and API keys never leave your machine.

<div align="center">

![SkySheep main window](docs/images/right-panel.png)

*The full "Night-ink" dark theme: sidebar, conversation area, live right panel, and the desktop pet*

</div>

## 🎯 What it can do for you

Instead of a feature checklist, here are the jobs it actually takes on:

**Tame files and documents** — "Sort half a year of downloads into folders by type", "pull key figures from dozens of PDFs into one Excel sheet". File read/write with precise editing and PDF / Word / Excel / PPT / CSV parsing are built-in tools — describe the job instead of writing a script first; moves and deletes go through reversible dedicated operations, so a mistake can be undone.

**Work inside an existing project** — read the code, locate the bug, edit files, run the tests; undo the whole round with one click if the result isn't right. Fits config changes, batch refactors, and small tools with clear acceptance criteria.

**Turn repetitive chores into scheduled automation** — "Every morning at 8, fetch new content from these sites, summarize it, and push it to our Feishu group", "compile the weekly report every Friday afternoon". Scheduled tasks and dependency pipelines handle the firing and the output passing; export a task to the Windows Task Scheduler and it fires even when the app is closed.

**Get several models in one room** — for decisions you'd rather not trust to a single model: roundtable has several models answer independently and the chairman fuses the drafts; team mode splits the work by roles on a task board; adversarial review runs a dedicated fault-finding pipeline.

**Operate the computer for you** — screenshot, mouse, keyboard, windows, clipboard (off by default; enable in settings). Built for repetitive UI chores like clicking "export" across a row of apps.

**Stay in control while away from the desk** — open the full UI from your phone's browser with confirmation cards intact; dispatch work and receive daily digests and completion notices over Feishu / WeChat.

## 👥 Three ways to put several models on one task

Multi-model here doesn't mean opening three windows and asking the same question. It means models take different parts of one task. Roundtable, team, and adversarial modes are selected in the composer, with only one active per turn. Relay is a separate right-side workbench that alternates full Agent turns between the main and peer sessions.

**Roundtable — one question, several models answering.** Members answer independently in parallel; the chairman (your main conversation model) fuses every draft into a single final answer, so sharp criticism and safe conclusions both survive. Members can wear identity presets (critic, fact-checker, minimalist, pragmatist, and more), an optional debate round lets them read each other's drafts and revise once, and every draft is stored with the session for later review, citation, and follow-up. Seven mechanisms — deduplicating near-identical drafts, light context for members, and more — keep the token bill down.

**Team — work split by role, tracked on a task board.** One big task becomes a set of tickets; each teammate has its own role, model, and tool scope, reports in the team channel, and gets accepted or sent back for rework. There are two kinds of director: you (every message you send is an instruction) or an AI director (pick a model as director and it drives decompose → dispatch → collect → accept → deliver on its own, while you keep the right to interrupt, take over, and call it off). When a few members need to hash something out, a "small meeting" runs a roundtable inside the channel without derailing the main line. Runaway guards include a global round cap, forced adjudication when rework loops exceed the limit, hand-off to the director when a member stalls, and forced delivery when the token budget is crossed.

**Adversarial — a review pipeline built to find fault.** It doesn't aim for a pretty answer; it aims to dig problems out. Four roles relay in sequence — discover, investigate, recommend, judge — each seeing only its own slice. Finding a problem pays better than nodding along (asymmetric incentives), stage contexts are isolated from each other, and if one stage fails the pipeline degrades to a usable conclusion instead of collapsing. Fits plan reviews, code reviews, and contract risk — cases where you'd rather offend than agree.

Two boundaries worth stating: roundtable and team go through their own permission gates, so sensitive actions still ask for confirmation; adversarial review is pure text reasoning and does not pass the permission gate. Multi-model really is more expensive than a single model — the controls already exist: the roundtable's seven token mechanisms, the team's global round cap and forced delivery on budget overrun, plus per-member model choice (cheap models as members, a strong model as chairman or director).

### Separate workbench: Relay

Relay is for research or plans that benefit from alternating between two screens. The main screen is the current session; the peer screen is an independent session opened for the relay. The sessions appear side by side in the same window and alternate their configured tasks. It is not a fourth composer mode: both sides are full Agent sessions, so tools, permission confirmations, and checkpoints still apply.

## 🔒 Why you can trust it with real work

An agent that edits files and runs commands must be survivable when things go wrong. Safety is the default behavior — no setup required:

- **Confirm every step**: read-only actions run through automatically; file writes (with a diff preview) and command execution prompt for confirmation one by one. Three permission modes — Safe execution / Auto-edit (auto-approves writes inside the working directory only) / Full access; relaxed modes can only be switched from the local UI
- **Command whitelist**: matches by full word prefix; commands carrying shell chaining or interpreter flags never match a rule and always re-confirm; rules persist per project, managed in settings
- **Everything is rollback-able**: file changes are snapshotted per round — undo this round's changes with one click, even after a restart; checkpoints keep 50 rounds per session, 500 per project
- **Data stays local**: sessions, config, and keys never leave your machine, zero telemetry; web fetch is restricted to public http(s) with SSRF protection; external web / document content is treated as data, never as instructions
- **Recovery is built in**: the session database keeps rolling backups (20 copies) with visual restore; the run center shows the state of every turn and background subagent, and failed or restart-interrupted runs can be re-sent / re-run with one click (only genuinely unfinished runs qualify — completed ones are never replayed, so write tasks can't double-fire); a project's bundled MCP / skill configs require explicit trust first; exported diagnostic bundles redact keys automatically

<details>
<summary><b>Fine-grained mechanisms</b> (expand)</summary>

- Four whitelist rule kinds (whole tool / word prefix / exact arguments / glob); interpreter `-c / -e / --eval` flags are full-text scanned so they can't smuggle a command past a prefix rule; legacy rules that no longer match after the 2.3.0 tightening are badged "stale" in the settings page (hover for the reason) for easy per-rule cleanup
- Computer control converges per action: the action whitelist only remembers the exact confirmed action — closing windows / writing the clipboard is never allowed as a whole tool
- `web_fetch` pins resolved IPs as the connection target (anti-DNS-rebinding) and re-checks redirects hop by hop
- Checkpoints: 50 rounds per session, 500 per project; session backups keep 20 copies, restorable visually
- Remote access is scoped per connection: a device holding the token only sees its own current session's runs, tasks, permissions, and status; rotating the token or turning remote access off actively disconnects established remote connections. This is not multi-account isolation — devices sharing one token can switch projects and sessions freely, so hand the token only to devices you trust
- In-app updates require the publisher to ship a `.sha256` checksum; when it is missing the update refuses to auto-install and points you to a manual download. The checksum and the installer come from the same release, so it guards against transfer corruption and one-sided tampering but does **not** prove publisher identity; a failed update is detectable, backed up, and manually recoverable — it does not auto-rollback
- MCP tool descriptions are length-capped against context crowding and injected instructions; the system prompt ships with a prompt-injection defense — web / document content is treated as data

</details>

## 🧭 How it differs from terminal agents

The difference from terminal agents like Claude Code or OpenAI Codex CLI is not a feature-by-feature checklist but the product form — terminal tools excel at scripting and CI integration; SkySheep excels at being **visible, controllable, and reachable**:

- **Multi-model as a native experience**: roundtable / team / adversarial modes are selected in the composer; Relay opens in the right-side panel from the sidebar (see [the section above](#-three-ways-to-put-several-models-on-one-task))
- **Visible**: a full graphical interface, Chinese-first — conversation, confirmation cards, diff previews, file changes, terminal output, and review diagnostics all live in the UI instead of a scrolling terminal log
- **Controllable**: permission gates, checkpoint rollback, workspace trust, and the command whitelist are all graphical — which file changed, who started it, and whether it can be undone, visible at a glance
- **Reachable**: work continues while you're away — scheduled tasks and dependency pipelines run on their own, pushing daily digests and completion notices to Feishu / WeChat / a Webhook; the full UI opens in your phone's browser with remote approval cards

Conversely, if what you want is scripting inside CI or a pure keyboard-driven workflow, terminal agents remain the better fit; SkySheep ships `skysheep run` headless mode and a terminal REPL as complements.

SkySheep is a **Windows desktop product**: the installer, desktop shell, DPAPI credential encryption, Windows sandbox, window and process management, and `schtasks` export are all built and polished for Windows only. The `--browser` mode can bring up the engine and web UI on macOS / Linux, but that is a developer fallback, not a supported product form. Cross-platform support will be reconsidered only after the Windows desktop experience reaches a stable bar (see [Roadmap & Non-goals](#-roadmap--non-goals)).

## 🚀 Installation & Running

**Installer (recommended for everyday users)**

Download `SkySheep-<version>-setup.exe` from [Releases](https://github.com/Sky-scrape/SkySheep/releases/latest) and double-click to install (Windows 10 / 11 x64). If Windows SmartScreen blocks the first launch, click "More info → Run anyway" (standard treatment for unsigned programs; see the [SmartScreen walkthrough](docs/smartscreen-说明.md)).

**Run from source (developers)**

Source runs are supported on Windows; on macOS / Linux the engine and web UI (`--browser`) work, but the desktop shell, credential encryption, and OS-integration features carry no support commitment.

```bash
cd SkySheep/engine
uv sync                          # Install dependencies (Python >= 3.11)
uv run skysheep app              # Desktop app (--browser opens in a browser)
uv run skysheep chat             # Terminal session
uv run skysheep run "task"       # One-shot headless run (JSON output supported)
```

**Initial setup**

A first-launch wizard connects a provider in three steps (pick a service → paste the key → go) and auto-detects local inference services; without an API key you can enter demo mode and watch a full round of real tool calls.

<details>
<summary><b>📦 Double-click launch / packaging</b> (expand)</summary>

```bash
cd engine
.venv\Scripts\python.exe tools\install_shortcut.py        # Start-menu/desktop shortcuts (source build)
.venv\Scripts\python.exe tools\install_shortcut.py --exe  # Point at the packaged SkySheep.exe
.venv\Scripts\pyinstaller.exe --noconfirm --clean SkySheep.spec   # Output: dist/SkySheep/SkySheep.exe (PyInstaller ships with uv sync)
```

Single-file installer: install [Inno Setup 7](https://jrsoftware.org/isdl.php) (6.x does not ship the Simplified Chinese message file with the compiler, which fails the installer build), then run `ISCC.exe tools\installer.iss`; the output lands in `installer/`. Or run `python tools/release.py` for a one-command package → verify → smoke-test → installer chain (build-only by default; `--upload` performs the release upload).

</details>

> **📦 Multiple instances**: source builds run under a `dev` identity by default, with data in `~/.skysheep-dev/` — isolated from the installed app's `~/.skysheep/` and safe to run side by side; the `SKYSHEEP_INSTANCE` environment variable spawns further independent instances.

> **⏰ Scheduled tasks require the app to be running**: scheduled tasks and agenda reminders fire from the app's internal loop. Choose "Minimize to system tray" when closing to keep them running in the background; tasks that come due while fully exited are caught up on the next launch. Enable "Launch at startup" under Settings → Advanced for always-on duty. Need a task to fire even when the app is fully closed? Export it as a Windows scheduled task from the task card ("⊞" button; registered via `schtasks` as "SkySheep-<task id>" and run standalone as `skysheep cron run <task id>`).

<details>
<summary><b>📋 Feature overview</b> (full capability list, expand)</summary>

**Roundtable multi-model — one question, several models answering together**

Member models answer independently in parallel; the chairman (the main conversation model) fuses all drafts into one final answer.

- 🎭 **Role presets**: five member roles — critic, fact-checker, concision, pragmatist, and more — balancing sharpness and coverage
- 💬 **Debate rounds**: members see each other's drafts and revise before the fusion; A/B mode keeps every member's original answer
- 📜 **Draft retention**: every draft is persisted with the session for later review, quoting, and follow-ups; seven token-control mechanisms (duplicate-draft dedupe, light member context, per-draft and total budget caps, and more) keep the bill in check

**Task pipelines — chain tasks by dependency and let them run in the background**

Nodes form a DAG pipeline: upstream nodes run in parallel, downstream starts automatically as dependencies land, through to the closing summary.

- ⚙️ **Dependency scheduling**: declare `all` / `any` dependencies and conditional branches; concurrency limits and per-node timeouts are configurable
- 🔁 **Failure resilience**: auto-retry with the last failure reason attached; resumed sessions re-queue with backoff — nothing hangs, nothing is lost
- 📥 **Output passing**: node output is summarized and injected downstream, with full text written to disk for reference; scheduled tasks, sub-agent tasks, and existing sessions can all join as nodes

**Models & conversation**

- Built-in presets for ten cloud services (Anthropic / OpenAI / Gemini / xAI / MiniMax / DeepSeek / Zhipu / Kimi / Qwen / Xiaomi MiMo) plus custom relay endpoints (including local inference services such as LM Studio); one-click model listing and context-window detection, plus a capability probe (streaming / tools / vision / reasoning) and a masked diagnostic report
- Reasoning effort auto-tuned per task; thinking process streamed live and replayable in a collapsible view; a per-session turn-diagnostics panel in the review page breaks down each round's time (tool counts & slowest tool / permission waits / tokens)
- Automatic context compaction (CJK-aware estimation, adjustable trigger ratio; manual `/compact` from the desktop input box — the CLI has no such command)
- Daily token budget guardrails; a channel alert is pushed when usage crosses the 80% / 100% tiers (at most one per tier per day; alerts follow the channels — with no chat / Webhook channel enabled there is no alert, and the in-app guardrail still applies); usage dashboard by session / provider / model with cost estimation
- Auxiliary chat can run its own model without touching the main conversation
- Prompt library (pinyin-initial filtering, AI polish, import/export); `~` prompts, `/` commands, `@` file references, `&` conversation references; voice input

**Task execution**

- 28 built-in tools: file read/write with precise editing, command execution (with background process management), regex search, web fetch and search (multiple providers), PDF / Word / Excel / PPT / CSV I/O, image understanding, AI image generation; web fetch ships injection defense — external content is wrapped in an explicit boundary frame declaring that instructions inside it are not yours, and a suspected-injection hint is appended when known injection patterns are detected
- Computer and browser control: screenshot, mouse, keyboard, window, clipboard, browser opening / in-page automation (off by default; enable in settings)
- Sub-agents: six built-in roles + custom roles (tool scope / model / reasoning effort configurable); parallel background execution with queuing; isolated spawns on a dedicated git worktree that auto-commits to its own branch
- Execute / plan dual mode with a live-synced task list; task duration estimates and actual-time logging
- Per-round checkpoints — undo this round's file changes with one click; rollbacks survive restarts

**Automation & memory**

- Scheduled tasks (recurring, with pre-authorized tool lists) and agenda reminders (weekly / monthly calendar, advance reminders); task and pipeline final states plus a daily run digest can be pushed to Feishu / WeChat / a generic Webhook, tasks can be exported as Windows scheduled tasks (they fire even when the app is closed), and the automation panel's "Today" card summarizes the day's runs
- Project memory edited in-app; global auto memory; archive-time memory distillation; scheduled memory tidy-up (originals backed up first); oversized global memory is injected by per-turn relevance (embedding-similarity ranking via a local Ollama when installed, rule scoring otherwise), and noteworthy facts from each round land as pending-review candidates that only reach memory after you adopt them
- Memory map: project evolution timeline, topic association graph, LLM-generated evolution summary
- Hooks: pre tool-call (can block) / post / task-finished, with a tester and a recent-runs panel
- `skysheep run` headless one-shot execution: pre-authorized tool list, JSON output, script-friendly

**Extensions & connectivity**

- MCP client: stdio / Streamable HTTP, Claude Desktop config compatible, seven built-in presets, bounded auto-reconnect
- Skill packages: global / project scopes; import from a folder, a .zip, or a GitHub/Gitee URL; scan-and-import skills already on this machine; twenty official scenario templates bundled — one-click install uses the in-app copy and works offline; /save-skill turns the current session's approach into a skill draft in one command
- Remote access: LAN token + QR code opens the full UI on your phone; Tailscale supported for cross-network access (security boundaries and advice in [docs/远程访问安全.md](docs/远程访问安全.md))
- Chat channels: Feishu (WebSocket long connection) / WeChat (QR login); read-only by default, optional approval cards with timeout auto-deny. Generic Webhook channel: outbound-only pushes (optional HMAC signature verification), no inbound allowlist concept
- Desktop form factor: six themes + follow-system, system tray, launch at startup, window geometry memory, global hotkey, desktop pet; multiple session tabs, a ten-tab right panel (auxiliary chat / browser / review / files / tasks / agenda / automation / project memory / memory map / MCP·Skills), and a multi-tab terminal docked at the bottom (Ctrl+\` toggle)
- Session management: full-text search (Chinese-aware), session branching, pin / archive / tags, Markdown / HTML export, rolling backups with visual restore

</details>

## 🖼 Screenshots

| Model provider settings | Agenda weekly view | Usage dashboard |
|:---:|:---:|:---:|
| ![Model provider settings](docs/images/providers.png) | ![Agenda weekly view](docs/images/agenda.png) | ![Usage dashboard](docs/images/usage.png) |
| Paste an API key into a built-in preset to connect | Scheduled tasks and reminders land on the calendar and fire on time | Token usage and cost estimates, per session |

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
│  ├─ config.py  ~/.skysheep/config.toml                           │
│  └─ cli/       Terminal REPL / desktop launcher                  │
└──────────────────────────────────────────────────────────────────┘
```

Design keynote: **event-stream driven** — the entire agent run is modeled as `AgentEvent`s, and the CLI, GUI, and WebSocket server all consume the same engine API; sensitive operations go through the `PermissionGate`, which emits a confirmation event and suspends until the frontend decides.

Auditability: all project source (the Python engine and the frontend HTML/CSS/JS) ships as readable, unobfuscated code; the mermaid/xterm bundles under `static/vendor/` are upstream build artifacts of third-party libraries (versions pinned, never modified).

## 🤝 Development & Contributing

```bash
cd engine
uv run pytest        # Full test suite (-m "not e2e" skips the real-socket tests; -m eval runs the 30-scenario behavioral eval baseline)
uv run ruff check .  # lint
```

Contributions are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md). To report a security vulnerability, use the private channel in [SECURITY.md](SECURITY.md) — please don't open a public issue. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) for the code of conduct.

Running into a problem? In the app, go to Settings → About → "💬 Report an issue" to auto-package a redacted diagnostic bundle and open the feedback page.

## 🗺 Roadmap & Non-goals

- ✅ current release **v2.6.0**: engine core → MCP / Skills ecosystem → desktop app → roundtable multi-model & task pipelines → memory map → review-hardening passes and structural refactors → session split view & adversarial review → runtime reliability & data lifecycle hardening → touch swipe-navigation taming & security-review remediation (full history in [CHANGELOG.md](CHANGELOG.md))
- 🚧 **Next up**: code-signed distribution · Windows desktop polish and stability (until then, see [docs/smartscreen-说明.md](docs/smartscreen-说明.md) for the SmartScreen workaround for unsigned installers)

**Non-goals**:

- **Native mobile apps** — the desktop shell is where permission gates, checkpoints, and the file panel get polished; there is no bandwidth right now for a second, mobile-grade security model. The web PWA shipped in 2.5.0 (add to home screen).
- **macOS / Linux desktop support (for now)** — SkySheep is a Windows desktop product: its security model (DPAPI credential encryption, the Windows sandbox, window and process management, `schtasks` scheduling) rests on Windows. The `--browser` mode can run the engine and web UI on macOS / Linux as a developer fallback, not as a supported product form. Other platforms get reconsidered only once the Windows desktop reaches a stable bar; until then the project deliberately avoids abstracting the security model, window management, and installer flow for cross-platform portability.
- **Vector memory / multi-tier context compaction** — the built-in compaction and archive-time memory digests already cover current usage; not worth growing the install size.
- **Replacing the built-in search with ripgrep** — pure-Python, zero-dependency "install and it works" distribution stays; no bundled binary, but an installed rg may optionally accelerate search (implemented: ASCII-pattern search is accelerated automatically when a local rg is present; the built-in search remains the default).
- **A proprietary plugin JS API** — extension points stay on the two open standards, MCP and Skills; the frontend remains zero-build.
- **An operated skill marketplace** — skills ship via the in-app import flow (folder / .zip / links); no curated store to moderate.

## 📄 License & Sponsorship

Released under the [MIT](LICENSE) license.

Donations are accepted via the channel listed in [.github/FUNDING.yml](.github/FUNDING.yml) — GitHub Sponsors: [Sky-scrape](https://github.com/sponsors/Sky-scrape). The first spending priority is **code-signed distribution**: signed installers remove the Windows SmartScreen warning (see [docs/smartscreen-说明.md](docs/smartscreen-说明.md)) so everyday users can install with a double-click.

---

<div align="center">

☁️ **The cloud sheep is with you** ☁️

</div>
