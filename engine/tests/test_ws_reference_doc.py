"""docs/WS方法参考.md 防漂移测试（P27）。

文档由 engine/tools/gen_ws_reference.py 从 server/app.py 的 _WS_METHODS
注册表自动生成。这里把「注册表现算内容」与仓库里已提交的文档逐字比对：
新增 / 改动 WS 方法（或说明、local_only 标志）忘了重跑生成器更新文档，
本地与 CI 都会在这里红。

不 shell out：直接 importlib 加载生成器模块、调用其内容函数。生成时间行
按「已提交文档头部的时间戳」回放后再比对，文档除该行外任何一处手改 /
漂移都逃不掉。只导入注册表、不启动服务器不连 socket。
"""

import importlib.util
import re
from difflib import unified_diff
from functools import lru_cache
from pathlib import Path

_TOOLS = Path(__file__).resolve().parents[1] / "tools"
_REPO_ROOT = _TOOLS.parents[1]  # engine/tools → 仓库根
_DOC = _REPO_ROOT / "docs" / "WS方法参考.md"

_SPEC = importlib.util.spec_from_file_location("gen_ws_reference", _TOOLS / "gen_ws_reference.py")
assert _SPEC and _SPEC.loader
gen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gen)

_REGEN_HINT = "重跑 uv run python tools/gen_ws_reference.py 更新文档"


@lru_cache(maxsize=1)
def _sections():
    """注册表分节（导入 app 拉起完整 backend 导入链，约 1-2 秒，缓存一次）。"""
    return gen.load_sections()


def _committed_doc() -> str:
    if not _DOC.exists():
        raise AssertionError(f"{_DOC} 不存在。{_REGEN_HINT}")
    return _DOC.read_text(encoding="utf-8")


def _header_line(committed: str, prefix: str) -> str:
    m = re.search(rf"(?m)^- {prefix}：(.+)$", committed)
    assert m, f"文档头部找不到「- {prefix}：」行（头部格式变了？同步改生成器与本测试）。{_REGEN_HINT}"
    return m.group(1).strip()


def test_ws_reference_doc_matches_registry():
    """已提交文档必须与注册表现算内容逐字一致（协议漂移的第一道闸）。"""
    committed = _committed_doc()
    # 生成时间行用文档里已提交的时间戳回放：时间本身不该让比对红，
    # 真正要抓的是「协议改了没重跑生成器」造成的其余任何一处差异
    now_text = _header_line(committed, "生成时间")
    actual = gen.build_document(now_text, _sections())
    if actual == committed:
        return
    diff = "\n".join(
        list(
            unified_diff(
                committed.splitlines(),
                actual.splitlines(),
                fromfile="docs/WS方法参考.md（已提交）",
                tofile="注册表现算（重新生成）",
                lineterm="",
            )
        )[:60]
    )
    raise AssertionError(
        f"docs/WS方法参考.md 与 _WS_METHODS 注册表不一致（协议改了没更新文档）。\n"
        f"{_REGEN_HINT}\n\n{diff}"
    )


def test_ws_reference_doc_header_total_matches_registry():
    """文档头部的方法总数必须等于注册表现算总数（失败信息比逐字比对更直白）。"""
    total = sum(len(s.rows) for s in _sections())
    committed = _committed_doc()
    m = re.search(r"(?m)^- 方法总数：(\d+)", committed)
    assert m, f"文档头部找不到「- 方法总数：」行。{_REGEN_HINT}"
    assert int(m.group(1)) == total, (
        f"文档头部方法总数 {m.group(1)} != 注册表现算 {total}。{_REGEN_HINT}"
    )


def test_registry_nonempty_and_local_only_flagged():
    """聚合链路哨兵：注册表非空且 local_only 标志确实进了文档（防「全空表也绿」）。"""
    sections = _sections()
    total = sum(len(s.rows) for s in sections)
    assert total > 0, "_WS_METHODS 聚合出空表，生成链路坏了"
    local_n = sum(1 for s in sections for r in s.rows if r.local_only)
    assert local_n > 0, "local_only 全军覆没（标志位被丢弃？），文档会失去本机专属信息"
