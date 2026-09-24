"""检查点存储：按会话保存「本轮改动前的文件快照」，支持一键回滚。

数据流：
    write_file/edit_file 覆盖前 → ChangeRecorder.record() 记改前字节
    → 一轮 turn 结束 → backend 把快照存入 CheckpointStore
    → 用户点「撤销本轮改动」→ restore() 把字节写回（新建的文件删除）

只追踪 write_file / edit_file / write_document / generate_image / move_file /
make_dir / delete_file 这些落盘工具；run_command 等命令造成的改动不追踪
（界面上会提示这一边界）——这也是工具层要提供 move_file / delete_file 的原因：
让文件整理这类典型任务走可回滚的正规工具，而不是 run_command 里的 mv / rm。

持久化：构造时传入 root（~/.skysheep/backups/checkpoints/<项目指纹>/）即落盘
——每个检查点一个目录（meta.json + 改前内容 blob），重启后仍可回滚；
不传 root 则维持纯内存行为（CLI / 测试用）。

并行安全（同一项目多会话/任务并行写同一目录）：
- 淘汰按会话分桶（各会话各有 MAX_CHECKPOINTS 条配额），别的会话轮次再多
  也不会把本会话的快照挤掉；另设全项目兜底上限防无限增长。
- 保存时记录每个文件「保存时刻」的内容签名；回滚前比对当前签名——
  不一致说明快照之后又有别的任务改过这个文件，直接覆盖会抹掉别人的
  改动，此时抛 CheckpointConflictError，由用户确认后 force 恢复。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from ..textio import write_bytes_atomic, write_text_atomic

MAX_CHECKPOINTS = 50  # 每个会话保留的检查点数（按会话分桶淘汰）
# 字节上限（安全审查 M15）：旧实现只按「条数」淘汰，一轮里改过的文件内容
# 全量进快照——模型写一个 2GB 的文件、50 条快照就能把磁盘吃穿。三层限额：
MAX_CHECKPOINT_FILE_BYTES = 32 << 20    # 单文件：超过就不进快照（大二进制文件）
MAX_CHECKPOINT_BYTES = 256 << 20        # 单条检查点：所有文件合计上限
MAX_CHECKPOINT_TOTAL_BYTES = 1 << 30    # 全库合计：超了淘汰最旧的
# 全项目兜底上限：正常几十个会话都到不了；只防「海量会话 × 各 50 条」把
# 磁盘/内存吃穿。触发时按时间淘汰全库最旧的。
MAX_CHECKPOINTS_TOTAL = 500


def _safe_dirname(raw: str) -> str:
    """会话 id → 目录名：白名单字符直接用，否则指纹化（防奇异字符进路径）。"""
    ok = all(c.isalnum() or c in "-_" for c in raw) and raw.isascii()
    return raw if (raw and ok) else "s-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _state_sig(path_s: str) -> str | None:
    """文件当前状态的签名：内容 sha256；目录记 "dir"；不存在记 None。

    签名只用于「保存之后有没有又被改过」的比对，不承担编码/行尾符职责。
    """
    p = Path(path_s)
    try:
        if p.is_dir():
            return "dir"
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _limit_pre(pre: dict[str, bytes | None]) -> tuple[dict[str, bytes | None], list[str]]:
    """按字节上限裁剪一轮快照，返回 (保留项, 被跳过的大文件路径)。

    超限的文件不进快照（而不是整条检查点丢弃）：宁可这些文件回滚不了，
    也不能让一次大文件写入把磁盘/内存吃穿（安全审查 M15）。
    """
    kept: dict[str, bytes | None] = {}
    skipped: list[str] = []
    total = 0
    # 先按大小从小到大收：同一轮里大文件先被跳过，尽量多保住小文件的可回滚性
    for path_s, data in sorted(pre.items(), key=lambda kv: len(kv[1] or b"")):
        size = len(data) if data is not None else 0
        if data is not None and size > MAX_CHECKPOINT_FILE_BYTES:
            skipped.append(path_s)
            continue
        if total + size > MAX_CHECKPOINT_BYTES:
            skipped.append(path_s)
            continue
        kept[path_s] = data
        total += size
    return kept, skipped


class CheckpointConflictError(Exception):
    """回滚目标文件在快照保存后又被改过（可能是并行任务写入）。

    携带冲突文件列表；用户确认接受覆盖后以 force=True 重试。
    """

    def __init__(self, conflicts: list[str]) -> None:
        super().__init__("files changed after checkpoint save: " + ", ".join(conflicts))
        self.conflicts = list(conflicts)


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
                    blobs = {str(k): v for k, v in (meta.get("blobs") or {}).items()}
                    bytes_total = int(meta.get("bytes") or 0)
                    if not bytes_total:
                        # 旧版 meta 没存 bytes：按 blob 文件实际大小补算，
                        # 全库字节上限对存量检查点才继续有效
                        for blob in blobs.values():
                            if blob:
                                try:
                                    bytes_total += (cp_dir / str(blob)).stat().st_size
                                except OSError:
                                    pass
                    cp = {
                        "id": str(meta["id"]),
                        "session_id": meta.get("session_id"),
                        "paths": [str(p) for p in meta.get("paths") or []],
                        "ts": float(meta.get("ts") or 0),
                        "blobs": blobs,
                        # 保存时刻的内容签名（旧版 meta 没有该字段 → 空表 =
                        # 跳过脏检查，回滚保持旧行为）
                        "sigs": {str(k): v for k, v in (meta.get("sigs") or {}).items()},
                        "bytes": bytes_total,
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
        """把磁盘上的 blob 读进 cp['files']（仅 root 模式且未加载时）。

        blob 读不到（被外部清理/局部丢失）的路径直接剔除：绝不能按「改前
        不存在」处理——那会让回滚把用户现存的文件删掉。恢复不了就少恢复
        一个文件，不制造破坏。"""
        if cp.get("files") is not None or not self._root:
            return
        files: dict[str, bytes | None] = {}
        cp_dir = self._cp_dir(cp)
        for path_s, blob in (cp.get("blobs") or {}).items():
            if blob is None:
                files[path_s] = None  # 改前确实不存在（落库时就是 None）
                continue
            try:
                files[path_s] = (cp_dir / str(blob)).read_bytes()
            except OSError:
                continue  # blob 丢了：这个路径回滚不了，也别碰它
        cp["files"] = files

    def _release(self, cp: dict) -> None:
        """用后释放：磁盘模式下内容已落 blob，内存里的副本可以丢弃，
        下次 get/restore 再懒加载。纯内存模式（无 root）不能丢——那是唯一副本。
        不释放的话，点一次「对比/撤销」就让改前字节（含图片等大二进制）常驻
        到该检查点被 50 条上限挤出为止。"""
        if self._root is not None:
            cp["files"] = None

    def _persist(self, cp: dict) -> bool:
        """把检查点写进磁盘目录；返回是否写成功（失败时内存副本不能丢）。"""
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
                "sigs": cp.get("sigs") or {},
                # 字节量随 meta 持久化：重启后全库字节上限（淘汰）才有数可依
                "bytes": int(cp.get("bytes") or 0),
            }
            write_text_atomic(cp_dir / "meta.json", json.dumps(meta, ensure_ascii=False))
            cp["blobs"] = blobs
            return True
        except OSError:
            return False

    def _prune(self) -> None:
        """超限淘汰（内存 + 磁盘）。

        主规则按会话分桶：每个会话各留 MAX_CHECKPOINTS 条，超出淘汰该桶最旧的
        ——并行会话/流水线节点轮次再多，也不会把别的会话还能回滚的快照挤掉。
        另设全项目兜底上限（MAX_CHECKPOINTS_TOTAL）：极端多会话时按时间淘汰
        全库最旧的，防止磁盘与内存无界增长。
        """
        by_session: dict[object, list[str]] = {}
        for k, cp in self._items.items():
            by_session.setdefault(cp["session_id"], []).append(k)
        victims: list[str] = []
        for ids in by_session.values():
            if len(ids) <= MAX_CHECKPOINTS:
                continue
            ids.sort(key=lambda k: self._items[k]["ts"])
            victims.extend(ids[: len(ids) - MAX_CHECKPOINTS])
        if len(self._items) > MAX_CHECKPOINTS_TOTAL:
            ordered = sorted(self._items, key=lambda k: self._items[k]["ts"])
            victims.extend(ordered[: len(self._items) - MAX_CHECKPOINTS_TOTAL])
        # 字节兜底：条数没超但快照都很胖时，按时间从旧到新淘汰到总量之内
        # （安全审查 M15：只限条数挡不住「几十条 × 几百 MB」）
        alive = {k: v for k, v in self._items.items() if k not in set(victims)}
        total = sum(int(v.get("bytes") or 0) for v in alive.values())
        if total > MAX_CHECKPOINT_TOTAL_BYTES:
            for cp_id in sorted(alive, key=lambda k: alive[k]["ts"]):
                if total <= MAX_CHECKPOINT_TOTAL_BYTES:
                    break
                total -= int(alive[cp_id].get("bytes") or 0)
                victims.append(cp_id)
        for cp_id in dict.fromkeys(victims):  # 去重保序
            cp = self._items.pop(cp_id, None)
            if cp is not None and self._root is not None:
                try:
                    shutil.rmtree(self._cp_dir(cp), ignore_errors=True)
                except OSError:
                    pass

    # ---- 对外接口（与旧版语义一致） ----

    def save(self, session_id: str | None, pre: dict[str, bytes | None]) -> dict | None:
        """保存一轮的改前快照；空快照（本轮没改文件）返回 None。

        同时记录每个文件「保存时刻」的内容签名（此刻磁盘上就是本轮改完的
        样子）；回滚时据此判断快照之后有没有被并行任务改过。
        """
        if not pre:
            return None
        kept, skipped = _limit_pre(pre)
        if not kept:
            return None
        self._seq += 1
        cp = {
            "id": f"cp{self._seq}",
            "session_id": session_id,
            "files": dict(kept),
            "paths": sorted(kept),
            "ts": time.time(),
            "blobs": {},
            "sigs": {path_s: _state_sig(path_s) for path_s in sorted(kept)},
            "bytes": sum(len(v) for v in kept.values() if v is not None),
        }
        self._items[cp["id"]] = cp
        if self._root is not None:
            # 落盘成功才释放内存副本；写失败（磁盘满等）时内存是唯一副本，
            # 丢了这条检查点就只剩一个空壳 id，回滚时静默无效
            if self._persist(cp):
                self._release(cp)  # 内容已落 blob：内存只留索引，回滚时再懒加载
        self._prune()
        return {
            "id": cp["id"], "paths": cp["paths"], "ts": cp["ts"], "skipped": skipped,
        }

    def list_for(self, session_id: str | None) -> list[dict]:
        return [
            {"id": c["id"], "paths": c["paths"], "ts": c["ts"]}
            for c in sorted(self._items.values(), key=lambda c: c["ts"])
            if c["session_id"] == session_id
        ]

    def forget_session(self, session_id: str | None) -> int:
        """清除某会话的全部检查点（内存索引 + 磁盘目录），返回清除条数。

        会话被删除时调用：改前字节是用户文件的完整快照，会话没了就不该
        再留——靠条数淘汰慢慢蒸发太慢，还占磁盘。只清当前 store 根目录
        （本项目绑定）下能找到的；会话在其他项目绑定期间留下的检查点
        归那次绑定的根目录管，由删项目时的整目录清理兜底。"""
        victims = [k for k, cp in self._items.items()
                   if cp.get("session_id") == session_id]
        for k in victims:
            cp = self._items.pop(k, None)
            if cp is not None and self._root is not None:
                shutil.rmtree(self._cp_dir(cp), ignore_errors=True)
        return len(victims)

    def get(self, checkpoint_id: str) -> dict | None:
        """按 id 取检查点（含改前内容，供 diff / 审查用）；不存在返回 None。

        内容随返回结构走：store 本体用后即释放，改前字节不再常驻内存。
        """
        cp = self._items.get(checkpoint_id)
        if cp is None:
            return None
        self._hydrate(cp)
        out = {
            "id": cp["id"],
            "session_id": cp["session_id"],
            "files": cp["files"],
            "paths": cp["paths"],
            "ts": cp["ts"],
        }
        self._release(cp)
        return out

    def restore(self, checkpoint_id: str, force: bool = False) -> list[str]:
        """把快照写回磁盘：有改前内容的恢复内容，新建文件直接删除。

        回滚前做脏检查：文件当前内容与保存时刻的签名不一致，说明快照之后
        又被改过（最典型是另一个并行会话/任务写了同一文件）——直接覆盖会把
        那份改动抹掉，此时抛 CheckpointConflictError 列出冲突文件，由调用方
        请用户确认后以 force=True 重试。旧版快照（meta 里没存签名）不做检查，
        保持原有行为。

        返回受影响的文件路径；检查点不存在抛 KeyError。
        """
        cp = self._items.get(checkpoint_id)
        if cp is None:
            raise KeyError("checkpoint not found: " + checkpoint_id)
        self._hydrate(cp)
        sigs = cp.get("sigs") or {}
        if sigs and not force:
            conflicts = sorted(
                path_s for path_s, expected in sigs.items()
                if _state_sig(path_s) != expected
            )
            if conflicts:
                self._release(cp)
                raise CheckpointConflictError(conflicts)
        restored: list[str] = []
        for path_s, data in cp["files"].items():
            p = Path(path_s)
            if data is None:
                # 改前不存在 → 回滚时应删除。目录要连内容一起删（delete_file /
                # move_file 记的是目录本身，shutil.move 后路径已不存在，只有
                # 「目标位置是新建目录」这类情况会走到这里）。
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.exists():
                    p.unlink()
            else:
                # 原子写：回滚写一半被中断会把文件留在半截状态（安全审查低危项）
                p.parent.mkdir(parents=True, exist_ok=True)
                write_bytes_atomic(p, data)
            restored.append(path_s)
        self._release(cp)
        return restored
