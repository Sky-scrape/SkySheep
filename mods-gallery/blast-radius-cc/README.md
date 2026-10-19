# 高危命令影响面（Claude Code 插件格式）

这是 `blast-radius` 的 Claude Code 插件格式版本，用于演示：

- `tool.check` 返回 `{ decision: "ask", reason }` —— 强制逐次确认，即使该命令
  本会免确认放行（白名单 / 只读 / 完全访问档都拦不住 Mod 的 ask）
- `ask` 路径的权限卡**不提供「总是允许」**——避免一次点选就永久绕过 Mod
- `tool.pre` 补一条 Notice 留痕

注意：本示例刻意**不返回 deny**，因为 Mod 一旦 deny 用户就只能停用 Mod 恢复。
需要硬拒绝时自写 `{ decision: "deny", reason }`。

安装：设置 · Mods 扩展 → 安装 Mod → 选这个目录（或其 zip 包）。
