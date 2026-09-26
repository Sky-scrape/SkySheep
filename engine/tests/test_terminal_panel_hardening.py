# 终端面板加固（2026-09-25 审查 P1-1 的回归）+ web_fetch URL 上限（P1-2 的回归）。
#
# P1-1 的裂缝：TerminalSlot.spawn 不传 env 时 ConPTY 子进程整体继承 os.environ
# （终端里 echo 一下就能读走 *_API_KEY），且 term_data 输出广播给所有 WS 连接
# （spawn/input 仅本机，输出却远端可见）。修复口径：spawn 走与 run_command 相同
# 的 child_environment()；term_data/term_exit 对非 local 连接在 emit 过滤层丢弃。
from pathlib import Path

from skysheep.server.backend import TerminalSlot
from skysheep.tools.shell import child_environment
from skysheep.tools.web import WebFetchArgs


class _FakePty:
    """替身：截获 PtyProcess.spawn 的调用参数，不真起 ConPTY。"""

    captured: dict = {}

    @classmethod
    def spawn(cls, argv, cwd=None, env=None, dimensions=None, backend=None):
        cls.captured = {"argv": argv, "cwd": cwd, "env": env, "dimensions": dimensions}
        return object()  # TerminalSlot 只存引用；本测试不触及其方法


def test_terminal_spawn_uses_secret_stripped_env(monkeypatch, tmp_path):
    """spawn 必须把剥敏后的环境传给 ConPTY：密钥变量不进终端进程。"""
    import winpty

    monkeypatch.setattr(winpty, "PtyProcess", _FakePty)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-TERMINAL-LEAK")
    monkeypatch.setenv("MY_BOT_TOKEN", "tok-TERMINAL-LEAK")
    monkeypatch.setenv("PATH", "C:\\Windows")  # 必要变量要保留

    slot = TerminalSlot()
    slot.spawn(tmp_path, rows=24, cols=80)

    env = _FakePty.captured["env"]
    assert env is not None, "spawn 未传 env（回退成整体继承 os.environ）"
    assert "OPENAI_API_KEY" not in env
    assert "MY_BOT_TOKEN" not in env
    assert env.get("PATH") == "C:\\Windows"
    # 与 run_command 的口径同一份实现
    assert env == child_environment()


def test_terminal_spawn_is_idempotent_when_alive(monkeypatch, tmp_path):
    """已存活的标签不重复 spawn（幂等是既有约定）。"""

    class _AlivePty:
        @classmethod
        def spawn(cls, *a, **kw):
            raise AssertionError("存活时不应再次 spawn")

    import winpty

    monkeypatch.setattr(winpty, "PtyProcess", _AlivePty)

    class _Proc:
        def isalive(self):
            return True

    slot = TerminalSlot()
    slot.proc = _Proc()
    slot.spawn(tmp_path, rows=24, cols=80)


def test_app_filters_terminal_events_for_remote():
    """源码契约：远端 emit 过滤层必须丢弃 term_data/term_exit。

    过滤闭包在 WS 处理器内部无法直接调用；与 test_ui_prefs 读 app.js 同理，
    用源码断言锁住接线——有人删过滤分支时测试变红。
    """
    from skysheep.server import app as server_app

    src = Path(server_app.__file__).read_text(encoding="utf-8")
    assert 'elif k in ("term_data", "term_exit"):' in src
    # 过滤分支必须位于「非本机」条件内（粗校验：过滤行出现在 client_is_local 判定之后）
    assert src.index("client_is_local = _client_is_local") < src.index(
        'elif k in ("term_data", "term_exit"):'
    )


def test_web_fetch_url_length_cap():
    """web_fetch URL 总长上限：超长 query 的外带面在参数层直接拒绝（审查 P1-2）。"""
    ok = WebFetchArgs(url="https://example.com/page?q=hello")
    assert ok.url == "https://example.com/page?q=hello"
    long_url = "https://example.com/?d=" + "A" * 3000
    try:
        WebFetchArgs(url=long_url)
    except Exception as e:  # pydantic ValidationError
        assert "url" in str(e).lower()
    else:
        raise AssertionError("超长 URL 未被拒绝")
