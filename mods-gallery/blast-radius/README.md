# 高危命令影响面（blast-radius）

SkySheep 官方示例 Mod（实验性）。对位 Claude Code Mods 生态里的 Blast Radius：
高风险命令执行前展示影响面供用户决策。

## 收紧路径（三层，全部只收紧、不放行）

1. **强制确认（静态声明，不依赖 JS）**：清单里
   `declarative.require_confirm_tools = ["run_command"]`——即使本项目白名单、
   「自动允许写入」档甚至「完全访问」档本会放行，`run_command` 一律回落
   逐次确认，确认卡上会注明「Mods 收紧声明命中」；
2. **确认卡补影响面说明**：`permissionRequest` 识别命令里的高危动作
   （递归删除、格式化、强杀进程、强推远端……），在确认卡上以
   「[Mod·blast-radius]」标识给出影响面说明；
3. **放行留痕**：`toolPre` 对最终放行的高危调用补一条 Notice。

## 定位

引擎原生的确认预览覆盖「写文件 / 删目录」的 diff，而**命令文本 → 语义影响面**
正是内建没覆盖、由 Mod 来补的面。本示例刻意**不 deny**——收紧 = 强制确认 +
补信息；拒绝能力（`declarative.deny_tools` 或 handler 返回 deny）由用户自写
清单按需声明。

高危词表是示例内置的精简版，可按需在 `main.js` 里扩充。
