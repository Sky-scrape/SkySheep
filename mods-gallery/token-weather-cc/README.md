# 上下文天气（Claude Code 插件格式）

这是 `token-weather` 的 Claude Code 插件格式版本，用于演示：

- `.claude-plugin/plugin.json` 清单位置（而不是 `mod.json`）
- `hooks/main.js` 入口约定
- `register(event, handler)` 声明式事件注册
- `($, e, next)` 中间件签名
- 元素树 UI（`{ slot, ui: { type, ... } }`）

状态用 `$.state.get/set`（引擎代存于本 Mod 的 state.json），时钟用 `$.clock.now()`。

安装：设置 · Mods 扩展 → 安装 Mod → 选这个目录（或其 zip 包）。
