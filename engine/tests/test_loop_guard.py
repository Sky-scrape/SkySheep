"""LoopGuard：同参数同结果的原样重试拦截 + 同错误熔断。"""

from __future__ import annotations

from skysheep.core.loop_guard import LoopGuard, fingerprint


def test_fingerprint_is_key_order_insensitive():
    assert fingerprint("read_file", {"path": "a", "n": 1}) == fingerprint(
        "read_file", {"n": 1, "path": "a"}
    )
    assert fingerprint("read_file", {"path": "a"}) != fingerprint(
        "read_file", {"path": "b"}
    )


def test_identical_result_warns_then_blocks():
    g = LoopGuard()
    results = []
    for _ in range(4):
        assert g.before("read_file", {"path": "a"}) is None
        results.append(g.after("read_file", {"path": "a"}, "same body", False))
    # 第 2 次起提示，第 4 次仍放行（拦的是「下一次」）
    assert results[0] == ""
    assert "相同参数" in results[1]
    assert g.before("read_file", {"path": "a"})  # 第 5 次被拦


def test_changed_result_resets_streak():
    g = LoopGuard()
    for i in range(5):
        assert g.before("read_file", {"path": "a"}) is None
        g.after("read_file", {"path": "a"}, f"body {i}", False)
    # 每次结果都不同 = 有进展，不能拦
    assert g.before("read_file", {"path": "a"}) is None


def test_different_args_are_independent():
    g = LoopGuard()
    for _ in range(4):
        g.after("read_file", {"path": "a"}, "same", False)
    assert g.before("read_file", {"path": "a"})
    assert g.before("read_file", {"path": "b"}) is None


def test_same_error_blocks_tool_after_threshold():
    g = LoopGuard()
    for i in range(4):
        assert g.before("run_command", {"command": f"cmd{i}"}) is None
        g.after("run_command", {"command": f"cmd{i}"}, "permission denied", True)
    # 第 5 次同错才熔断（不同命令、同一句错误）
    assert g.before("run_command", {"command": "cmd9"}) is None
    g.after("run_command", {"command": "cmd9"}, "permission denied", True)
    block = g.before("run_command", {"command": "cmd10"})
    assert block and "已暂停调用该工具" in block


def test_different_errors_do_not_trip_breaker():
    g = LoopGuard()
    for i in range(6):
        g.after("run_command", {"command": f"cmd{i}"}, f"compile error {i}", True)
    assert g.before("run_command", {"command": "cmd-new"}) is None


def test_success_resets_error_streak():
    g = LoopGuard()
    for i in range(4):
        g.after("run_command", {"command": f"cmd{i}"}, "boom", True)
    g.after("run_command", {"command": "ok"}, "fine", False)
    for i in range(4):
        g.after("run_command", {"command": f"again{i}"}, "boom", True)
    assert g.before("run_command", {"command": "next"}) is None


def test_reset_clears_turn_state():
    g = LoopGuard()
    for _ in range(4):
        g.after("read_file", {"path": "a"}, "same", False)
    assert g.before("read_file", {"path": "a"})
    g.reset()
    assert g.before("read_file", {"path": "a"}) is None
