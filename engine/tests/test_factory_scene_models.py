"""工厂场景 · 大型设备模型（冷却塔 / 管廊 / 输煤栈桥）回归。

`app-factory.js` 是零构建普通 script，全部绘图函数藏在文件末尾的 IIFE 里，
源码检查抓不到「坐标算成 NaN」「颜色键拼错」这类运行时问题。本文件用 Node
vm 加载真实的 `app-factory.js`，在 IIFE 收口前注入 `window.__factoryTest`
导出内部函数，再用假 Canvas 逐模型跑 `drawModel`：

- `buildIndustrialSite` 的四种厂区变体（主厂房高度 38 / 24 / 56 / 34）都跑到
- 全部模型坐标为有限数；新设备 path 至少两点、无零长度段、高度/宽度为正
- `drawModel` 对每个 kind 都真的产生像素（新 kind 已接进分派）
- 假 Canvas 记录 `fillStyle` 与 `fillRect` 参数：不出现 NaN，也不把 undefined
  当颜色
- `buildWorld` 的 bounds 覆盖新增公用设施，`fitCamera` 后边界角点仍在缓冲区内

没有 Node 时整组 skip（与 test_mods_node 等同一口径）；不碰浏览器、不写用户数据。
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

_NEW_KINDS = ("cooling", "pipeRack", "transferBridge")
# buildIndustrialSite 里四个 variant 分支的主厂房高度（variant = hash("site"+id) % 4）
_VARIANT_MAIN_HEIGHTS = {"0": 38, "1": 24, "2": 56, "3": 34}

# 注入到 IIFE 收口之前的导出；只暴露测试要直接调用的内部函数/状态。
_EXPORTED = (
    "S",
    "PAL_DAY",
    "PAL_NIGHT",
    "buildIndustrialSite",
    "buildWorld",
    "drawModel",
    "drawModels",
    "fitCamera",
    "hashKey",
    "drawCoolingTowerModel",
    "drawPipeRackModel",
    "drawTransferBridgeModel",
    "drawRackSupports",
)

# 在 Node 里跑的探针。所有断言只收集进 report，由 Python 逐项断言，
# 这样一次 Node 启动就能给出多个用例的失败细节。
_DRIVER_JS = r'''
(function () {
  "use strict";
  var fx = window.__factoryTest;
  var report = {
    fatal: null,
    variantIds: {},
    variantHeights: {},
    variantNewKinds: {},
    newKindCounts: {},
    geometry: {},
    modelIssues: [],
    drawThrows: [],
    drawNumberIssues: [],
    drawColorIssues: [],
    paletteGaps: {},
    fillCounts: {},
    cameraIssues: [],
    bounds: null
  };
  function note(list, msg) { if (list.length < 30) list.push(String(msg)); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }
  function finite(v) { return typeof v === "number" && Number.isFinite(v); }

  function makeGradient(issues) {
    return {
      addColorStop: function (offset, color) {
        if (!finite(offset) || offset < 0 || offset > 1) note(issues.numbers, "gradient stop=" + offset);
        if (typeof color !== "string" || !color.trim()) note(issues.colors, String(color));
      }
    };
  }
  // 假 Canvas：只认 app-factory.js 用到的 API；颜色与数值在这里记账。
  function makeCtx() {
    var issues = { colors: [], numbers: [], calls: 0 };
    var style = "#000000";
    var target = {
      globalAlpha: 1,
      imageSmoothingEnabled: false,
      fillRect: function (x, y, w, h) {
        issues.calls += 1;
        if (![x, y, w, h].every(finite)) {
          note(issues.numbers, "fillRect(" + x + "," + y + "," + w + "," + h + ")");
        }
      },
      save: function () {},
      restore: function () {},
      translate: function () {},
      setTransform: function () {},
      createLinearGradient: function () { return makeGradient(issues); },
      createRadialGradient: function () { return makeGradient(issues); }
    };
    return new Proxy(target, {
      get: function (t, key) {
        if (key === "fillStyle") return style;
        if (key === "_issues") return issues;
        if (own(t, key)) return t[key];
        return function () { note(issues.numbers, "缺少假 Canvas 方法 " + String(key)); };
      },
      set: function (t, key, value) {
        if (key === "fillStyle") {
          if (typeof value !== "string" || !value.trim()) note(issues.colors, String(value));
          style = value;
          return true;
        }
        if (key === "globalAlpha" && !finite(value)) note(issues.numbers, "globalAlpha=" + value);
        t[key] = value;
        return true;
      }
    });
  }

  function fakeFactory(id) {
    return {
      id: id, name: id, x: 0, y: 0, halfW: 80, halfH: 40, lines: [],
      belt: [{ x: 0, y: 0 }, { x: 20, y: 10 }], tray: { x: 10, y: 10 },
      entrance: { x: 0, y: 0 }, root_ok: true
    };
  }

  function makeProjects(n) {
    var projects = [];
    for (var i = 0; i < n; i += 1) {
      var lines = [];
      for (var j = 0; j <= i % 3; j += 1) {
        lines.push({ key: "proj" + i + "-line" + j, kind: "cron", id: i * 10 + j, status: "idle" });
      }
      projects.push({ id: "proj-" + i, name: "项目" + i, lines: lines });
    }
    return projects;
  }

  function scanModels(models, tag) {
    models.forEach(function (m, idx) {
      var where = tag + "[" + idx + "]:" + m.kind;
      if (typeof m.kind !== "string" || !m.kind) note(report.modelIssues, where + " 缺少 kind");
      Object.keys(m).forEach(function (key) {
        // 这三个是回指（line -> rec -> factory -> project），不参与数值扫描。
        if (key === "line" || key === "rec" || key === "project" || key === "factory") return;
        var value = m[key];
        if (typeof value === "number" && !Number.isFinite(value)) {
          note(report.modelIssues, where + "." + key + "=" + value);
        }
      });
      if (m.path) {
        if (!Array.isArray(m.path) || m.path.length < 2) {
          note(report.modelIssues, where + ".path 少于 2 个点");
          return;
        }
        m.path.forEach(function (pt, pi) {
          if (!pt || !finite(pt.x) || !finite(pt.y)) {
            note(report.modelIssues, where + ".path[" + pi + "] 非有限坐标");
          }
        });
      }
    });
  }

  function countNewKinds(models) {
    var counts = { cooling: 0, pipeRack: 0, transferBridge: 0 };
    models.forEach(function (m) {
      if (own(counts, m.kind)) counts[m.kind] += 1;
    });
    return counts;
  }

  function pathLen(pts) {
    var total = 0;
    for (var i = 1; i < pts.length; i += 1) {
      total += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    }
    return total;
  }

  function geometryOf(models) {
    var cooling = models.filter(function (m) { return m.kind === "cooling"; });
    var pipeRack = models.filter(function (m) { return m.kind === "pipeRack"; });
    var bridge = models.filter(function (m) { return m.kind === "transferBridge"; });
    return {
      cooling: {
        count: cooling.length,
        radii: cooling.map(function (m) { return m.r; }),
        heights: cooling.map(function (m) { return m.h; }),
        z0s: cooling.map(function (m) { return m.z0 || 0; }),
        points: cooling.map(function (m) { return [m.x, m.y]; })
      },
      pipeRack: {
        count: pipeRack.length,
        widths: pipeRack.map(function (m) { return m.width; }),
        heights: pipeRack.map(function (m) { return [m.h0, m.h1]; }),
        pathPoints: pipeRack.map(function (m) { return m.path ? m.path.length : 0; }),
        pathLengths: pipeRack.map(function (m) { return pathLen(m.path || []); })
      },
      transferBridge: {
        count: bridge.length,
        widths: bridge.map(function (m) { return m.width; }),
        heights: bridge.map(function (m) { return [m.h0, m.h1]; }),
        pathPoints: bridge.map(function (m) { return m.path ? m.path.length : 0; }),
        pathLengths: bridge.map(function (m) { return pathLen(m.path || []); })
      }
    };
  }

  function variants() {
    var found = {};
    for (var n = 0; n < 500 && Object.keys(found).length < 4; n += 1) {
      var id = "variant-" + n;
      var v = Math.abs(fx.hashKey("site" + id)) % 4;
      if (!own(found, v)) found[v] = id;
    }
    if (Object.keys(found).length !== 4) throw new Error("找不到覆盖四个厂区变体的项目 id");
    return found;
  }

  try {
    var ids = variants();
    for (var v = 0; v < 4; v += 1) {
      var site = fx.buildIndustrialSite({ x: 0, y: 0 }, [fakeFactory(ids[v])]);
      var steel = site.models.filter(function (m) {
        return m.kind === "box" && m.role === "steel" && !m.bg && m.z0 === 5;
      });
      report.variantIds[String(v)] = ids[v];
      report.variantHeights[String(v)] = steel.length ? steel[0].h : null;
      report.variantNewKinds[String(v)] = countNewKinds(site.models);
      scanModels(site.models, "variant" + v);
    }

    var baseSite = fx.buildIndustrialSite({ x: 0, y: 0 }, [fakeFactory(ids[0])]);
    report.geometry = geometryOf(baseSite.models);
    report.newKindCounts = countNewKinds(baseSite.models);

    // 新绘图函数里引用的调色板键必须真的存在（拼错 stoneLight 之类在这里现形）。
    var newSource = [
      fx.drawCoolingTowerModel, fx.drawPipeRackModel, fx.drawTransferBridgeModel, fx.drawRackSupports
    ].map(function (fn) { return fn.toString(); }).join("\n");
    var refs = {};
    newSource.replace(/\bpal\.([A-Za-z0-9_]+)/g, function (all, key) { refs[key] = true; return all; });
    ["PAL_DAY", "PAL_NIGHT"].forEach(function (name) {
      report.paletteGaps[name] = Object.keys(refs).filter(function (key) {
        return !own(fx[name], key);
      }).sort();
    });

    // 4 个项目覆盖环 0 的四个槽位；全部模型逐个按昼/夜盘、三档缩放绘制。
    fx.buildWorld({ projects: makeProjects(4), counters: {} });
    var worldModels = fx.S.world.models;
    scanModels(worldModels, "world");
    [["PAL_DAY", false], ["PAL_NIGHT", true]].forEach(function (pair) {
      var pal = fx[pair[0]];
      fx.S.night = pair[1];
      [0.22, 1, 2.6].forEach(function (zoom) {
        fx.S.cam.zoom = zoom;
        worldModels.forEach(function (m) {
          var ctx = makeCtx();
          try {
            fx.drawModel(ctx, pal, m);
          } catch (e) {
            note(report.drawThrows, pair[0] + "@" + zoom + " " + m.kind + ": " + e.message);
          }
          ctx._issues.colors.forEach(function (c) {
            note(report.drawColorIssues, pair[0] + "@" + zoom + " " + m.kind + ": " + c);
          });
          ctx._issues.numbers.forEach(function (msg) {
            note(report.drawNumberIssues, pair[0] + "@" + zoom + " " + m.kind + ": " + msg);
          });
          if (zoom === 1) report.fillCounts[m.kind] = (report.fillCounts[m.kind] || 0) + ctx._issues.calls;
        });
      });
    });

    // drawModels 的画家排序 / path 中点入口也走一遍，防止只有单模型路径可用。
    fx.S.cam.zoom = 1;
    ["PAL_DAY", "PAL_NIGHT"].forEach(function (name) {
      fx.S.night = name === "PAL_NIGHT";
      var ctx = makeCtx();
      try {
        fx.drawModels(ctx, fx[name]);
      } catch (e) {
        note(report.drawThrows, name + " drawModels: " + e.message);
      }
      ctx._issues.colors.forEach(function (c) { note(report.drawColorIssues, name + " drawModels: " + c); });
      ctx._issues.numbers.forEach(function (msg) {
        note(report.drawNumberIssues, name + " drawModels: " + msg);
      });
    });

    // bounds / fitCamera：0/1/4/7 个项目都检查（7 个会跨到第二环）。
    [0, 1, 4, 7].forEach(function (n) {
      fx.buildWorld({ projects: makeProjects(n), counters: {} });
      fx.S.bufW = 900;
      fx.S.bufH = 500;
      fx.S.cssW = 900;
      fx.S.cssH = 500;
      fx.fitCamera(false);
      var b = fx.S.world.bounds;
      var cam = fx.S.cam;
      [b.minX, b.maxX, b.minY, b.maxY, cam.x, cam.y, cam.zoom].forEach(function (value) {
        if (!finite(value)) note(report.cameraIssues, n + " 项目时 bounds/cam 出现非有限值: " + value);
      });
      if (!(b.minX < b.maxX) || !(b.minY < b.maxY)) {
        note(report.cameraIssues, n + " 项目时 bounds 不成区间: " + JSON.stringify(b));
      }
      var skyH = Math.max(26, Math.round(fx.S.bufH * 0.22) + 6);
      var cx = (b.minX + b.maxX) / 2;
      // 世界后缘要落在地平线以下：镜头中心按天空带高度下移，不再等于 bounds 几何中心
      var cy = b.minY + (fx.S.bufH / 2 - skyH) / cam.zoom;
      if (Math.abs(cam.x - cx) > 1e-6 || Math.abs(cam.y - cy) > 1e-6) {
        note(report.cameraIssues, n + " 项目时镜头中心未按取景公式对齐");
      }
      if (!(cam.zoom >= 0.22 && cam.zoom <= 1.45)) {
        note(report.cameraIssues, n + " 项目时 zoom 越界: " + cam.zoom);
      }
      fx.S.world.models.forEach(function (m) {
        if (m.bg) return;
        var points = m.path || [{ x: m.x, y: m.y }];
        var hw = m.hw || m.r || m.width || 8;
        var hh = m.hh || (m.r ? m.r * 0.5 : 5);
        var height = Math.min(30, (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0)));
        points.forEach(function (pt) {
          if (pt.x - hw < b.minX - 1e-6 || pt.x + hw > b.maxX + 1e-6) {
            note(report.cameraIssues, n + " 项目时 " + m.kind + " 横向超出 bounds");
          }
          if (pt.y - height - hh < b.minY - 1e-6) {
            note(report.cameraIssues, n + " 项目时 " + m.kind + " 顶部超出 bounds");
          }
          if (pt.y + hh > b.maxY + 1e-6) {
            note(report.cameraIssues, n + " 项目时 " + m.kind + " 底部超出 bounds");
          }
        });
      });
      var pad = 30;
      var z = cam.zoom;
      [[b.minX, b.minY], [b.minX, b.maxY], [b.maxX, b.minY], [b.maxX, b.maxY]].forEach(function (corner) {
        var sx = (corner[0] - cam.x) * z + fx.S.bufW / 2;
        var sy = (corner[1] - cam.y) * z + fx.S.bufH / 2;
        if (sx < pad - 2 || sx > fx.S.bufW - pad + 2 || sy < skyH - 2 || sy > fx.S.bufH - 10) {
          note(report.cameraIssues, n + " 项目时 bounds 角点越出缓冲区 (" + sx + "," + sy + ")");
        }
      });
      if (n === 4) {
        report.bounds = { minX: b.minX, maxX: b.maxX, minY: b.minY, maxY: b.maxY };
        var cooling = fx.S.world.models.filter(function (m) { return m.kind === "cooling"; });
        var bridges = fx.S.world.models.filter(function (m) { return m.kind === "transferBridge"; });
        // 高塔只算占地（塔顶可以出画），否则一座冷却塔会把整个场景压小
        var coolFootprint = Math.min.apply(null, cooling.map(function (m) {
          return m.y - (m.z0 || 0) - (m.r || 0) * 0.5 - 16;
        }));
        if (b.minY > coolFootprint + 1e-6) note(report.cameraIssues, "bounds.minY 未覆盖冷却塔占地");
        if (bridges.length !== 1 || !bridges[0].bg) note(report.cameraIssues, "远景转运桥缺失或未标记 bg");
      }
    });
  } catch (e) {
    report.fatal = String((e && e.stack) || e);
  }

  process.stdout.write("__SCENE_REPORT__" + JSON.stringify(report) + "\n");
})();
'''


def _build_probe_source() -> str:
    """把 app-factory.js 的 IIFE 尾部改成先导出内部 API，再接上 Node 探针。"""
    js = (STATIC_DIR / "app-factory.js").read_text(encoding="utf-8")
    exports = ", ".join(f"{name}: {name}" for name in _EXPORTED)
    inject = f"  window.__factoryTest = {{ {exports} }};\n"
    idx = js.rindex("})();")
    instrumented = js[:idx] + inject + js[idx:]
    shim = (
        "var window = {};\n"
        "var document = { getElementById: function () { return null; } };\n"
    )
    return shim + instrumented + "\n" + _DRIVER_JS


@pytest.fixture(scope="module")
def scene_report(tmp_path_factory) -> dict:
    if NODE_BIN is None:
        pytest.skip("需要 Node.js 运行时（node 未安装）")
    probe_path = tmp_path_factory.mktemp("factory-scene") / "scene_probe.js"
    probe_path.write_text(_build_probe_source(), encoding="utf-8")
    proc = subprocess.run(
        [NODE_BIN, str(probe_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("__SCENE_REPORT__")),
        None,
    )
    if proc.returncode != 0 or line is None:
        pytest.fail(
            f"Node 工厂场景探针失败（exit {proc.returncode}）\n"
            f"stdout:\n{proc.stdout[-4000:]}\nstderr:\n{proc.stderr[-4000:]}"
        )
    report = json.loads(line[len("__SCENE_REPORT__") :])
    if report.get("fatal"):
        pytest.fail("工厂场景探针异常：\n" + report["fatal"])
    return report


def test_factory_scene_builds_all_four_plot_variants(scene_report):
    """四种厂区变体都要真实构建出来（不只是源码里存在四个分支）。"""
    assert scene_report["variantHeights"] == _VARIANT_MAIN_HEIGHTS, (
        f"四种厂区未全部覆盖或主厂房高度变化：{scene_report['variantHeights']}"
        f"（id: {scene_report['variantIds']}）"
    )
    for variant in ("0", "1", "2", "3"):
        assert scene_report["variantNewKinds"][variant] == {
            "cooling": 2,
            "pipeRack": 2,
            "transferBridge": 1,
        }, f"厂区变体 {variant} 缺少新增公用设施"


def test_factory_scene_new_models_have_finite_coordinates_and_paths(scene_report):
    """模型坐标全部有限；三件新设备的 path / 高度 / 宽度符合设计值。"""
    assert scene_report["modelIssues"] == [], (
        "模型出现 NaN/Infinity 或坏 path：" + "; ".join(scene_report["modelIssues"])
    )
    g = scene_report["geometry"]
    assert g["cooling"]["count"] == 2
    assert g["cooling"]["radii"] == [20, 17]
    assert sorted(g["cooling"]["heights"]) == [52, 62]
    assert g["cooling"]["z0s"] == [3, 3]
    assert g["cooling"]["points"] == [[-78, -64], [46, -72]]

    assert g["pipeRack"]["count"] == 2
    assert g["pipeRack"]["widths"] == [7, 6]
    assert g["pipeRack"]["heights"] == [[9, 9], [8, 8]]
    assert g["pipeRack"]["pathPoints"] == [2, 2]
    assert all(length >= 10 for length in g["pipeRack"]["pathLengths"])

    assert g["transferBridge"]["count"] == 1
    assert g["transferBridge"]["widths"] == [8]
    assert g["transferBridge"]["heights"] == [[20, 15]]
    assert g["transferBridge"]["pathPoints"] == [2]
    assert all(length > 50 for length in g["transferBridge"]["pathLengths"])


def test_factory_scene_draw_model_has_no_nonfinite_pixels(scene_report):
    """drawModel 的每个 kind 都产生像素，fillRect 参数不出现 NaN/Infinity。"""
    assert scene_report["drawThrows"] == [], (
        "drawModel 抛异常：" + "; ".join(scene_report["drawThrows"])
    )
    assert scene_report["drawNumberIssues"] == [], (
        "drawModel 出现非有限数值：" + "; ".join(scene_report["drawNumberIssues"])
    )
    fills = scene_report["fillCounts"]
    for kind in _NEW_KINDS:
        assert fills.get(kind, 0) > 0, f"{kind} 没有画出任何像素（drawModel 分派缺失）"
    for kind, count in fills.items():
        assert count > 0, f"{kind} 没有画出任何像素"


def test_factory_scene_draw_model_uses_defined_colors(scene_report):
    """fillStyle 不得被赋成 undefined/非字符串（调色板键必须真实存在）。"""
    gaps = {name: keys for name, keys in scene_report["paletteGaps"].items() if keys}
    assert not gaps and scene_report["drawColorIssues"] == [], (
        f"drawModel 给 fillStyle 赋了未定义颜色。调色板缺口: {gaps}；"
        f"绘制现场: {scene_report['drawColorIssues'][:5]}。"
        "新增设备用了 pal.stone / pal.stoneDark / pal.stoneLight，但 PAL_DAY 与 PAL_NIGHT "
        "都没有这三个键（既有 drawTerrain 也在用），需要补键或改用已定义的钢色。"
    )


def test_factory_scene_camera_bounds_cover_utility_models(scene_report):
    """bounds 覆盖冷却塔/管廊/栈桥，fitCamera 后画面仍落在缓冲区内。"""
    assert scene_report["cameraIssues"] == [], (
        "镜头/边界检查失败：" + "; ".join(scene_report["cameraIssues"])
    )
    bounds = scene_report["bounds"]
    assert bounds is not None
    # 4 个项目时：台地后缘（含冷却塔占地）与两翼堆场都要在取景范围内
    assert bounds["minY"] <= -141.0, f"bounds.minY 没有覆盖台地后缘: {bounds}"
    assert bounds["maxX"] >= 280.0, f"bounds.maxX 没有覆盖翼侧堆场: {bounds}"
