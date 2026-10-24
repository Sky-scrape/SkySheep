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
    "batchRunTargets",
    "batchPauseTargets",
    "matchFilterMode",
    "filterActive",
    "fmtTokens",
    "progressText",
    "BATCH_RUN_LIMIT",
    "S",
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

    // 9) 批量派工 / 暂停的目标筛选
    var content = projects[0].lines;
    report.cases.batchRun = ids(fx.batchRunTargets(content));
    report.cases.batchPause = ids(fx.batchPauseTargets(content));
    report.cases.batchRunStopped = ids(fx.batchRunTargets(projects[1].lines));
    report.cases.batchLimit = fx.BATCH_RUN_LIMIT;

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
  } catch (e) {
    report.fatal = String((e && e.stack) || e);
  }

  process.stdout.write("__PANEL_REPORT__" + JSON.stringify(report) + "\n");
})();
'''


def _build_probe_source() -> str:
    js = (STATIC_DIR / "app-factory.js").read_text(encoding="utf-8")
    exports = ", ".join(f"{name}: {name}" for name in _EXPORTED)
    inject = f"  window.__panelTest = {{ {exports} }};\n"
    idx = js.rindex("})();")
    instrumented = js[:idx] + inject + js[idx:]
    shim = (
        "var window = {};\n"
        "var document = { getElementById: function () { return null; } };\n"
    )
    return shim + instrumented + "\n" + _DRIVER_JS


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
    """「全部」下空厂区仍要露出——否则「＋ 新建一条定时产线」的入口会跟着消失。"""
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


def test_batch_targets_exclude_running_paused_stopped_and_pipelines(panel_report):
    # 内容厂：晨间简报在跑、素材归档暂停、装配线不参与 → 只剩竞品巡检
    assert panel_report["batchRun"] == ["cron:12"]
    # 暂停目标：在跑与待命的都能停，已暂停的跳过，装配线不碰
    assert panel_report["batchPause"] == ["cron:11", "cron:12"]
    # 工具厂：依赖体检可派；夜班（停工）不派
    assert panel_report["batchRunStopped"] == ["cron:21"]
    assert panel_report["batchLimit"] == 5, "批量派工单次上限"


def test_filter_active_flag_and_formatting(panel_report):
    assert panel_report["filterActive"] == [False, True, True, False]
    assert panel_report["tokens"] == ["0", "999", "1.5k", "18k", "2.5M"]
    progress = panel_report["progress"]
    assert progress[0] == "fs.write · 第 7 步"
    assert progress[1] == "第 3 步"
    assert progress[2] == "", "超过 30 秒的进度不能继续显示（不谎报上一个工具名）"
