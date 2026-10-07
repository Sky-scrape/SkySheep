# mods-gallery — 官方示例 Mod

随应用内置的官方示例 Mod（实验性）。结构与 `skills-gallery/` 一致：一目录一个 Mod
（`mod.json` + `main.js` + `README.md`），根 `manifest.json` 是离线快照；
安装入口在 **设置 · Mods 扩展（实验性）** 的官方示例区（一键安装，从打包内副本装，
无需联网）。

| 目录 | Mod | 定位（对照 Claude Code Mods） | hooks | permissions |
|---|---|---|---|---|
| `token-weather/` | 上下文天气 | Token Weather：把上下文占用做成天气式常驻状态条（晴/多云/风暴） | tool_post, turn_stop | observe |
| `blast-radius/` | 高危命令影响面 | Blast Radius：高危命令强制逐次确认 + 影响面说明（只收紧，不放行） | permission_request, tool_pre | tighten |

两个示例同样是「Mods 怎么写」的参考实现：事件挂点、`sky.state` 状态、
Widget 词表与 declarative 收紧声明的最小用法。Mod 的安全模型与能力边界见
`engine/src/skysheep/core/mods.py` 模块注释与 `SECURITY.md` 的「实验性 Mods 的边界」。
