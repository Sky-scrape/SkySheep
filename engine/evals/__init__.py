"""SkySheep 行为级评测基线（engine/evals/）。

做成包（有 __init__.py）是为了让本目录的 conftest.py 以 ``evals.conftest``
导入：tests/conftest.py 被测试文件按顶层名 ``conftest`` 导入
（``from conftest import FakeProvider``），两个平级目录各有一份 conftest.py
时顶层名会互相覆盖。包化后两边互不干扰。
"""
