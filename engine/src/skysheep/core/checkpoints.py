"""检查点存储：按会话保存「本轮改动前的文件快照」，支持一键回滚。

数据流：
    write_file/edit_file 覆盖前 → ChangeRecorder.record() 记改前字节
    → 一轮 turn 结束 → backend 把快照存入 CheckpointStore
    → 用户点「撤销本轮改动」→ restore() 把字节写回（新建的文件删除）

只追踪 write_file / edit_file / generate_image 三个落盘工具；run_command 等
命令造成的改动不追踪（界面上会提示这一边界）。

持久化：构造时传入 root（~/.skysheep/backups/checkpoints/<项目指纹>/）即落盘
——每个检查点一个目录（meta.json + 改前内容 blob），重启后仍可回滚；
不传 root 则维持纯内存行为（CLI / 测试用）。超上限按时间淘汰最旧的。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

MAX_CHECKPOINTS = 50


def _safe_dirname(raw: str) -> str:
    """会话 id → 目录名：白名单字符直接用，否则指纹化（防奇异字符进路径）。"""
    ok = all(c.isalnum() or c in "-_" for c in raw) and raw.isascii()
    return raw if (raw and ok) else "s-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


class CheckpointStore:
    """checkpoint_id -> {id, session_id, files, paths, ts}，容量上限 FIFO。

    root 传入时同步写盘：files 内容在内存里首用时加载（_hydrate），
    列表操作只读 meta，不碰大文件内容。
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root else None
        self._items: dict[str, dict] = {}
        self._seq = 0
        if self._root is not None:
            self._load_from_disk()

    # ---- 磁盘布局：root/<会话目录>/<cp_id>/（meta.json + 0.bin、1.bin…） ----

    def _cp_dir(self, cp: dict) -> Path:
        return self._root / _safe_dirname(cp["session_id"] or "quick") / cp["id"]

    def _load_from_disk(self) -> None:
        """启动时扫描 meta.json 恢复检查点索引（内容懒加载）；坏条目跳过。"""
        try:
            session_dirs = sorted(self._root.iterdir())
        except OSError:
            return
        max_seq = 0
        for sdir in session_dirs:
            if not sdir.is_dir():
                continue
            try:
                cp_dirs = sorted(sdir.iterdir())
            except OSError:
                continue
            for cp_dir in cp_dirs:
                meta_path = cp_dir / "meta.json"
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    cp = {
                        "id": str(meta["id"]),
                        "session_id": meta.get("session_id"),
                        "paths": [str(p) for p in meta.get("paths") or []],
                        "ts": float(meta.get("ts") or 0),
                        "blobs": {str(k): v for k, v in (meta.get("blobs") or {}).items()},
                        "files": None,  # 懒加载：get/restore 时才读 blob
                    }
                except (OSError, ValueError, TypeError):
                    continue
                self._items[cp["id"]] = cp
                digits = "".join(c for c in cp["id"] if c.isdigit())
                if digits:
                    max_seq = max(max_seq, int(digits))
        self._seq = max_seq

    def _hydrate(self, cp: dict) -> None:
        """把磁盘上的 blob 读进 cp['files']（仅 root 模式且未加载时）。"""
        if cp.get("files") is not None or not self._root:
            return
        files: dict[str, bytes | None] = {}
        cp_dir = self._cp_dir(cp)
        for path_s, blob in (cp.get("blobs") or {}).items():
            if blob is None:
                files[path_s] = None
                continue
            try:
                files[path_s] = (cp_dir / str(blob)).read_bytes()
            except OSError:
                files[path_s] = None  # blob 丢了按「新建文件」处理，回滚时删除
        cp["files"] = files

    def _persist(self, cp: dict) -> None:
        """把检查点写进磁盘目录；失败静默（内存里的仍在，回滚能力不打折）。"""
        cp_dir = self._cp_dir(cp)
        try:
            cp_dir.mkdir(parents=True, exist_ok=True)
            blobs: dict[str, str | None] = {}
            for i, (path_s, data) in enumerate(cp["files"].items()):
                if data is None:
                    blobs[path_s] = None
                else:
                    blob_name = f"{i}.bin"
                    (cp_dir / blob_name).write_bytes(data)
                    blobs[path_s] = blob_name
            meta = {
                "id": cp["id"],
                "session_id": cp["session_id"],
                "ts": cp["ts"],
                "paths": cp["paths"],
                "blobs": blobs,
            }
            (cp_dir / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False), encoding="utf-8"
            )
            cp["blobs"] = blobs
        except OSError:
            pass

    def _prune(self) -> None:
        """超过上限时按时间淘汰最旧的（内存 + 磁盘）。"""
        while len(self._items) > MAX_CHECKPOINTS:
            oldest_id = min(self._items, key=lambda k: self._items[k]["ts"])
            cp = self._items.pop(oldest_id)
            if self._root is not None:
                try:
                    shutil.rmtree(self._cp_dir(cp), ignore_errors=True)
                except OSError:
                    pass

    # ---- 对外接口（与旧版语义一致） ----

    def save(self, session_id: str | None, pre: dict[str, bytes | None]) -> dict | None:
        """保存一轮的改前快照；空快照（本轮没改文件）返回 None。"""
        if not pre:
            return None
        self._seq += 1
        cp = {
            "id": f"cp{self._seq}",
            "session_id": session_id,
            "files": dict(pre),
            "paths": sorted(pre),
            "ts": time.time(),
            "blobs": {},
        }
        self._items[cp["id"]] = cp
        if self._root is not None:
            self._persist(cp)
        self._prune()
        return {"id": cp["id"], "paths": cp["paths"], "ts": cp["ts"]}

    def list_for(self, session_id: str | None) -> list[dict]:
        return [
            {"id": c["id"], "paths": c["paths"], "ts": c["ts"]}
            for c in sorted(self._items.values(), key=lambda c: c["ts"])
            if c["session_id"] == session_id
        ]

    def get(self, checkpoint_id: str) -> dict | None:
        """按 id 取检查点（含改前内容，供 diff / 审查用）；不存在返回 None。"""
        cp = self._items.get(checkpoint_id)
        if cp is None:
            return None
        self._hydrate(cp)
        return {
            "id": cp["id"],
            "session_id": cp["session_id"],
            "files": cp["files"],
            "paths": cp["paths"],
            "ts": cp["ts"],
        }

    def restore(self, checkpoint_id: str) -> list[str]:
        """把快照写回磁盘：有改前内容的恢复内容，新建文件直接删除。

        返回受影响的文件路径；检查点不存在抛 KeyError。
        """
        cp = self._items.get(checkpoint_id)
        if cp is None:
            raise KeyError("checkpoint not found: " + checkpoint_id)
        self._hydrate(cp)
        restored: list[str] = []
        for path_s, data in cp["files"].items():
            p = Path(path_s)
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
            restored.append(path_s)
        return restored
