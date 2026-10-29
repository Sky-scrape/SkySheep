"""文本文件的编码与行尾符安全处理。

背景（为什么需要这个模块）：中文 Windows 上大量旧文本文件是 GB18030/GBK 编码，
而此前的读写统一按 ``utf-8, errors="replace"`` 读、按 utf-8 覆盖写。后果是一条
静默的数据损坏路径：GBK 文件读出来是一串 U+FFFD 替换字符，模型以为文件就长这样，
一旦 edit_file 写回，原文被替换字符永久覆盖。行尾符同样无声变化——Python 文本模式
默认 ``newline=None``，写回时把 LF 换成 os.linesep（Windows 上 CRLF）。

本模块的约定：

- **读**：探测编码（BOM → utf-8 → gb18030，且做 round-trip 校验），返回
  「文本 + 编码 + 主行尾符」；文本一律归一成 LF 供内部处理，这样 edit_file 的
  old_string 匹配、diff、行号统计都只有一种形态。
- **写**：把 LF 还原成原主行尾符，用原编码落盘。探测不到确凿编码时
  ``certain=False``，调用方应拒绝写而不是带着替换字符覆盖。
- **binary**：含 NUL 字节且有没识别出 BOM 的，判为二进制，调用方另行处理
  （图片走 read_image，其余明确报错）。

只依赖标准库，tools/ 与 server/ 都可直接引用。
"""

from __future__ import annotations

import codecs
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# 顺序有讲究：UTF-32 的 BOM 以 UTF-16 的 BOM 开头，必须先匹配长的。
# 用 "utf-16" / "utf-32"（而非 -le/-be 变体）是为了让 codec 自己吃掉 BOM，
# 落盘时再按本机字节序写回，避免把 BOM 变成正文字符。
BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)

# 无 BOM 时的兜底顺序。utf-8 先试（ASCII 子集天然命中，新文件不被误判成别的编码），
# 再退到中文 Windows 的实际主流编码 gb18030（GBK / GB2312 的超集，且是完整
# Unicode 双向映射，round-trip 稳定）。
FALLBACK_ENCODINGS: tuple[str, ...] = ("utf-8", "gb18030")

DEFAULT_ENCODING = "utf-8"
DEFAULT_NEWLINE = "\n"


@dataclass
class TextFile:
    """解码结果。``text`` 已归一成 LF；``certain=False`` 表示编码靠猜。"""

    text: str
    encoding: str
    newline: str
    certain: bool
    binary: bool = False


def detect_newline(text: str) -> str:
    """取占主导的行尾符；没有任何换行时按 LF。"""
    crlf = text.count("\r\n")
    lone_cr = text.count("\r") - crlf
    lone_lf = text.count("\n") - crlf
    # crlf 用 >= ：纯 CRLF 文件里 lone_cr / lone_lf 都是 0，必须让 crlf 胜出
    if crlf and crlf >= lone_cr and crlf >= lone_lf:
        return "\r\n"
    if lone_cr > lone_lf:
        return "\r"
    return "\n"


def to_lf(text: str) -> str:
    """CRLF / CR → LF（内部统一形态）。"""
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


def from_lf(text: str, newline: str) -> str:
    """LF → 目标行尾符（写盘前还原）。"""
    if newline == "\n" or not text:
        return text
    return to_lf(text).replace("\n", newline)


def _looks_like_bomless_utf16(data: bytes) -> bool:
    """无 BOM UTF-16 的廉价形态特征（审计低危 L11）。

    GB18030 的「解码 → 再编码」对合法序列是双射，round-trip 甄别不了
    「其实是 UTF-16 却被 GB18030 解成功」：纯中文的 UTF-16LE 每个码元
    两字节都非零（CJK 基本区 U+4E00–U+9FFF 的高位字节在 0x4E–0x9F），
    不含 NUL 走不到二进制判定，会被以 certain=True 当成 GB18030 交出——
    模型看到乱码还被当作确凿编码，edit 按乱码基准改写。两条廉价特征：

    - 零字节占比显著：UTF-16 的 ASCII 码元高字节为 0（含换行/空格的
      文本都命中）。注意调用位置在 NUL 二进制判定之后，这类数据早已
      被接走，此条在当前调用点是休眠保险，保留以让特征自洽；
    - 码元高字节带：纯 CJK 的 UTF-16 无任何零字节，但码元高字节集中
      在 0x4E–0x9F（LE 在奇数位、BE 在偶数位）；GB2312 汉字的第二
      字节惯例在 0xA1–0xFE，两段不重叠，≥75% 即判为 UTF-16 形态。
      误伤面：数据还必须**不是**合法 UTF-8 且恰能构成合法 GB18030
      序列——GB2312 汉字第二字节恒 ≥0xA1，不会落进判定带。

    命中后由调用方把编码降为「不确定」（certain=False）：读仍以替换
    字符展示，写被拒绝——宁可拒绝也不能按乱码基准改写。
    """
    if len(data) < 8 or len(data) % 2 != 0:
        return False
    if data.count(b"\x00") * 8 >= len(data):  # 零字节占比 ≥ 1/8
        return True
    for high in (data[1::2], data[0::2]):  # LE 高位在奇数位，BE 在偶数位
        band = sum(1 for b in high if 0x4E <= b < 0xA0)
        if band * 4 >= len(high) * 3:  # ≥ 75% 落在 CJK 高字节带
            return True
    return False


def sniff(data: bytes) -> tuple[str | None, bool, bool]:
    """探测字节串的编码。

    返回 ``(encoding, certain, binary)``：

    - 命中 BOM 时直接采信，``certain=True``；
    - 含 NUL 又没有 BOM，判为二进制（``encoding=None``）；
    - 否则按 utf-8 → gb18030 逐个严格解码，并用 **round-trip 校验**
      （``text.encode(enc) == data``）确认，避免 GB18030 把别的编码
      「解成功」却对不上原文；
    - gb18030 命中前先过无 BOM UTF-16 形态甄别（``_looks_like_bomless_utf16``）：
      GB18030 对它是双射误配，round-trip 甄别不了，降为 ``(None, False, False)``
      走「拒绝写回」路径而不是交出 certain=True 的乱码基准；
    - 全部失败时 ``(None, False, False)``，调用方按替换字符展示并拒绝写回。
    """
    for bom, enc in BOM_ENCODINGS:
        if data.startswith(bom):
            return enc, True, False
    if b"\x00" in data:
        return None, False, True
    if not data:
        return DEFAULT_ENCODING, True, False
    for enc in FALLBACK_ENCODINGS:
        if enc == "gb18030" and _looks_like_bomless_utf16(data):
            return None, False, False
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        if text.encode(enc) == data:
            return enc, True, False
    return None, False, False


def decode_bytes(data: bytes) -> TextFile:
    """字节 → TextFile。文本一律归一成 LF。"""
    encoding, certain, binary = sniff(data)
    if binary:
        return TextFile("", DEFAULT_ENCODING, DEFAULT_NEWLINE, False, binary=True)
    if encoding is None:
        # 解不出来：用替换字符展示（调用方据此拒绝写回），不抛异常——
        # 用户至少有东西可看，而不是一句「读不了」。
        return TextFile(to_lf(data.decode("utf-8", errors="replace")), DEFAULT_ENCODING,
                        DEFAULT_NEWLINE, False)
    try:
        raw = data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return TextFile(to_lf(data.decode("utf-8", errors="replace")), DEFAULT_ENCODING,
                        DEFAULT_NEWLINE, False)
    return TextFile(to_lf(raw), encoding, detect_newline(raw), True)


def read_text_file(path: Path) -> TextFile:
    """读文件并探测编码 / 行尾符。可能抛 OSError。"""
    return decode_bytes(path.read_bytes())


def encode_text(text: str, encoding: str, newline: str) -> bytes:
    """按目标编码与行尾符编码文本（先归一到 LF 再换行，避免 \\r\\n 被重复处理）。"""
    prepared = from_lf(to_lf(text), newline)
    return prepared.encode(encoding)


def _fsync_before_replace(f, sync: bool) -> None:
    """rename 前把数据钉到盘上（数据安全审查：断电时 rename 先于数据落盘）。

    close() 只保证数据进了 OS 页缓存；没有 fsync 时 os.replace 的元数据提交
    可能先于数据落盘（NTFS write-behind / ext4 delayed allocation 都会出现），
    断电后留下空文件或旧内容。可丢失的文件（desktop.pid、窗口几何）传
    sync=False 跳过，省一次磁盘同步。
    """
    if not sync:
        return
    f.flush()
    os.fsync(f.fileno())


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8", *, sync: bool = True) -> None:
    """原子写「引擎自己的」状态文件（config.toml / 任务簿 / mcp.json / ui.json）。

    与 write_text_file 的分工：那个面向用户文件（要保留原编码与行尾符），这个
    面向引擎自有的固定 UTF-8 状态文件。共同点是「先写同目录临时文件、再
    os.replace」：直接覆盖写在写一半时的中间态会暴露给并发读者，进程被杀还会
    留下半个文件——对 JSON 而言就是整个配置/任务簿读不出来（安全审查 M13）。
    同目录保证同一卷，rename 才是原子语义。

    sync=True（默认）在 rename 前 flush + fsync，断电不丢内容；丢了也不碍事
    的文件（desktop.pid、窗口几何）传 sync=False。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode(encoding))
            _fsync_before_replace(f, sync)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_bytes_atomic(path: Path, data: bytes, *, sync: bool = True) -> None:
    """原子写字节内容（检查点回滚、图片落盘这类二进制写用）。

    与 write_text_atomic 同款：先写同目录临时文件再 os.replace，写一半被
    中断时目标仍是旧内容（安全审查低危项：检查点 restore 此前是直接
    write_bytes，回滚到一半崩溃会留下半截文件）。sync 语义同上，默认在
    rename 前 fsync 落盘。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            _fsync_before_replace(f, sync)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _posix_keep_mode(path: Path) -> int | None:
    """POSIX：覆盖已有文件时返回要保留的原权限位；新建文件返回 None。

    新建不再还原「普通 umask 语义」——旧实现用 ``os.umask(0o022) 再
    恢复`` 探测 umask，而 umask 是进程全局属性，探测窗口内其他线程
    新建的文件会拿到错误权限位（审计低危 L12）。改为不探测：新建文件
    保持 mkstemp 建出的 0600，宁可严格私有。Windows 走目录 ACL 继承，
    不调用本函数。
    """
    try:
        return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None


def write_text_file(path: Path, text: str, encoding: str, newline: str) -> None:
    """按目标编码与行尾符落盘（先建父目录）。可能抛 OSError / UnicodeEncodeError。

    原子写：先写同目录临时文件，再 os.replace 覆盖目标。直接覆盖写在
    「写了一半」的中间态会暴露给并发的读者（别的会话/任务的工具、检查点
    回滚、编辑器自动重载），进程被杀时还会留下半个文件；同目录保证同一卷，
    rename 才是原子语义——要么旧内容，要么新内容。临时文件名唯一，两个
    并行写同一目标时各自落各自的临时文件，不会互相覆盖半成品。

    rename 前 flush + fsync（与 write_text_atomic 同一标准）：close() 只把
    数据送进 OS 页缓存，没有 fsync 时 os.replace 的元数据提交可能先于数据
    落盘，断电后留下空文件或旧内容。调用面全是用户编辑的文本文件，量级小，
    不提供跳过开关。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode_text(text, encoding, newline)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            _fsync_before_replace(f, sync=True)
        if os.name != "nt":
            # POSIX：覆盖已有文件时保留原权限位；新建文件保持 mkstemp 的
            # 0600（不做 umask 探测，见 _posix_keep_mode）。
            # Windows 走目录 ACL 继承，不需要处理。
            mode = _posix_keep_mode(path)
            if mode is not None:
                os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---- 启动清扫：硬杀/断电留下的原子写临时文件 ----
#
# 上面三处原子写（write_text_file / write_text_atomic / write_bytes_atomic）
# 的临时文件只在 except BaseException 里清理——进程被 taskkill /F 或断电时
# 走不到，盘面会残留 `名字.xxxxxxxx.tmp`（验证实测：杀进程五轮两类路径都中）。
# 残留无任何机制回收，长期堆在数据目录里。mkstemp 的命名规则是固定的
# （prefix + 8 个 [a-z0-9_] 随机字符 + ".tmp"），引擎数据目录内可以据此
# 严格识别并回收；用户项目目录不扫——用户自己的构建/编辑器也会产生 .tmp，
# 与 textio 的残留无法可靠区分，宁可不动（该取舍由调用方保证：清扫只对
# 引擎数据目录 ~/.skysheep 调用，见 SessionStore.connect）。

# tempfile._RandomNameSequence 的字符集（小写字母 + 数字 + 下划线，固定 8 位）
_TMP_RANDOM_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def is_textio_tmp_name(name: str) -> bool:
    """文件名是否符合 textio 原子写临时文件的 mkstemp 命名规则。

    只认「.tmp 结尾、去尾后最后一段恰为 8 个 [a-z0-9_] 字符」的名字：
    用户自己叫 ``笔记.tmp``、``backup.bak2024.tmp`` 的文件都不会命中，
    避免误删。
    """
    if not name.endswith(".tmp") or len(name) <= 4 + 9:
        return False
    head, sep, tail = name[:-4].rpartition(".")
    if not sep or len(tail) != 8:
        return False
    return all(c in _TMP_RANDOM_CHARS for c in tail)


def sweep_stale_tmp_files(home: Path, *, max_age_s: float = 60.0) -> list[str]:
    """清扫引擎数据目录内 textio 原子写残留的 .tmp 临时文件，返回已删路径。

    只扫传入的 home（引擎数据目录）内递归；**用户项目目录不扫**——
    write_text_file 的临时文件落在用户文件旁边，与用户自己的 .tmp 无法
    区分，误删用户文件比残留几个临时文件严重得多。

    max_age_s 只删足够老的残留：启动瞬间其他组件可能正有一笔原子写在途，
    误删会让随后的 os.replace 落空；60 秒内的临时文件留给下次启动处理。
    home 不存在（首次启动）时直接返回空。
    """
    removed: list[str] = []
    try:
        candidates = list(home.rglob("*.tmp"))
    except OSError:
        return removed
    now = time.time()
    for p in candidates:
        try:
            if p.is_symlink() or not p.is_file():
                continue
            if not is_textio_tmp_name(p.name):
                continue
            if now - p.stat().st_mtime < max_age_s:
                continue
            p.unlink()
            removed.append(str(p))
        except OSError:
            continue  # 单个文件删不掉（被占用等）：留给下次启动
    return removed
