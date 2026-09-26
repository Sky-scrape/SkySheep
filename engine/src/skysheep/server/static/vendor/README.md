# vendor/ — 第三方前端库（本地化，零构建直引）

本目录内的文件是第三方库的**构建产物副本**（minified），随应用静态目录分发，
页面不走任何 CDN——离线可用，也避免外链被墙/被投毒。index.html 直接引
`highlight.min.js / qrcode.min.js / xterm.css`，mermaid 与 xterm 由 app.js `loadLib` 懒加载。

**升级或更换任一文件时，请在下表补一行「版本 + 来源 URL + 拉取日期」**，
README.en.md 对外声明 "versions pinned, never modified"，这份清单就是该声明的可核验依据。

| 文件 | 库 | 版本 | 来源 | 拉取日期 | 备注 |
|---|---|---|---|---|---|
| highlight.min.js | highlight.js | **11.9.0**（文件内可核验） | https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js （或同级 cdn） | 2025-09 前（≤2025-09-16） | 语法高亮 |
| mermaid.min.js | mermaid | **11.17.2**（11.x 系最新；12.0.0 已发布但跨大版本未验证，暂不跟） | https://cdn.jsdelivr.net/npm/mermaid@11.17.2/dist/mermaid.min.js | 2026-09-26 | 3,572,661 字节，sha256 `581ed7d7…390eb8`；流程图/时序图渲染，app.js 以 `securityLevel: "strict"` 初始化；浏览器实测图表渲染正常 |
| xterm.js | @xterm/xterm（终端模拟） | **5.5.0**（5.x 系最新；6.0.0 已发布但跨大版本未验证，暂不跟） | https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.min.js | 2026-09-26 | 289,714 字节，sha256 `4196e242…49e7bf`；右侧终端面板；夹具实测 Terminal/FitAddon 全局与初始化正常 |
| xterm-fit.js | @xterm/addon-fit | **0.10.0**（与 xterm 5.5.0 配套） | https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.min.js | 2026-09-26 | FitAddon，1,779 字节，sha256 `a6a7bbb3…1aa38f1` |
| xterm.css | @xterm/xterm 样式 | 与 xterm.js 同包（5.5.0） | https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css | 2026-09-26 | 5,559 字节，sha256 `ba8e6985…17faf2b6` |
| qrcode.min.js | qrcodejs（davidshimjs 风格） | 上游即无版本号/发版 | https://github.com/davidshimjs/qrcodejs | ≤2025-09-15 | 19,927 字节；局域网连接二维码 |

> 2026-09-25 审查注：本表由安全审查建立；前三行的具体版本/URL 是历史遗留空缺，
> 当初拉取的记录已不可考。下次升级任一库时把准确信息补进表格即可闭环。
