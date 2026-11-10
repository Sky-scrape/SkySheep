"""项目内文档索引（文档级 RAG）：分块 + 落盘索引 + 混合检索（BM25 + 向量）。

解决的问题：``read_document`` 只能把整本 PDF 抽成文本塞进上下文——200 页的
手册要么截断、要么靠 ``start_page`` 一页页翻，问一句「第 7 章那个参数默认
值是多少」要来回读很多轮。本模块把文档切成带页码的小块、落一份本地索引，
之后按问题检索出最相关的几块（带页码，可回原文核对）。

设计取舍：
- **索引落项目内** ``<工作目录>/.skysheep/doc-index.sqlite3``：与 transcripts /
  reports 同属引擎在项目里的状态区，删掉即重建，不污染用户文件；不放进会话库
  是刻意的——索引可能有几千条向量块，塞进主库会让滚动备份（20 份）跟着膨胀。
  索引的**建立**是一次写盘动作，由 ``index_documents`` 工具走权限门确认；
  检索本身只读。
- **向量是可选增强**：本机有 Ollama（``tools/memory_embed`` 同一套探测与 HTTP）
  时给分块补嵌入，检索走 BM25 + 余弦的加权混合；没有 Ollama / 预算耗尽 /
  模型换了，索引自动按「仅关键词」工作（BM25 中文二元组照样能查），
  绝不因嵌入失败让检索不可用。
- **失效判定按 size + mtime**：检索前逐条比对，变了就标 stale 不参与检索并
  提示重建；不每次检索都重算 sha256（等于把文档重新解析一遍）。
- 所有函数是同步的（SQLite + 解析都是阻塞活），工具层统一 ``asyncio.to_thread``。
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from array import array
from pathlib import Path

from . import retrieval
from .textio import read_text_file

INDEX_DIR = ".skysheep"
INDEX_FILENAME = "doc-index.sqlite3"

# 支持索引的文本型文档（Office/PDF 走 extract_document_text，纯文本直接读）
DOC_SUFFIXES = (".pdf", ".docx", ".xlsx", ".pptx", ".md", ".txt", ".markdown", ".rst")

CHUNK_CHARS = 800          # 目标块大小：够一段完整论述，又不至于占满上下文
CHUNK_OVERLAP = 100        # 相邻块重叠：跨块边界的句子不会两边都残缺
# 过短的碎块（页眉残留）不进索引；但别卡太高——按页切分时「第 3 页：默认 3 次」
# 这种短页恰恰是答案所在，卡到 30 字符会把真答案丢掉（实测踩过）。
CHUNK_MIN_CHARS = 10
MAX_DOC_CHARS = 3_000_000  # 单文档上限：再大的更像数据文件，不适合问答
MAX_INDEX_FILES = 200      # 目录索引的文件数上限（防一次点选把整个盘扫进来）
MAX_EMBED_CHUNKS = 1500    # 单次建索引最多补嵌的块数（超出余下只走关键词）
EMBED_BUDGET_S = 90.0      # 建索引的嵌入墙钟预算（本地 Ollama 正常远低于它）
SNIPPET_CHARS = 1200       # 单块返回给模型的最大字符数

# 混合权重：BM25 保召回（中文二元组对术语命中很稳），余弦保「同义不同词」
HYBRID_BM25_WEIGHT = 0.5
HYBRID_VEC_WEIGHT = 0.5

_SCHEMA = """
CREATE TABLE IF NOT EXISTS docs (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    vec_model TEXT NOT NULL DEFAULT '',
    chunks INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    idx INTEGER NOT NULL,
    page INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    vec BLOB,
    UNIQUE(path, idx)
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
"""


def index_db_path(project: Path) -> Path:
    return Path(project) / INDEX_DIR / INDEX_FILENAME


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.executescript(_SCHEMA)
    return conn


def _vec_blob(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def _vec_from_blob(blob: bytes | None) -> list[float] | None:
    if not blob:
        return None
    try:
        return list(array("f", blob))
    except (ValueError, TypeError):
        return None


# ---- 分块 ----

_PAGE_MARK_RE = None


def _page_marker_re():
    global _PAGE_MARK_RE
    if _PAGE_MARK_RE is None:
        import re

        _PAGE_MARK_RE = re.compile(r"^--- 第 (\d+) 页 ---$", re.MULTILINE)
    return _PAGE_MARK_RE


def split_pages(text: str) -> list[tuple[int, str]]:
    """按 ``--- 第 N 页 ---`` 标记切页；没有标记（Word/Excel/纯文本）返回 [(0, text)]。

    PDF / PPT 的抽取器会插入该标记（见 tools/docs.py），这里只做解析，
    不依赖具体文件类型。
    """
    marks = list(_page_marker_re().finditer(text))
    if not marks:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    head = text[: marks[0].start()].strip()
    # 抽取器的元信息行（``[PDF 共 12 页]``）不是正文：长度很短且整行被方括号
    # 包住的直接丢，不当成第 0 页的一块（否则每次检索都多一条噪声命中）。
    if head and len(head) >= CHUNK_MIN_CHARS and not (head.startswith("[") and head.endswith("]")):
        out.append((0, head))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        body = text[m.end():end].strip()
        if body:
            out.append((int(m.group(1)), body))
    return out


def split_chunks(page_text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """把一页文本切成带重叠的块：优先在句末/换行处断开，避免半个句子开头。"""
    text = " ".join(page_text.split())
    if len(text) <= size:
        return [text] if len(text) >= CHUNK_MIN_CHARS else []
    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            lo = start + int(size * 0.6)
            cuts = [text.rfind(ch, lo, end) for ch in "。！？；\n.!?;"]
            cut = max(cuts)
            if cut > lo:
                end = cut + 1
        piece = text[start:end].strip()
        if len(piece) >= CHUNK_MIN_CHARS:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


def chunk_document(text: str) -> list[tuple[int, str]]:
    """文档文本 → [(页码, 块文本)]，无页标记时页码为 0。"""
    out: list[tuple[int, str]] = []
    for page, body in split_pages(text):
        for piece in split_chunks(body):
            out.append((page, piece))
    return out


def _extract_text(p: Path) -> str:
    if p.suffix.lower() in (".pdf", ".docx", ".xlsx", ".pptx"):
        from .tools.docs import extract_document_text

        return extract_document_text(p, max_chars=MAX_DOC_CHARS)
    return read_text_file(p).text[:MAX_DOC_CHARS]


def _iter_doc_files(root: Path) -> tuple[list[Path], list[str]]:
    """收集 root 下可索引的文档；返回 (文件列表, 跳过说明)。"""
    skipped: list[str] = []
    if root.is_file():
        if root.suffix.lower() in DOC_SUFFIXES:
            return [root], skipped
        return [], [f"{root.name}: 不支持的格式（支持 {' / '.join(DOC_SUFFIXES)}）"]
    files: list[Path] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or INDEX_DIR in p.parts:
            continue
        if p.suffix.lower() not in DOC_SUFFIXES:
            continue
        files.append(p)
    if len(files) > MAX_INDEX_FILES:
        skipped.extend(f.name for f in files[MAX_INDEX_FILES:])
        files = files[:MAX_INDEX_FILES]
        skipped.insert(0, f"文件数超过上限 {MAX_INDEX_FILES}，其余未索引")
    return files, skipped


# ---- 建索引 ----


def index_paths(
    project: Path, target: Path, *, force: bool = False,
    embed_budget_s: float = EMBED_BUDGET_S,
) -> dict:
    """对 target（文件或目录）建/更新索引；返回统计与逐文件说明。

    只接受工作目录内的文档：索引键是「相对工作目录的路径」，目录外的文件
    没有稳定键，也避免把别人的磁盘内容悄悄索引进这个项目。
    """
    project = Path(project).resolve()
    target = Path(target).resolve()
    try:
        target.relative_to(project)
    except ValueError:
        raise ValueError("只能索引当前工作目录内的文档") from None

    files, skipped = _iter_doc_files(target)
    if not files:
        return {"indexed": 0, "chunks": 0, "embedded": 0, "skipped": skipped}

    from .tools.memory_embed import embed_texts, embeddings_available

    db_path = index_db_path(project)
    conn = _connect(db_path)
    stats = {"indexed": 0, "chunks": 0, "embedded": 0, "skipped": skipped}
    deadline = time.monotonic() + max(0.0, embed_budget_s)
    embed_ok = embeddings_available(deadline=deadline)
    vec_model = ""
    try:
        from .tools.memory_embed import OLLAMA_EMBED_MODEL

        vec_model = OLLAMA_EMBED_MODEL if embed_ok else ""
    except Exception:  # noqa: BLE001 - 常量导入失败按无嵌入处理
        embed_ok = False

    try:
        for p in files:
            rel = p.relative_to(project).as_posix()
            # 符号链接可能指向工作目录外：只索引「解析后仍在项目内」的文件，
            # 否则一次 index_documents 等于把别人的磁盘内容批量读出并入库。
            try:
                p.resolve().relative_to(project)
            except (OSError, ValueError):
                skipped.append(f"{rel}: 指向工作目录外（符号链接），未索引")
                continue
            st = p.stat()
            row = conn.execute(
                "SELECT size, mtime, vec_model FROM docs WHERE path = ?", (rel,)
            ).fetchone()
            if (
                row is not None and not force
                and row[0] == st.st_size and abs(row[1] - st.st_mtime) < 1e-6
            ):
                continue  # 未变化：跳过（索引是增量的）
            if st.st_size > MAX_DOC_CHARS * 4:  # 粗筛：文本抽取后有上限，源文件先挡一道
                skipped.append(f"{rel}: 文件过大（{st.st_size / 1048576:.1f}MB）")
                continue
            try:
                text = _extract_text(p)
            except Exception as e:  # noqa: BLE001 - 单个文档失败不拖垮整批
                skipped.append(f"{rel}: 解析失败（{type(e).__name__}: {e}）")
                continue
            text = text[:MAX_DOC_CHARS]
            pieces = chunk_document(text)
            if not pieces:
                skipped.append(f"{rel}: 没有可索引的文本")
                continue
            digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel,))
            vecs: list[list[float] | None] = [None] * len(pieces)
            if embed_ok and len(pieces) <= MAX_EMBED_CHUNKS:
                got = embed_texts([t for _, t in pieces], deadline=deadline)
                if got is not None and len(got) == len(pieces):
                    vecs = list(got)
                    stats["embedded"] += len(got)
            conn.executemany(
                "INSERT INTO chunks(path, idx, page, text, vec) VALUES (?,?,?,?,?)",
                [
                    (rel, i, page, piece, _vec_blob(vecs[i]) if vecs[i] else None)
                    for i, (page, piece) in enumerate(pieces)
                ],
            )
            conn.execute(
                "INSERT OR REPLACE INTO docs(path, size, mtime, sha256, vec_model, chunks, indexed_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (rel, st.st_size, st.st_mtime, digest, vec_model, len(pieces), time.time()),
            )
            stats["indexed"] += 1
            stats["chunks"] += len(pieces)
        conn.commit()
    finally:
        conn.close()
    return stats


def _rel_filter(project: Path, target: Path | None) -> tuple[str | None, bool]:
    """(路径前缀, 是否精确匹配)。None 表示全库。"""
    if target is None:
        return None, False
    rel = Path(target).resolve().relative_to(Path(project).resolve()).as_posix()
    return rel, Path(target).is_file()


def _load_chunks(
    conn: sqlite3.Connection, prefix: str | None, exact: bool,
) -> list[tuple[str, int, int, str, bytes | None]]:
    if prefix is None:
        rows = conn.execute("SELECT path, idx, page, text, vec FROM chunks").fetchall()
    elif exact:
        rows = conn.execute(
            "SELECT path, idx, page, text, vec FROM chunks WHERE path = ? ORDER BY idx", (prefix,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT path, idx, page, text, vec FROM chunks WHERE path = ? "
            "OR path LIKE ? ORDER BY path, idx",
            (prefix, prefix.rstrip("/") + "/%"),
        ).fetchall()
    return rows


def search(
    project: Path, target: Path | None, query: str, top_k: int = 5,
) -> dict:
    """在已建索引上检索：返回 {results, stale, missing, keyword_only}。

    - results: [{path, page, idx, text, score}]，score 是归一化混合分（0~1）；
    - stale: 源文件已变更、索引过期的路径（不参与检索，提示重建）；
    - missing: 目标范围内完全没有索引的路径（提示先建索引）。
    """
    project = Path(project).resolve()
    # 先解析范围（越界 / 非法目标在这里就该拒绝），再判断索引是否存在：
    # 没有任何索引时也要能回答「你要找的范围还没建索引」。
    prefix, exact = _rel_filter(project, target)
    db_path = index_db_path(project)
    if not db_path.exists():
        return {
            "results": [], "stale": [],
            "missing": [prefix] if prefix else [], "keyword_only": True,
        }
    conn = _connect(db_path)
    try:
        docs = conn.execute("SELECT path, size, mtime, vec_model FROM docs").fetchall()
        stale: list[str] = []
        fresh: set[str] = set()
        vec_ok: set[str] = set()
        from .tools.memory_embed import OLLAMA_EMBED_MODEL

        for rel, size, mtime, vec_model in docs:
            try:
                st = (project / rel).stat()
            except OSError:
                stale.append(rel)
                continue
            if st.st_size != size or abs(st.st_mtime - mtime) >= 1e-6:
                stale.append(rel)
            else:
                fresh.add(rel)
                if vec_model == OLLAMA_EMBED_MODEL:
                    # 换过嵌入模型：旧向量维度/语义都不再可信，该文档退回纯 BM25
                    vec_ok.add(rel)
        rows = [
            r for r in _load_chunks(conn, prefix, exact)
            if r[0] in fresh
        ]
    finally:
        conn.close()

    if not rows:
        missing = [prefix] if prefix else []
        return {"results": [], "stale": stale, "missing": missing, "keyword_only": True}

    texts = [r[3] for r in rows]
    bm25 = retrieval.bm25_scores(query, texts)
    cos: list[float] | None = None
    chunk_vecs = [_vec_from_blob(r[4]) if r[0] in vec_ok else None for r in rows]
    if any(chunk_vecs):
        from .tools.memory_embed import cosine_scores_raw

        cos = cosine_scores_raw(chunk_vecs, query)
    if cos is not None and any(v is not None for v in cos):
        cos_filled = [c if c is not None else 0.0 for c in cos]
        merged = retrieval.merge_weighted(
            (retrieval.normalize(bm25), HYBRID_BM25_WEIGHT),
            (retrieval.normalize(cos_filled), HYBRID_VEC_WEIGHT),
        )
        keyword_only = False
    else:
        merged = retrieval.normalize(bm25)
        keyword_only = True

    picked = retrieval.top_indices(merged, max(1, top_k))
    results = []
    for i in picked:
        path, idx, page, text, _ = rows[i]
        results.append({
            "path": path,
            "page": page,
            "idx": idx,
            "text": text[:SNIPPET_CHARS],
            "score": round(merged[i], 4),
        })
    return {"results": results, "stale": stale, "missing": [], "keyword_only": keyword_only}


def index_summary(project: Path) -> dict:
    """索引概况（设置页/工具提示用）：文档数、块数、是否启用向量。"""
    db_path = index_db_path(project)
    if not db_path.exists():
        return {"docs": 0, "chunks": 0, "vec_model": ""}
    conn = _connect(db_path)
    try:
        docs = conn.execute("SELECT COUNT(*), COALESCE(SUM(chunks), 0) FROM docs").fetchone()
        models = {r[0] for r in conn.execute("SELECT DISTINCT vec_model FROM docs") if r[0]}
    finally:
        conn.close()
    return {
        "docs": int(docs[0] or 0),
        "chunks": int(docs[1] or 0),
        "vec_model": ",".join(sorted(models)),
    }
