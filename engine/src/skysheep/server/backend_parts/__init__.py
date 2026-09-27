"""ServerBackend 的职责区拆分（mixin 子模块）。

backend.py 保留核心（__init__/生命周期/对话主流程/快照/分发辅助），
按职责注释切成若干 mixin，`Backend` 继承它们组装出完整能力。
各 mixin 模块不得 import backend（会循环引用）；共享的小件（类型别名、
纯数据类）放在 `_shared.py`，由 backend 与 mixin 双方引用。
"""
