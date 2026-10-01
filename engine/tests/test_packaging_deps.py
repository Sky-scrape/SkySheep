"""打包链路依赖入册回归：PyInstaller 必须在 pyproject dev 组与 uv.lock 里。

旧坑：PyInstaller 不入册，`uv run`（跑测试等）会按 lock 把它未入册的传递
依赖（如 altgraph）修剪掉，打包突然起不来、只在发布当天暴露，靠发布清单
人肉 `uv pip install` 补装兜底。入册 dev 组后由 uv.lock 锁定；这两条断言
防止它将来再被移出册子。
"""

import tomllib
from pathlib import Path

_ENGINE = Path(__file__).resolve().parents[1]


def test_pyinstaller_in_dev_group():
    """pyproject 的 dev 依赖组必须包含 pyinstaller。"""
    pyproject = tomllib.loads((_ENGINE / "pyproject.toml").read_text(encoding="utf-8"))
    dev = pyproject["dependency-groups"]["dev"]
    assert any(
        str(dep).strip().lower().replace("_", "-").startswith("pyinstaller")
        for dep in dev
    ), f"dev 组缺 pyinstaller，uv run 会修剪它导致打包链路断：{dev}"


def test_pyinstaller_locked_in_uv_lock():
    """uv.lock 必须锁住 pyinstaller（含传递依赖），否则 uv sync --locked 装不出完整环境。"""
    lock = tomllib.loads((_ENGINE / "uv.lock").read_text(encoding="utf-8"))
    names = {p["name"] for p in lock.get("package", [])}
    assert "pyinstaller" in names
    # pyinstaller 自己声明的关键传递依赖也要在册（altgraph 是当年被修剪断链的典型）
    assert "altgraph" in names
