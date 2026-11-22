"""ServerBackend 的职责区拆分（mixin 子模块）。

backend.py 保留运行时内核：`__init__` 一次铺齐共享字段、runtime 池与项目
上下文、Agent / 工具注册表装配、setup / shutdown、事件底座（StreamDeltaMerger
与模块级共用件）、`snapshot` / `boot` 前门契约与 WS 分发辅助。对话主流程已在
批 5a 迁到 chat_pipeline.py；会话模型 / 权限档位 / 用量统计 / 项目切换等在
批 5b 按域迁出；其余职责按注释切成 mixin，`ServerBackend` 继承它们组装出
完整能力。
各 mixin 模块不得 import backend（会循环引用）：内核共用件的模块级名字由
backend 在自身定义完成后单向注入各 mixin 模块的命名空间（见 backend.py 末尾
的接线注记）；共享的小件（类型别名、纯数据类）放在 `_shared.py`，由 backend
与 mixin 双方引用。
"""
