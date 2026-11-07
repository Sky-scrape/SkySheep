"""config init 模板写入：走原子写，不留半截 TOML，无临时文件残留。"""

from __future__ import annotations

import tomllib

import pytest

from skysheep.config import CONFIG_TEMPLATE, ConfigError, config_path, write_config_template


def test_write_config_template_creates_parseable_config(home):
    """首次写入：父目录自动创建、内容与模板一致且是合法 TOML、无 .tmp 残留。"""
    p = write_config_template()
    assert p == config_path()
    assert p.exists()
    # write_text_atomic 自带建父目录（config.py 不再显式 mkdir）
    with p.open("rb") as f:
        parsed = tomllib.load(f)
    assert isinstance(parsed, dict)
    assert parsed.get("providers")
    assert p.read_text(encoding="utf-8") == CONFIG_TEMPLATE
    # 原子写的临时文件用完即清：同目录不留 config.toml.*.tmp
    assert list(p.parent.glob(p.name + ".*")) == []


def test_write_config_template_refuses_existing_without_force(home):
    """已存在且未指定 force → ConfigError，且不动原文件。"""
    p = write_config_template()
    before = p.read_text(encoding="utf-8")
    with pytest.raises(ConfigError):
        write_config_template()
    assert p.read_text(encoding="utf-8") == before


def test_write_config_template_force_overwrites(home):
    """force=True 覆盖已有配置，仍走原子写（无临时文件残留）。"""
    p = write_config_template()
    p.write_text("# 手改过的配置\n", encoding="utf-8")
    write_config_template(force=True)
    assert p.read_text(encoding="utf-8") == CONFIG_TEMPLATE
    assert list(p.parent.glob(p.name + ".*")) == []
