"""场景模板清单：随引擎打包的官方技能目录（只读数据）。

「场景模板中心」的数据源是 ``gallery_manifest.json``——skills-gallery/ 官方
种子技能包的离线快照（结构与仓库根的 manifest.json 相同：dir / name /
description / source）。与已下线的技能广场不同：这里没有任何远端索引与
在线检索，读的是打包内文件；安装官方来源时优先用打包内技能副本
（bundled_dir_for，PyInstaller datas 随包分发），GitHub 链接只作包内缺失时
的更新回退——装一个模板在线要直连下载整仓归档且无缓存，中文网络环境经常
失败。清单读取失败返回空列表，调用方自行回落。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..textio import read_text_file

# 打包内清单位置：与本模块同目录，随 wheel / PyInstaller 一起分发
MANIFEST_PATH = Path(__file__).parent / "gallery_manifest.json"

# 打包内技能本体目录：SkySheep.spec 从仓库根 skills-gallery/ 逐文件收进来，
# 落位与本模块同目录的 gallery/<dir>/。源码/wheel 运行时该目录不存在，
# bundled_dir_for 返回 None → 安装回落在线下载，行为与旧版一致。
GALLERY_BUNDLE_DIR = Path(__file__).parent / "gallery"


def load_gallery_manifest() -> list[dict]:
    """读打包内的场景模板清单，返回条目数组（读取/解析失败返回空列表）。

    编码探测走 textio（打包文件恒为 UTF-8，但统一入口避免绕过编码防线）；
    单条缺 name 的坏条目直接丢弃，不让一条脏数据拖垮整个模板区。
    """
    try:
        tf = read_text_file(MANIFEST_PATH)
    except OSError:
        return []
    try:
        data = json.loads(tf.text)
    except ValueError:
        return []
    skills = data.get("skills") if isinstance(data, dict) else None
    if not isinstance(skills, list):
        return []
    return [s for s in skills if isinstance(s, dict) and str(s.get("name", "")).strip()]


def bundled_dir_for(source: str) -> Path | None:
    """官方场景模板的打包内副本目录；不可用返回 None（安装回落在线下载）。

    按清单条目匹配：source 网址精确相等（容忍尾斜杠）、网址路径以
    ``/skills-gallery/<dir>`` 结尾、或 source 就是目录名。命中后副本目录必须
    真实存在且含 SKILL.md——坏副本当作没有，宁走网络也不装残包。
    """
    src = str(source or "").strip().rstrip("/")
    if not src:
        return None
    for entry in load_gallery_manifest():
        dir_name = str(entry.get("dir", "")).strip()
        if not dir_name:
            continue
        entry_source = str(entry.get("source", "")).strip().rstrip("/")
        if src not in (entry_source, dir_name) and not src.endswith(
            f"/skills-gallery/{dir_name}"
        ):
            continue
        candidate = GALLERY_BUNDLE_DIR / dir_name
        if (candidate / "SKILL.md").is_file():
            return candidate
        return None  # 清单命中但包内没有/不完整：直接回落在线下载
    return None
