"""桌面截屏与网页截图共用的副本保存与容量淘汰逻辑。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

# 截图副本保留上限（~/.skysheep/screenshots）：只保留最近若干张，防止无声涨满磁盘。
KEEP_SCREENSHOTS = 60


def _prune_screenshots(shots: Path) -> None:
    """截图副本保留策略：超过 KEEP_SCREENSHOTS 时删最旧的。

    只处理 .png 文件（不动用户自己丢进来的其它东西），删除失败静默——
    清目录是附带的维护动作，不该让截图本身报错。
    """
    try:
        files = [f for f in shots.glob("*.png") if f.is_file()]
    except OSError:
        return
    if len(files) <= KEEP_SCREENSHOTS:
        return
    try:
        files.sort(key=lambda f: f.stat().st_mtime)
    except OSError:
        return
    for old in files[: len(files) - KEEP_SCREENSHOTS]:
        try:
            old.unlink()
        except OSError:
            pass


def save_screenshot_copy(shots: Path, png: bytes, *, prefix: str = "") -> Path | None:
    """把 PNG 字节落盘到截图目录并按上限裁剪旧副本；失败返回 None 不阻断主流程。"""
    path = shots / f"{prefix}{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
    try:
        shots.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png)
        _prune_screenshots(shots)
        return path
    except OSError:
        return None
