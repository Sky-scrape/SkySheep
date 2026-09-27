"""场景模板清单：随引擎打包的官方技能目录（只读数据）。

「场景模板中心」的数据源是 ``gallery_manifest.json``——skills-gallery/ 官方
种子技能包的离线快照（结构与仓库根的 manifest.json 相同：dir / name /
description / source）。与已下线的技能广场不同：这里没有任何远端索引与
在线检索，读的是打包内文件，安装仍复用既有 skills.install（source 是
GitHub 链接时走链接导入流程）。清单读取失败返回空列表，调用方自行回落。
"""

from __future__ import annotations

import json
from pathlib import Path

from ..textio import read_text_file

# 打包内清单位置：与本模块同目录，随 wheel / PyInstaller 一起分发
MANIFEST_PATH = Path(__file__).parent / "gallery_manifest.json"


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
