"""主题登记对齐：以 app-themes.js 的 THEMES 表为唯一真源，机械校验 app.css 列出的七处登记。

app.css 主题段开头的「新增主题同步清单」点名七处（漏一处就表现为「选了不生效 /
标题栏不跟 / 首帧闪白」），本用例逐处比对；期望值一律从 THEMES 表与它的 dark
布尔派生，不再手抄名单：

  ① static/app.css            [data-theme="id"] 完整变量组（+ ::selection）
  ② static/index.html         首帧兜底底色、设置页主题卡、两个「跟随系统」下拉
  ③ static/app-themes.js      THEMES 表（本用例的真源）
  ④ backend_parts/remote.py   THEME_PREFS / THEME_AUTO_LIGHT / THEME_AUTO_DARK
  ⑤ server/app.py             首帧注入（THEME_IDS 由 THEME_PREFS 派生，不另立名单）
  ⑥ wintheme.THEME_PALETTE / DARK_THEME_IDS；bootpages._ERR
  ⑦ tests                     名单改从各自真源派生（test_ui_prefs / test_wintheme）

漏一处都能通过语法检查，所以只能靠集合相等断言拦；失败信息直接指名缺哪处、
缺哪个 id。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"
REMOTE_PY = ROOT / "src" / "skysheep" / "server" / "backend_parts" / "remote.py"

# THEME_PREFS 除主题 id 外还承载的非主题值：auto=跟随系统、timed=定时切换、
# light/dark=旧版两档（读取时映射回 paper/night）
_NON_THEME_PREFS = {"auto", "timed", "light", "dark"}


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _themes_from_app_js() -> dict[str, bool]:
    """app-themes.js 顶层 `const THEMES = {...}`：主题 id → 是否深色（唯一事实来源）。

    dark 布尔同时决定「跟随系统」的深浅落点、标题栏色板与首帧兜底，必须和 id
    一一对上，所以这里连值一起解析，后面的深浅两族期望值都从它派生。
    （表已于第 4 批出仓到 app-themes.js；函数名沿用出仓前叫法，见设计 §B3 将来
    生成器 recipe 对本名的引用。）
    """
    js = read_static("app-themes.js")
    m = re.search(r"^const THEMES = \{(.*?)^\};", js, re.M | re.S)
    assert m, "app-themes.js 必须保留顶层 `const THEMES = {...}` 字面量（主题名单的唯一事实来源）"
    themes: dict[str, bool] = {}
    # 顶层键缩进两格，值形如 { name: "纸墨", dark: false }
    for name, body in re.findall(
        r"^\s{2}([A-Za-z][\w-]*):\s*\{(.*?)\},?\s*$", m.group(1), re.M
    ):
        dark = re.search(r"\bdark:\s*(true|false)", body)
        assert dark, f"THEMES.{name} 缺少 dark 布尔（深浅落点与标题栏色板都靠它）"
        themes[name] = dark.group(1) == "true"
    return themes


def _theme_ids_in_app_css() -> set[str]:
    """app.css 里带 `--bg` 的变量组声明的主题 id。

    先剥注释再匹配：段首说明里有 `[data-theme="id"]` 这种模板写法，不剥会把
    假 id 当成分组；纸墨是 `:root, [data-theme="paper"]` 逗号组合选择器，所以取
    选择器串里出现的全部 [data-theme=…]，不要求独占一行。
    """
    css = re.sub(r"/\*.*?\*/", "", read_static("app.css"), flags=re.S)
    found: set[str] = set()
    for rule in re.finditer(r"(?P<sel>[^{}]*)\{(?P<body>[^{}]*)\}", css):
        if "--bg:" not in rule.group("body"):
            continue
        found.update(re.findall(r'\[data-theme="([^"]+)"\]', rule.group("sel")))
    return found


def _theme_ids_in_index_fallback() -> set[str]:
    """index.html <head> 首帧兜底底色表里的主题 id（纸墨由裸 html 规则承担，不在表里）。"""
    html = read_static("index.html")
    return set(re.findall(r'html\[data-theme="([^"]+)"\]\s*\{\s*background:', html))


def _theme_ids_in_index_cards() -> set[str]:
    """设置页主题卡（`class="theme-card"` 的按钮）的 data-theme-pref。

    auto / timed 是两种模式卡，不是主题 id，比对前剔除；卡片的属性顺序不作
    要求（按整张 button 标签找值，不锚固定位置）。
    """
    html = read_static("index.html")
    found: set[str] = set()
    for tag in re.findall(r"<button\b[^>]*>", html, re.I):
        if "theme-card" not in tag:
            continue
        m = re.search(r'data-theme-pref="([^"]+)"', tag)
        if m:
            found.add(m.group(1))
    return found - {"auto", "timed"}


def _theme_ids_in_auto_select(select_id: str) -> set[str]:
    """「跟随系统时」浅色 / 深色下拉里 option 的 value 集合。"""
    html = read_static("index.html")
    m = re.search(
        rf'<select[^>]*\bid="{re.escape(select_id)}"[^>]*>(.*?)</select>',
        html,
        re.S,
    )
    assert m, f"index.html 找不到「跟随系统」下拉 #{select_id}"
    return set(re.findall(r'<option[^>]*\bvalue="([^"]+)"', m.group(1)))


def _remote_tuple_from_source(name: str) -> tuple[str, ...]:
    """按源码解析 backend_parts/remote.py 的模块级元组常量（不 import，避免拖起重依赖）。"""
    tree = ast.parse(REMOTE_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, (tuple, list)), f"{name} 必须是字符串元组"
            return tuple(value)
    raise AssertionError(f"backend_parts/remote.py 里找不到模块级 {name} 赋值")


def _registered_sets(
    themes: dict[str, bool],
) -> list[tuple[str, set[str], set[str]]]:
    """(登记处, 该处声明的主题 id 集合, 按 THEMES 派生的期望集合)。

    各处允许的额外项已就地归并：THEME_PREFS 去掉 auto/timed/light/dark 四个
    非主题值；首帧兜底表补上由裸 html 规则承担的纸墨。
    """
    from skysheep import bootpages, wintheme

    all_ids = set(themes)
    light_ids = {theme_id for theme_id, dark in themes.items() if not dark}
    dark_ids = {theme_id for theme_id, dark in themes.items() if dark}
    return [
        ("app.css 的 [data-theme] 变量组", _theme_ids_in_app_css(), all_ids),
        ("wintheme.THEME_PALETTE", set(wintheme.THEME_PALETTE), all_ids),
        ("bootpages._ERR", set(bootpages._ERR), all_ids),
        ("backend_parts/remote.py 的 THEME_PREFS",
         set(_remote_tuple_from_source("THEME_PREFS")) - _NON_THEME_PREFS, all_ids),
        ("index.html 首帧兜底底色表", _theme_ids_in_index_fallback() | {"paper"}, all_ids),
        ("index.html 设置页主题卡", _theme_ids_in_index_cards(), all_ids),
        ("backend_parts/remote.py 的 THEME_AUTO_LIGHT",
         set(_remote_tuple_from_source("THEME_AUTO_LIGHT")), light_ids),
        ("backend_parts/remote.py 的 THEME_AUTO_DARK",
         set(_remote_tuple_from_source("THEME_AUTO_DARK")), dark_ids),
        ("index.html「跟随系统」浅色下拉",
         _theme_ids_in_auto_select("theme-auto-light"), light_ids),
        ("index.html「跟随系统」深色下拉",
         _theme_ids_in_auto_select("theme-auto-dark"), dark_ids),
    ]


_THEMES = _themes_from_app_js()
_REGISTRIES = _registered_sets(_THEMES)


@pytest.mark.parametrize("where, registered, expected", _REGISTRIES,
                         ids=[where for where, _, _ in _REGISTRIES])
def test_theme_ids_registered_everywhere(where: str, registered: set[str],
                                         expected: set[str]):
    """THEMES 表里的每个 id 都必须在 `where` 处登记，且该处不许留已下线的死 id。"""
    assert _THEMES, "app-themes.js 的 THEMES 表不能为空"
    missing = sorted(expected - registered)
    extra = sorted(registered - expected)
    detail = []
    if missing:
        detail.append("缺少：" + "、".join(missing))
    if extra:
        detail.append("多出（THEMES 里没有的死 id）：" + "、".join(extra))
    assert not detail, (
        f"{where} 与 app-themes.js 的 THEMES 表不一致——\n  " + "\n  ".join(detail)
        + f"\n主题名单以 app-themes.js 的 THEMES 为准（当前 {len(_THEMES)} 套），"
        "加/删主题时把 app.css 主题段开头那七处一起改。"
    )


def test_dark_theme_ids_match_theme_table():
    """wintheme.DARK_THEME_IDS 必须与 THEMES 的 dark 布尔逐项一致。

    它决定标题栏、启动页与首帧注入的深浅判定；手写集合漂移时新深色主题会被
    当成浅色（标题栏不跟、首帧闪一下），而那几处渲染都不经过 Python 断言。
    只允许保留旧版 `dark` 别名（theme_is_dark 兼容旧 ui.json 用）。
    """
    from skysheep import wintheme

    expected = {theme_id for theme_id, dark in _THEMES.items() if dark}
    assert expected <= wintheme.DARK_THEME_IDS, (
        "DARK_THEME_IDS 漏了 THEMES 里的深色主题："
        + "、".join(sorted(expected - wintheme.DARK_THEME_IDS))
    )
    extra = wintheme.DARK_THEME_IDS - expected
    assert extra == {"dark"}, (
        "DARK_THEME_IDS 多出 THEMES 里没有的 id（只允许旧版 dark 别名）："
        + "、".join(sorted(extra))
    )


def test_themes_table_lives_in_app_themes_js():
    """THEMES 表出仓（第 4 批）后的落位不变量：真源恰在 app-themes.js 一处，app.js 不留副本。

    app-themes.js 先于 app.js 加载（before_app）；app.js 若「顺手抄一份」副本，
    两处名单必然分叉（主题卡名单与命令面板动作都从全局 THEMES 取），改动真源
    时副本静默过期——出现副本就直接拦下。
    """
    assert "const THEMES = {" in read_static("app-themes.js")
    assert "const THEMES = {" not in read_static("app.js"), (
        "THEMES 真源已拆至 app-themes.js，app.js 的副本会分叉名单（改 app-themes.js）")
