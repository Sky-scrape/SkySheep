"""评测基线 · 协作行为场景（eval 标记入口）。

场景本体与断言在 tests/test_collab_scenarios.py（协作组装行为的默认套件回归——
fake provider 驱动真实 ServerBackend 协作管道，不联网、不碰真实 home）；本文件
把同一批场景函数挂进 evals 基线：eval 标记由本目录 conftest 自动打，
`uv run pytest -m eval` 与默认套件跑的是同一份实现，不存在两套断言漂移。

为什么场景本体不在 evals/：evals 目录的用例被 pyproject 的 addopts
（-m "not eval"）在收集期剔除，直接 `pytest evals/xxx.py` 一条也收不到
（evals/README.md「为什么单跑文件也要带 -m eval」）——协作回归要进默认套件
持续护航，所以本体放 tests/，这里只做挂载。
"""

from tests.test_collab_scenarios import (  # noqa: F401
    test_collab_adversarial_verdicts,
    test_collab_roundtable_fusion,
    test_collab_team_closed_loop,
)
