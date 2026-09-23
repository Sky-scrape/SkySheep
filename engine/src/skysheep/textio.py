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


def sniff(data: bytes) -> tuple[str | None, bool, bool]:
    """探测字节串的编码。

    返回 ``(encoding, certain, binary)``：

    - 命中 BOM 时直接采信，``certain=True``；
    - 含 NUL 又没有 BOM，判为二进制（``encoding=None``）；
    - 否则按 utf-8 → gb18030 逐个严格解码，并用 **round-trip 校验**
      （``text.encode(enc) == data``）确认，避免 GB18030 把别的编码
      「解成功」却对不上原文；
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


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    """原子写「引擎自己的」状态文件（config.toml / 任务簿 / mcp.json / ui.json）。

    与 write_text_file 的分工：那个面向用户文件（要保留原编码与行尾符），这个
    面向引擎自有的固定 UTF-8 状态文件。共同点是「先写同目录临时文件、再
    os.replace」：直接覆盖写在写一半时的中间态会暴露给并发读者，进程被杀还会
    留下半个文件——对 JSON 而言就是整个配置/任务簿读不出来（安全审查 M13）。
    同目录保证同一卷，rename 才是原子语义。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(text.encode(encoding))
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """原子写字节内容（检查点回滚、图片落盘这类二进制写用）。

    与 write_text_atomic 同款：先写同目录临时文件再 os.replace，写一半被
    中断时目标仍是旧内容（安全审查低危项：检查点 restore 此前是直接
    write_bytes，回滚到一半崩溃会留下半截文件）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_text_file(path: Path, text: str, encoding: str, newline: str) -> None:
    """按目标编码与行尾符落盘（先建父目录）。可能抛 OSError / UnicodeEncodeError。

    原子写：先写同目录临时文件，再 os.replace 覆盖目标。直接覆盖写在
    「写了一半」的中间态会暴露给并发的读者（别的会话/任务的工具、检查点
    回滚、编辑器自动重载），进程被杀时还会留下半个文件；同目录保证同一卷，
    rename 才是原子语义——要么旧内容，要么新内容。临时文件名唯一，两个
    并行写同一目标时各自落各自的临时文件，不会互相覆盖半成品。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = encode_text(text, encoding, newline)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if os.name != "nt":
            # POSIX：mkstemp 建出的文件是 0600，替换后会带着这个权限位。
            # 覆盖已有文件时保留原权限位；新建文件还原成普通 umask 语义。
            # Windows 走目录 ACL 继承，不需要处理。
            try:
                mode = stat.S_IMODE(path.stat().st_mode)
            except OSError:
                umask = os.umask(0o022)
                os.umask(umask)
                mode = 0o666 & ~umask
            os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
