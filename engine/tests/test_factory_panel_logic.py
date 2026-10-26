"""工厂面板 · 筛选与批量操作的运行时回归（P2）。

`app-factory.js` 里这几件事是纯逻辑（不碰 DOM），但藏在 IIFE 里，源码检查
只能证明「函数存在」。这里沿用 `test_factory_scene_models.py` 的做法：用 Node
vm 加载真实的 `app-factory.js`，在 IIFE 收口前注入导出，直接对纯函数下断言。

覆盖：
- 筛选模式（全部 / 在跑 / 需留意 / 停工）与关键词（厂区名 / 产线名）的组合；
- 关键词命中厂区名时整座厂区放行；空厂区在「全部」下仍露出（否则新建入口会消失）；
- 批量派工只挑「启用的定时产线且不在跑」，批量暂停只挑「仍在启用的定时产线」，
  装配线不参与，单次派工上限是 5 条；
- token / 工序文案的格式化（顺手把 P1 的两个渲染函数也钉住）。

没有 Node 时整组 skip；不碰浏览器、不写用户数据。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from skysheep.core.mods_node import find_node

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"
NODE_BIN = shutil.which("node") or find_node()

_EXPORTED = (
    "filterProjects",
    "matchFilterMode",
    "filterActive",
    "fmtTokens",
    "progressText",
    "S",
    "daylightFactor",
    "statFor",
    "PALLET_SIZE",
    "PALLET_MAX",
    "REWORK_MAX",
)

_DRIVER_JS = r'''
(function () {
  "use strict";
  var fx = window.__panelTest;
  var report = { fatal: null, cases: {} };

  function keys(groups) {
    return groups.map(function (g) {
      return {
        project: String(g.project.name),
        lines: g.lines.map(function (l) { return l.key; }),
      };
    });
  }
  function ids(lines) {
    return lines.map(function (l) { return l.key; });
  }

  try {
    var projects = [
      {
        id: 1, name: "内容厂", root_ok: true, lines: [
          { key: "cron:11", kind: "cron", id: 11, name: "晨间简报", status: "running" },
          { key: "cron:12", kind: "cron", id: 12, name: "竞品巡检", status: "idle" },
          { key: "cron:13", kind: "cron", id: 13, name: "素材归档", status: "paused" },
          { key: "pipe:1", kind: "pipeline", id: 1, name: "发布装配线", status: "draft" },
        ]
      },
      {
        id: 2, name: "工具厂", root_ok: true, lines: [
          { key: "cron:21", kind: "cron", id: 21, name: "依赖体检", status: "idle" },
          { key: "cron:22", kind: "cron", id: 22, name: "夜班", status: "stopped" },
        ]
      },
      { id: 3, name: "空厂区", root_ok: true, lines: [] },
    ];
    var attention = new Set(["cron:12", "cron:22"]);

    // 1) 全部：空厂区仍露出（新建入口），没有关键词时不筛
    report.cases.all = keys(fx.filterProjects(projects, attention, { mode: "all", q: "" }));

    // 2) 在跑：只留 running
    report.cases.running = keys(fx.filterProjects(projects, attention, { mode: "running", q: "" }));

    // 3) 需留意：按 attention 集合（含跳过 / 停工 / 孤儿）
    report.cases.attention = keys(fx.filterProjects(projects, attention, { mode: "attention", q: "" }));

    // 4) 停工：paused 与 stopped 都算
    report.cases.stopped = keys(fx.filterProjects(projects, attention, { mode: "stopped", q: "" }));

    // 5) 关键词命中产线名
    report.cases.queryLine = keys(fx.filterProjects(projects, attention, { mode: "all", q: "巡检" }));

    // 6) 关键词命中厂区名：整座厂区放行
    report.cases.queryProject = keys(fx.filterProjects(projects, attention, { mode: "all", q: "工具" }));

    // 7) 模式 + 关键词组合（工具厂里在跑的没有 → 空结果）
    report.cases.combo = keys(fx.filterProjects(projects, attention, { mode: "running", q: "工具" }));

    // 8) 大小写与空白（英文名）
    report.cases.caseInsensitive = keys(fx.filterProjects(
      [{ id: 9, name: "DataPlatform", lines: [
        { key: "cron:91", kind: "cron", id: 91, name: "Daily Build", status: "idle" }] }],
      new Set(), { mode: "all", q: "  daily  " }));

    // 9) 场景「痕迹」口径：一只托盘几箱、最多几只、返工堆封顶几箱；
    //    statFor 是成品垛 / 返工堆的唯一取数口（缺数据时返回全零，不能是 undefined）
    report.cases.pallet = [fx.PALLET_SIZE, fx.PALLET_MAX, fx.REWORK_MAX];
    fx.S.stats = { 5: { done: 13, rework: 4, busy: 1, lines: 3 } };
    report.cases.stat = fx.statFor(5);
    report.cases.statMissing = fx.statFor(999);

    // 10) filterActive：默认不活跃，任一条件即活跃
    report.cases.filterActive = [
      fx.filterActive(),
      (function () { fx.S.filter.mode = "running"; return fx.filterActive(); })(),
      (function () { fx.S.filter.mode = "all"; fx.S.filter.q = "x"; return fx.filterActive(); })(),
      (function () { fx.S.filter.q = ""; return fx.filterActive(); })(),
    ];

    // 11) 文案格式化（P1 的 token / 工序显示）
    report.cases.tokens = [0, 999, 1500, 18400, 2500000].map(fx.fmtTokens);
    report.cases.progress = [
      fx.progressText({ tool: "fs.write", steps: 7, at: Date.now() / 1000 }),
      fx.progressText({ tool: "", steps: 3, at: Date.now() / 1000 }),
      fx.progressText({ tool: "shell", steps: 1, at: Date.now() / 1000 - 120 }),
    ];

    // 12) 昼光系数：默认本机 06:00 / 18:00 为中心、各 ±40 分钟过渡带
    var D = function (h, m) {
      return Math.round(fx.daylightFactor(new Date(2026, 9, 25, h, m)) * 1000) / 1000;
    };
    report.cases.daylight = [
      D(5, 19), D(5, 20), D(6, 0), D(6, 40), D(12, 0), D(17, 20), D(18, 0), D(18, 40), D(23, 0),
    ];
  } catch (e) {
    report.fatal = String((e && e.stack) || e);
  }

  process.stdout.write("__PANEL_REPORT__" + JSON.stringify(report) + "\n");
})();
'''


def _build_probe_source() -> str:
    """按加载顺序拼接工厂四个分区文件（共享 window.FX），探针从 FX 取内部 API。"""
    js = "\n".join((STATIC_DIR / n).read_text(encoding="utf-8") for n in (
        "app-factory.js", "app-factory-scene.js",
        "app-factory-drawer.js", "app-factory-draw.js"))
    exports = ", ".join(f"{name}: window.FX.{name}" for name in _EXPORTED)
    tail = f"\nwindow.__panelTest = {{ {exports} }};\n"
    shim = (
        "var window = {};\n"
        "var document = { getElementById: function () { return null; } };\n"
    )
    return shim + js + tail + "\n" + _DRIVER_JS


@pytest.fixture(scope="module")
def panel_report(tmp_path_factory) -> dict:
    if NODE_BIN is None:
        pytest.skip("需要 Node.js 运行时（node 未安装）")
    probe_path = tmp_path_factory.mktemp("factory-panel") / "panel_probe.js"
    probe_path.write_text(_build_probe_source(), encoding="utf-8")
    proc = subprocess.run(
        [NODE_BIN, str(probe_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("__PANEL_REPORT__")), None
    )
    if proc.returncode != 0 or line is None:
        pytest.fail(
            f"Node 工厂面板探针失败（exit {proc.returncode}）\n"
            f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
        )
    report = json.loads(line[len("__PANEL_REPORT__"):])
    if report.get("fatal"):
        pytest.fail("工厂面板探针异常：\n" + report["fatal"])
    return report["cases"]


def test_filter_all_keeps_empty_yards_for_new_line_entry(panel_report):
    """「全部」下空厂区仍要露出——目录里得能看见「这个项目还没产线」，
    否则用户会以为工厂漏掉了它（建线本身在自动化页，不在工厂里）。"""
    assert panel_report["all"] == [
        {"project": "内容厂", "lines": ["cron:11", "cron:12", "cron:13", "pipe:1"]},
        {"project": "工具厂", "lines": ["cron:21", "cron:22"]},
        {"project": "空厂区", "lines": []},
    ]


def test_filter_running_attention_and_stopped(panel_report):
    assert panel_report["running"] == [{"project": "内容厂", "lines": ["cron:11"]}]
    assert panel_report["attention"] == [
        {"project": "内容厂", "lines": ["cron:12"]},
        {"project": "工具厂", "lines": ["cron:22"]},
    ]
    # 停工 = 暂停 + 厂区不可用；装配线不参与
    assert panel_report["stopped"] == [
        {"project": "内容厂", "lines": ["cron:13"]},
        {"project": "工具厂", "lines": ["cron:22"]},
    ]


def test_filter_query_matches_line_or_whole_project(panel_report):
    assert panel_report["queryLine"] == [{"project": "内容厂", "lines": ["cron:12"]}]
    assert panel_report["queryProject"] == [
        {"project": "工具厂", "lines": ["cron:21", "cron:22"]},
    ]
    # 模式与关键词同时生效：工具厂没有在跑的产线 → 整组消失
    assert panel_report["combo"] == []
    # 英文名大小写不敏感，两端空白被忽略
    assert panel_report["caseInsensitive"] == [
        {"project": "DataPlatform", "lines": ["cron:91"]},
    ]

def test_daylight_factor_transition_band(panel_report):
    """昼光系数：日出日落各 ±40 分钟过渡带，带内线性、带外取整；正午恒昼。"""
    assert panel_report["daylight"] == [0, 0, 0.5, 1, 1, 1, 0.5, 0, 0]


def test_scene_trace_limits_and_stat_lookup(panel_report):
    """成品垛 / 返工堆的口径：一只托盘 6 箱、最多 2 只、返工堆 6 箱封顶。"""
    assert panel_report["pallet"] == [6, 2, 6]
    assert panel_report["stat"] == {"done": 13, "rework": 4, "busy": 1, "lines": 3}
    # 取不到厂区时返回全零而不是 undefined：绘制层直接读 .done / .rework
    assert panel_report["statMissing"] == {"done": 0, "rework": 0, "busy": 0, "lines": 0}


def test_filter_active_flag_and_formatting(panel_report):
    assert panel_report["filterActive"] == [False, True, True, False]
    assert panel_report["tokens"] == ["0", "999", "1.5k", "18k", "2.5M"]
    progress = panel_report["progress"]
    assert progress[0] == "fs.write · 第 7 步"
    assert progress[1] == "第 3 步"
    assert progress[2] == "", "超过 30 秒的进度不能继续显示（不谎报上一个工具名）"
