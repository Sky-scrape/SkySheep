"""子进程控制台窗口抑制（Windows）。

桌面壳是 GUI 进程（打包版 console=False、`.pyw` 入口），自身没有控制台。
此时拉起控制台程序（cmd.exe / git / taskkill / schtasks …），Windows 会给
子进程新分配一个控制台——用户看到的就是「Agent 一执行命令就闪一个黑窗口」。

CREATE_NO_WINDOW 给子进程一个不可见的控制台：管道输出、退出码、超时树杀
全部照常，只是不显示窗口。POSIX 上该标志不存在，统一取 0（Popen 的
creationflags 默认值就是 0，行为不变）。

MCP 的 stdio 客户端由 mcp SDK 自带同一处理，hooks / 渠道 / 搜索等既有
spawn 点也已各自带该标志；新增「引擎拉起子进程」的代码请从这里取。
"""

from __future__ import annotations

import sys

# 0x08000000 = CREATE_NO_WINDOW（仅 Windows 有效）
NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
