"""后端杂症修整的回归锚点（两处）：

① collab_gallery.collab_template_list 对 CollabTemplateStore.skipped 直取属性：
   存储类 __init__ 总是初始化 skipped（core/collab_templates.py），原 getattr
   防御是多余的——这里锚「skipped 如实带出、0 与 >0 两态直取不抛错」。
② checkpoints._persist 落盘失败不再静默：记一条带失败原因的 WARNING；
   行为语义不变——save 仍返回检查点、内存副本保留（重启前回滚仍可用）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

from skysheep.core.checkpoints import CheckpointStore
from skysheep.core.collab_templates import CollabTemplateStore
from skysheep.server.backend_parts.collab_gallery import CollabGalleryMixin


class _GalleryHarness(CollabGalleryMixin):
    """collab_template_list 只依赖这两员：真实协作册 + 空团队模板册。"""

    def __init__(self, store: CollabTemplateStore) -> None:
        self.collab_templates = store
        self.team_templates = SimpleNamespace(templates=[])


def _write_store_file(path: Path, items: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"templates": items}, ensure_ascii=False), encoding="utf-8"
    )


def test_collab_template_list_reads_skipped_directly(tmp_path):
    """skipped 直取不抛错，且空册 0 / 坏条目 >0 两态都如实带出。"""
    path = tmp_path / "collab_templates.json"

    # 空册：skipped == 0
    store = CollabTemplateStore(path)
    store.load()
    result = _GalleryHarness(store).collab_template_list()
    assert result["skipped"] == 0
    assert result["skipped"] == store.skipped

    # 两条坏条目（非 dict 一条、kind=team 一条）计入 skipped，好条目照常进列表
    _write_store_file(path, [
        "坏条目-不是字典",
        {
            "name": "不该进协作册", "kind": "team",
            "roles": [{"key": "x", "name": "乙"}],
        },
        {
            "name": "我的圆桌", "kind": "roundtable",
            "roles": [{"key": "a", "name": "甲", "prompt": "p"}],
        },
    ])
    store.load()
    assert store.skipped == 2
    result = _GalleryHarness(store).collab_template_list()
    assert result["skipped"] == 2
    assert result["skipped"] == store.skipped
    assert sum(1 for t in result["templates"] if t["name"] == "我的圆桌") == 1


def test_persist_failure_logs_warning(tmp_path, monkeypatch, caplog):
    """_persist 失败记 WARNING（带检查点 id 与失败原因），语义不变仍走内存保留。"""
    store = CheckpointStore(tmp_path / "cps")
    work = tmp_path / "work.txt"
    work.write_bytes(b"OLD")

    def _raiser(*args, **kwargs):
        raise OSError("磁盘满")

    monkeypatch.setattr("skysheep.core.checkpoints.write_bytes_atomic", _raiser)
    with caplog.at_level(logging.WARNING, logger="skysheep.core.checkpoints"):
        cp = store.save("s1", {str(work): b"OLD"})

    assert cp is not None  # 失败不致命：save 仍返回检查点
    warnings = [
        r for r in caplog.records
        if r.name == "skysheep.core.checkpoints" and r.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "落盘失败" in message and "磁盘满" in message
    assert cp["id"] in message

    # 行为语义不变：内存副本未被释放（get 仍能取到改前字节），磁盘上没有 meta
    hydrated = store.get(cp["id"])
    assert hydrated is not None
    assert hydrated["files"][str(work)] == b"OLD"
    assert not (tmp_path / "cps" / "s1" / cp["id"] / "meta.json").exists()
