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
- `sawRoofFaces` 的 9 个面（每段坡面/山墙/采光立面）彼此共边、不脱离，
  全部落底点都在菱形顶面边界上，抬升点恰在底点正上方 `rise` 处
- `sceneDrawItems` 的传送带切片保留原路径拐角与总长、高度沿总长连续；
  绘制顺序是「铺装 → 背景 → 主体」，主体按 y 升序、同 xy 按 z0 升序

没有 Node 时整组 skip（与 test_mods_node 等同一口径）；不碰浏览器、不写用户数据。
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import pytest

from skysheep.core.mods_node import find_node

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"
NODE_BIN = shutil.which("node") or find_node()

_NEW_KINDS = ("cooling", "pipeRack", "transferBridge")
# 厂外建筑（前场料场 / 罐区）：模型种类 → buildIndustrialSite 里所属的建筑群
_FRONT_BUILDING_KINDS = ("coalShed", "siloBattery", "gasholder", "basin", "loadingRack", "flare")
_FRONT_GROUPS = ("stockyard", "tankFarm")
# buildIndustrialSite 里四个 variant 分支的主厂房高度（variant = hash("site"+id) % 4）
_VARIANT_MAIN_HEIGHTS = {"0": 38, "1": 24, "2": 56, "3": 34, "4": 27, "5": 32, "6": 22, "7": 29}

# 注入到 IIFE 收口之前的导出；只暴露测试要直接调用的内部函数/状态。
_EXPORTED = (
    "S",
    "PAL_DAY",
    "PAL_NIGHT",
    "buildIndustrialSite",
    "buildWorld",
    "makePlateau",
    "gridSlots",
    "GRID_X",
    "GRID_Y",
    "drawModel",
    "drawModels",
    "sceneDrawItems",
    "drawLayer",
    "dom",
    "resizeScene",
    "onPointerDown",
    "onPointerMove",
    "onPointerUp",
    "onPointerLeave",
    "onWheel",
    "sawRoofFaces",
    "fitCamera",
    "sceneHorizon",
    "hashKey",
    "drawCoolingTowerModel",
    "drawPipeRackModel",
    "drawTransferBridgeModel",
    "drawRackSupports",
    "drawCoalShedModel",
    "drawSiloBatteryModel",
    "drawGasholderModel",
    "drawBasinModel",
    "drawLoadingRackModel",
    "drawFlareModel",
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
    compounds: {},
    compoundIssues: [],
    newKindCounts: {},
    geometry: {},
    modelIssues: [],
    drawThrows: [],
    drawNumberIssues: [],
    drawColorIssues: [],
    paletteGaps: {},
    fillCounts: {},
    roof: { cases: [], issues: [] },
    slicing: { cases: [], issues: [] },
    ordering: { rows: [], order: [], issues: [] },
    cameraIssues: [],
    bounds: null,
    frontGroups: {},
    frontBuildings: {},
    frontRoads: {},
    frontPlacement: {}
  };
  function note(list, msg) { if (list.length < 30) list.push(String(msg)); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj, key); }
  function finite(v) { return typeof v === "number" && Number.isFinite(v); }
  function eqNum(a, b, eps) {
    var tol = eps || 1e-9;
    return finite(a) && finite(b) && Math.abs(a - b) <= tol * Math.max(1, Math.abs(a), Math.abs(b));
  }

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
      entrance: { x: 0, y: 0 }, hub: { x: 20, y: 2 }, root_ok: true
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
        pathLengths: bridge.map(function (m) { return pathLen(m.path || []); }),
        bgFlags: bridge.map(function (m) { return !!m.bg; })
      }
    };
  }

  function variants() {
    var found = {};
    for (var n = 0; n < 500 && Object.keys(found).length < 8; n += 1) {
      var id = "variant-" + n;
      var v = Math.abs(fx.hashKey("site" + id)) % 8;
      if (!own(found, v)) found[v] = id;
    }
    if (Object.keys(found).length !== 8) throw new Error("找不到覆盖八个厂区变体的项目 id");
    return found;
  }

  try {
    var ids = variants();
    for (var v = 0; v < 8; v += 1) {
      var factory = fakeFactory(ids[v]);
      var site = fx.buildIndustrialSite({ x: 0, y: 0 }, [factory]);
      var compound = site.models.filter(function (m) { return m.compoundPart; });
      var ground = compound.filter(function (m) { return m.z0 === 5; });
      report.compounds[String(v)] = {
        style: factory.compoundStyle,
        signature: ground.map(function (m) {
          return [m.kind, m.x, m.y, m.hw || m.r, m.hh, m.h, m.role].join(":");
        }).join("|"),
        tanks: ground.filter(function (m) { return !!m.silo; }).length,
        containers: compound.filter(function (m) { return !!m.container; }).length,
        chimneys: compound.filter(function (m) { return !!m.chimney; }).length,
        processModels: compound.filter(function (m) {
          return m.kind === "furnace" || m.kind === "distillation";
        }).map(function (m) { return { kind: m.kind, h: m.h, platforms: m.platforms || 0 }; })
      };
      compound.forEach(function (m) {
        if (m.projectId !== factory.id) note(report.compoundIssues, "模型未绑定项目 " + v);
        var points = m.path || [m];
        points.forEach(function (p) {
          var w = m.hw || m.r || 0, d = m.hh || m.r * 0.5 || 0;
          var ratio = Math.abs(p.x) / factory.halfW + Math.abs(p.y) / factory.halfH +
                      Math.max(w / factory.halfW, d / factory.halfH);
          if (ratio > 0.96 + 1e-9) note(report.compoundIssues, "建筑占地越界 " + v + ":" + m.kind);
        });
      });
      var steel = site.models.filter(function (m) {
        return m.kind === "box" && m.role === "steel" && !m.bg && m.z0 === 5;
      });
      report.variantIds[String(v)] = ids[v];
      report.variantHeights[String(v)] = steel.length ? steel[0].h : null;
      report.variantNewKinds[String(v)] = countNewKinds(site.models);
      scanModels(site.models, "variant" + v);
      [fx.PAL_DAY, fx.PAL_NIGHT].forEach(function (pal) {
        site.models.forEach(function (model) {
          var ctx = makeCtx();
          fx.S.night = pal === fx.PAL_NIGHT;
          if (model.kind === "furnace" || model.kind === "distillation") {
            [0.22, 1, 2.6].forEach(function (zoom) {
              fx.S.cam.zoom = zoom;
              fx.drawModel(ctx, pal, model);
            });
            if (!ctx._issues.calls) note(report.drawNumberIssues, model.kind + " 未产生像素");
            fx.S.cam.zoom = 1;
          } else fx.drawModel(ctx, pal, model);
          ctx._issues.colors.forEach(function (issue) {
            note(report.drawColorIssues, "variant" + v + " " + model.kind + ": " + issue);
          });
          ctx._issues.numbers.forEach(function (issue) {
            note(report.drawNumberIssues, "variant" + v + " " + model.kind + ": " + issue);
          });
        });
      });
    }

    var baseSite = fx.buildIndustrialSite({ x: 0, y: 0 }, [fakeFactory(ids[0])]);
    report.geometry = geometryOf(baseSite.models);
    report.newKindCounts = countNewKinds(baseSite.models);
    report.roadsideTanks = baseSite.models.filter(function (m) { return !!m.roadsideTank; })
      .map(function (m) {
        return { side: m.roadsideTank, x: m.x, y: m.y, h: m.h, r: m.r, bg: m.bg };
      });
    report.facilityIssues = [];
    [0, 2, 8, 9, 25].forEach(function (count) {
      var grouped = fx.buildIndustrialSite({ x: 0, y: 0 },
        Array.from({ length: count }, function (_, i) { return fakeFactory("group-" + i); }));
      var ring = Math.max(1, Math.ceil((Math.sqrt(count + 1) - 1) / 2));
      ["waterTower", "substation", "repairDepot", "coolingPlant"].forEach(function (name) {
        var original = baseSite.models.filter(function (m) { return m.facility === name; });
        var current = grouped.models.filter(function (m) { return m.facility === name; });
        current.forEach(function (m, i) {
          (m.path || [m]).forEach(function (p, j) {
            var ref = (original[i].path || [original[i]])[j];
            if (!eqNum(p.x - ref.x, m.facilityTarget.x * (ring - 1)) ||
                !eqNum(p.y - ref.y, m.facilityTarget.y * (ring - 1))) {
              report.facilityIssues.push(name + " 零件相对位置改变 " + count);
            }
          });
        });
        if (name === "waterTower") {
          var pad = current.filter(function (m) { return m.paving; })[0];
          current.forEach(function (m) {
            if (Math.abs(m.x - pad.x) > 6 || Math.abs(m.y - pad.y) > 3) {
              report.facilityIssues.push("水塔零件脱离底座");
            }
          });
        }
      });
    });
    report.upperUtilities = {};
    ["serviceQuarter", "energyQuarter"].forEach(function (name) {
      var group = baseSite.models.filter(function (m) { return m.facility === name; });
      var expanded = fx.buildIndustrialSite({ x: 0, y: 0 },
        Array.from({ length: 9 }, function (_, i) { return fakeFactory("outer-" + i); }));
      var moved = expanded.models.filter(function (m) { return m.facility === name; });
      report.upperUtilities[name] = {
        count: group.length,
        buildings: group.filter(function (m) { return m.kind === "box" && !m.paving; }).length,
        independent: group.every(function (m) { return m.bg && m.peripheral && !m.projectId; }),
        placement: group.every(function (m) {
          var anchor = m.facilityAnchor;
          return anchor.y >= -165 && anchor.y <= -130 &&
            (name === "serviceQuarter" ? anchor.x < 0 : anchor.x > 0);
        }),
        rigidMove: group.every(function (m, i) {
          var a = m.path ? m.path[0] : m, b = moved[i].path ? moved[i].path[0] : moved[i];
          return eqNum(b.x - a.x, m.facilityAnchor.x) && eqNum(b.y - a.y, m.facilityAnchor.y);
        })
      };
    });

    // ---- 厂外建筑（前场料场 / 罐区）：种类齐全、占地为正、贴着公路不越界 ----
    report.frontGroups = {};
    report.frontBuildings = {};
    report.frontRoads = {};
    report.frontPlacement = { "parts:stockyard": [], "parts:tankFarm": [] };
    ["stockyard", "tankFarm"].forEach(function (name) {
      var group = baseSite.models.filter(function (m) { return m.facility === name; });
      report.frontGroups[name] = {
        count: group.length,
        kinds: group.map(function (m) { return m.kind; }),
        // 两组直接落在沙地上：不许有自己的铺装平台（用户点名的「底部白色平台」）
        platforms: group.filter(function (m) { return !!m.paving; }).length,
        // 近景组：不标 bg（标了就成雾里的远景灰），但仍是 peripheral（参与边界与外移）
        near: group.every(function (m) { return !m.bg && m.peripheral && !m.projectId; }),
        // 位置按台地前缘公路算好（plateauAligned），不再按环数整体位移
        plateauAligned: group.every(function (m) { return !!m.plateauAligned; }),
        anchored: group.some(function (m) { return !!m.facilityAnchor; })
      };
      group.forEach(function (m) {
        var item = {
          group: name, kind: m.kind,
          hw: m.hw || 0, hh: m.hh || 0, r: m.r || 0,
          h: m.h || m.h0 || 0, rise: m.rise || 0,
          count: m.count || 0, gapX: m.gapX || 0,
          z0: m.z0 || 0, role: m.role || "",
          path: m.path ? m.path.length : 0
        };
        if (!own(report.frontBuildings, m.kind)) report.frontBuildings[m.kind] = [];
        report.frontBuildings[m.kind].push(item);
        report.frontPlacement["parts:" + name].push(item);
      });
    });
    // 两组都不许自带道路模型：场内道已按要求整条去掉（主线只有园区那条主干道）
    report.frontRoads.models = baseSite.models.filter(function (m) {
      return m.kind === "road";
    }).length;
    // 不同项目数下两组都得贴在公路外侧：带上台地尺寸与建筑占地，由 Python 侧算垂距
    [1, 4, 9].forEach(function (count) {
      var factories = Array.from({ length: count }, function (_, i) {
        return fakeFactory("place-" + i);
      });
      var site = fx.buildIndustrialSite({ x: 0, y: 0 }, factories);
      var pl = fx.makePlateau(factories);
      var groups = {};
      ["stockyard", "tankFarm"].forEach(function (name) {
        groups[name] = site.models.filter(function (m) {
          return m.facility === name;
        }).map(function (m) {
          return {
            kind: m.kind,
            x: m.x, y: m.y, hw: m.hw || m.r || 0,
            hh: m.hh || (m.r ? m.r * 0.5 : 0),
            // 轴系零件（煤棚 / 筒仓群）带「沿公路方向」的单位向量：
            // 贴路垂距的支撑长度要按纵深算（见 _near_road_distance）
            axis: m.axis ? [m.axis.x, m.axis.y] : null,
            path: m.path ? m.path.map(function (p) { return [p.x, p.y]; }) : null
          };
        });
      });
      report.frontPlacement[String(count)] = {
        plateau: { hw: pl.hw, hh: pl.hh },
        groups: groups
      };
    });


    (function () {
      var eps = 1e-9;
      function refPt(p, u, v, lift) {
        return { x: p.x + (u - v) * p.hw, y: p.y + (u + v - 1) * p.hh - (lift || 0) };
      }
      function refFaces(p) {
        var out = [];
        for (var i = 0; i < 3; i += 1) {
          var a = i / 3, b = (i + 1) / 3;
          out.push({
            side: "slope",
            base: [refPt(p, a, 0, 0), refPt(p, a, 1, 0)],
            baseIdx: [0, 1],
            points: [refPt(p, a, 0, 0), refPt(p, a, 1, 0), refPt(p, b, 1, p.rise), refPt(p, b, 0, p.rise)]
          });
          out.push({
            side: "gable",
            base: [refPt(p, a, 1, 0), refPt(p, b, 1, 0)],
            baseIdx: [0, 1],
            points: [refPt(p, a, 1, 0), refPt(p, b, 1, 0), refPt(p, b, 1, p.rise)]
          });
          out.push({
            side: "glazing",
            base: [refPt(p, b, 0, 0), refPt(p, b, 1, 0)],
            baseIdx: [0, 3],
            points: [refPt(p, b, 0, 0), refPt(p, b, 0, p.rise), refPt(p, b, 1, p.rise), refPt(p, b, 1, 0)]
          });
        }
        return out;
      }
      function near(a, b) {
        return Math.abs(a.x - b.x) <= eps && Math.abs(a.y - b.y) <= eps;
      }
      function edgesOf(points) {
        var edges = [];
        for (var i = 0; i < points.length; i += 1) {
          edges.push([points[i], points[(i + 1) % points.length]]);
        }
        return edges;
      }
      function sameEdge(e1, e2) {
        return (near(e1[0], e2[0]) && near(e1[1], e2[1])) ||
               (near(e1[0], e2[1]) && near(e1[1], e2[0]));
      }
      function diamondRatio(pt, p) {
        return Math.abs(pt.x - p.x) / p.hw + Math.abs(pt.y - p.y) / p.hh;
      }
      // 覆盖实际调用的整数像素尺寸与几个亚像素/大尺寸用例。
      var cases = [
        { x: 320, y: 240, hw: 26, hh: 13, rise: 5 },
        { x: 320, y: 240, hw: 120, hh: 60, rise: 18 },
        { x: -41.5, y: 88.25, hw: 63, hh: 31, rise: 9 },
        { x: 0, y: 0, hw: 2, hh: 1, rise: 2 }
      ];
      cases.forEach(function (p, ci) {
        var tag = "屋顶用例" + ci;
        var faces = fx.sawRoofFaces(p.x, p.y, p.hw, p.hh, p.rise);
        var ref = refFaces(p);
        report.roof.cases.push({
          faces: faces.length,
          sides: faces.map(function (f) { return f.side; }),
          basePoints: ref.reduce(function (acc, f) { return acc + f.base.length; }, 0)
        });
        if (faces.length !== 9) note(report.roof.issues, tag + " 面数 " + faces.length + "（应为 9）");
        var basePts = [];
        faces.forEach(function (face, fi) {
          var want = ref[fi];
          if (!want) return;
          if (face.side !== want.side) {
            note(report.roof.issues, tag + " 面" + fi + " side=" + face.side + "（应为 " + want.side + "）");
          }
          var pts = face.points || [];
          if (pts.length !== want.points.length) {
            note(report.roof.issues, tag + " 面" + fi + " 顶点数 " + pts.length +
                 "（应为 " + want.points.length + "）");
          }
          want.points.forEach(function (wp, pi) {
            var ap = pts[pi];
            if (!ap || !finite(ap.x) || !finite(ap.y)) {
              note(report.roof.issues, tag + " 面" + fi + " 顶点" + pi + " 非有限");
            } else if (!near(ap, wp)) {
              note(report.roof.issues, tag + " 面" + fi + " 顶点" + pi +
                   " 偏离设计坐标 (" + ap.x + "," + ap.y + ")");
            }
          });
          want.base.forEach(function (bp, bi) {
            // 底点必须落在菱形顶面的边界或内部：|dx|/hw + |dy|/hh <= 1
            if (diamondRatio(bp, p) > 1 + eps) {
              note(report.roof.issues, tag + " 面" + fi + " 底点" + bi + " 落在菱形顶面外");
            }
            basePts.push(bp);
          });
          want.baseIdx.forEach(function (bi) {
            var ap = pts[bi];
            if (!ap || !finite(ap.x) || !finite(ap.y)) return;
            if (diamondRatio(ap, p) > 1 + eps) {
              note(report.roof.issues, tag + " 面" + fi + " 实际底点" + bi + " 落在菱形顶面外");
            }
          });
        });
        // 每个抬升顶点都必须正好在某个底点正上方 rise 处，不能凭空漂移
        ref.forEach(function (face, fi) {
          face.points.forEach(function (pt) {
            var isBase = face.base.some(function (bp) { return near(bp, pt); });
            if (isBase) return;
            var anchored = basePts.some(function (bp) {
              return Math.abs(bp.x - pt.x) <= eps && Math.abs(bp.y - (pt.y + p.rise)) <= eps;
            });
            if (!anchored) note(report.roof.issues, tag + " 面" + fi + " 抬升点未对齐底点");
          });
        });
        // 面间无脱离：每个面至少与另一面共一条整边，且邻接图连通
        var edgesByFace = faces.map(function (f) { return edgesOf(f.points || []); });
        var adj = faces.map(function () { return []; });
        var linked = faces.map(function () { return 0; });
        for (var i = 0; i < faces.length; i += 1) {
          for (var j = i + 1; j < faces.length; j += 1) {
            var shared = edgesByFace[i].some(function (e1) {
              return edgesByFace[j].some(function (e2) { return sameEdge(e1, e2); });
            });
            if (shared) {
              adj[i].push(j);
              adj[j].push(i);
              linked[i] += 1;
              linked[j] += 1;
            }
          }
        }
        linked.forEach(function (n, fi) {
          // 每个面至少两条整边与邻面重合：坡面接山墙+立面，山墙接坡面+立面，立面接坡面+山墙
          if (n < 2) note(report.roof.issues, tag + " 面" + fi + " 与相邻面脱离（共边 " + n + " 条）");
        });
        var seen = {}, queue = [0], visited = 0;
        seen[0] = true;
        while (queue.length) {
          var cur = queue.pop();
          visited += 1;
          adj[cur].forEach(function (nxt) {
            if (!seen[nxt]) { seen[nxt] = true; queue.push(nxt); }
          });
        }
        if (faces.length && visited !== faces.length) {
          note(report.roof.issues, tag + " 面相接图不连通（" + visited + "/" + faces.length + "）");
        }
      });
    })();

    // 新绘图函数里引用的调色板键必须真的存在（拼错 stoneLight 之类在这里现形）。
    var newSource = [
      fx.drawCoolingTowerModel, fx.drawPipeRackModel, fx.drawTransferBridgeModel, fx.drawRackSupports,
      fx.drawCoalShedModel, fx.drawSiloBatteryModel, fx.drawGasholderModel,
      fx.drawBasinModel, fx.drawLoadingRackModel, fx.drawFlareModel
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

    // ---- 传送带切片：每段不超过 14 单位，沿总长插值高度，保留原路径拐角 ----
    (function () {
      function samePt(a, b) { return Math.abs(a.x - b.x) <= 1e-9 && Math.abs(a.y - b.y) <= 1e-9; }
      function issue(tag, msg) { note(report.slicing.issues, tag + "：" + msg); }
      function distToPath(pt, path) {
        var best = Infinity;
        for (var i = 1; i < path.length; i += 1) {
          var a = path[i - 1], b = path[i];
          var dx = b.x - a.x, dy = b.y - a.y;
          var len2 = dx * dx + dy * dy;
          var t = len2 ? Math.max(0, Math.min(1, ((pt.x - a.x) * dx + (pt.y - a.y) * dy) / len2)) : 0;
          best = Math.min(best, Math.hypot(pt.x - (a.x + t * dx), pt.y - (a.y + t * dy)));
        }
        return best;
      }
      var cases = [
        {
          tag: "折线带",
          model: {
            kind: "conveyor",
            path: [{ x: -12, y: 4 }, { x: 30, y: 14 }, { x: 30, y: 62 }, { x: -18, y: 62 }],
            h0: 6, h1: 24, width: 4, ore: true
          }
        },
        {
          tag: "水平带",
          model: { kind: "conveyor", path: [{ x: -100, y: 20 }, { x: 40, y: 20 }], h0: 12, h1: 12, width: 5 }
        }
      ];
      cases.forEach(function (c) {
        var src = c.model.path;
        var total = pathLen(src);
        var h0 = c.model.h0, h1 = c.model.h1;
        var expected = 0;
        for (var s = 1; s < src.length; s += 1) {
          var seg = Math.hypot(src[s].x - src[s - 1].x, src[s].y - src[s - 1].y);
          expected += Math.max(1, Math.ceil(seg / 14));
        }
        var items = fx.sceneDrawItems([c.model]);
        var pieces = items.map(function (it) { return it.m; });
        var info = { tag: c.tag, pieces: pieces.length, expectedPieces: expected, totalLength: total };
        report.slicing.cases.push(info);
        if (pieces.length !== expected) issue(c.tag, "切片数 " + pieces.length + "，期望 " + expected);
        if (!pieces.length) { issue(c.tag, "没有切出片段"); return; }
        // 绘制顺序按中点 y 排，这里按原路径首尾相接重新串起来。
        var ordered = [], used = [];
        var cursor = src[0];
        for (var step = 0; step < pieces.length; step += 1) {
          var found = -1;
          for (var pi = 0; pi < pieces.length; pi += 1) {
            if (used[pi]) continue;
            var pp = pieces[pi].path;
            if (pp && pp.length === 2 && samePt(pp[0], cursor)) { found = pi; break; }
          }
          if (found < 0) break;
          used[found] = true;
          ordered.push(pieces[found]);
          cursor = pieces[found].path[1];
        }
        if (ordered.length !== pieces.length) {
          issue(c.tag, "切片不能沿原路径首尾相接重建");
          return;
        }
        if (!samePt(ordered[0].path[0], src[0])) issue(c.tag, "首片段起点偏离原路径起点");
        if (!samePt(ordered[ordered.length - 1].path[1], src[src.length - 1])) {
          issue(c.tag, "末片段终点偏离原路径终点");
        }
        var acc = 0, sum = 0, maxLen = 0;
        ordered.forEach(function (piece, pi) {
          var a = piece.path[0], b = piece.path[1];
          if (!finite(a.x) || !finite(a.y) || !finite(b.x) || !finite(b.y)) {
            issue(c.tag, "片段 " + pi + " 坐标非有限");
            return;
          }
          var len = Math.hypot(b.x - a.x, b.y - a.y);
          if (len > 14 + 1e-9) issue(c.tag, "片段 " + pi + " 长 " + len + " 超过 14");
          if (!(len > 0)) issue(c.tag, "片段 " + pi + " 零长度");
          maxLen = Math.max(maxLen, len);
          sum += len;
          if (distToPath(a, src) > 1e-9 || distToPath(b, src) > 1e-9) {
            issue(c.tag, "片段 " + pi + " 脱离原路径");
          }
          if (typeof piece.support !== "boolean") issue(c.tag, "片段 " + pi + " support 不是布尔值");
          var wantH0 = total ? h0 + (h1 - h0) * (acc / total) : h0;
          if (!eqNum(piece.h0, wantH0)) {
            issue(c.tag, "片段 " + pi + " 起点高度 " + piece.h0 + "，按总长插值应为 " + wantH0);
          }
          acc += len;
          var wantH1 = total ? h0 + (h1 - h0) * (acc / total) : h0;
          if (!eqNum(piece.h1, wantH1)) {
            issue(c.tag, "片段 " + pi + " 终点高度 " + piece.h1 + "，按总长插值应为 " + wantH1);
          }
          if (pi > 0 && !eqNum(ordered[pi - 1].h1, piece.h0)) {
            issue(c.tag, "片段 " + pi + " 与上一段高度不连续");
          }
        });
        info.maxPieceLength = maxLen;
        info.sumLength = sum;
        info.firstH0 = ordered[0].h0;
        info.lastH1 = ordered[ordered.length - 1].h1;
        if (!eqNum(sum, total, 1e-6)) issue(c.tag, "切片总长 " + sum + "，原路径 " + total);
        if (!eqNum(ordered[0].h0, h0)) issue(c.tag, "首片段高度未接上模型 h0");
        if (!eqNum(ordered[ordered.length - 1].h1, h1)) issue(c.tag, "末片段高度未接上模型 h1");
        src.forEach(function (q, qi) {
          if (qi === 0 || qi === src.length - 1) return;
          var hit = ordered.some(function (piece) {
            return samePt(piece.path[0], q) || samePt(piece.path[1], q);
          });
          if (!hit) issue(c.tag, "拐角 (" + q.x + "," + q.y + ") 未落在切片边界");
        });
        // 切片只拷贝模型，不许改写原 conveyor
        if (c.model.path.length !== src.length) issue(c.tag, "切片改写了原模型 path");
        if (!eqNum(c.model.h0, h0) || !eqNum(c.model.h1, h1)) issue(c.tag, "切片改写了原模型高度");
      });
    })();

    // ---- 绘制顺序：铺装 → 背景 → 主体；主体 y 升序，同 xy 按 z0 升序 ----
    (function () {
      function issue(msg) { note(report.ordering.issues, msg); }
      var models = [
        { id: "z-high", kind: "box", x: 20, y: 8, z0: 12, hw: 6, hh: 3, h: 8 },
        { id: "main-front", kind: "box", x: 0, y: 400, z0: 0, hw: 6, hh: 3, h: 8 },
        { id: "paving-front", kind: "box", x: 0, y: 500, z0: 0, hw: 20, hh: 10, h: 2, paving: true },
        { id: "main-back", kind: "box", x: 0, y: -400, z0: 0, hw: 6, hh: 3, h: 8 },
        { id: "bg", kind: "box", x: 0, y: 100, z0: 0, hw: 6, hh: 3, h: 8, bg: true },
        { id: "z-low", kind: "box", x: 20, y: 8, z0: 3, hw: 6, hh: 3, h: 8 },
        { id: "paving-back", kind: "box", x: 0, y: -600, z0: 0, hw: 20, hh: 10, h: 2, paving: true },
        { id: "main-mid", kind: "box", x: 0, y: 3, z0: 0, hw: 6, hh: 3, h: 8 },
        {
          id: "belt", kind: "conveyor",
          path: [{ x: -40, y: 150 }, { x: -40, y: 170 }],
          h0: 8, h1: 12, width: 4
        }
      ];
      var items = fx.sceneDrawItems(models);
      var rows = items.map(function (it, idx) {
        return {
          idx: idx,
          id: it.m.id,
          kind: it.m.kind,
          layer: fx.drawLayer(it.m),
          d: it.d,
          z0: it.m.z0 || 0
        };
      });
      report.ordering.rows = rows;
      report.ordering.order = rows.map(function (r) { return r.id; });
      function rowOf(id) {
        for (var i = 0; i < rows.length; i += 1) if (rows[i].id === id) return rows[i];
        return null;
      }
      // 铺装在任意 y 都抢先：paving-front 的 y=500 远大于 main-back 的 -400
      rows.forEach(function (row) {
        if (row.layer !== 0) return;
        rows.forEach(function (other) {
          if (other.layer !== 0 && other.idx < row.idx) {
            issue("铺装 " + row.id + " 排在 " + other.id + " 之后（铺装必须先画）");
          }
        });
      });
      // 背景其次：bg 的 y=100 大于 main-back，仍要先画
      rows.forEach(function (row) {
        if (row.layer !== 1) return;
        rows.forEach(function (other) {
          if (other.layer === 2 && other.idx < row.idx) {
            issue("背景 " + row.id + " 排在主体 " + other.id + " 之后");
          }
        });
      });
      [0, 1, 2].forEach(function (layer) {
        var prev = null;
        rows.forEach(function (row) {
          if (row.layer !== layer) return;
          if (prev && row.d < prev.d - 1e-9) {
            issue("层 " + layer + " 内 y 排序倒退：" + prev.id + " → " + row.id);
          }
          prev = row;
        });
      });
      var zLow = rowOf("z-low"), zHigh = rowOf("z-high");
      if (!zLow || !zHigh) issue("同 xy 排序用例缺失");
      else if (!(zLow.idx < zHigh.idx)) {
        issue("相同 xy 未按 z0 升序：z-low(z0=3) 应在 z-high(z0=12) 之前");
      }
      var pavingBack = rowOf("paving-back"), pavingFront = rowOf("paving-front");
      if (!pavingBack || !pavingFront) issue("铺装排序用例缺失");
      else if (!(pavingBack.idx < pavingFront.idx)) issue("铺装未按 y 升序");
      var beltPieces = rows.filter(function (r) { return r.kind === "conveyor"; });
      if (beltPieces.length !== 2) {
        issue("排序用例里的传送带应切成 2 段，实际 " + beltPieces.length);
      } else {
        var mid = rowOf("main-mid"), front = rowOf("main-front");
        if (!(mid && front && mid.idx < beltPieces[0].idx && beltPieces[1].idx < front.idx)) {
          issue("切片未按中点 y 落进主体队形");
        }
        if (!(beltPieces[0].d <= beltPieces[1].d)) issue("切片未按中点 y 排序");
      }
      if (items.length !== 10) {
        issue("绘制项数量 " + items.length + "（应为 10：9 个模型 + 传送带多切 1 段）");
      }

      // 真实场景同样要满足层序与同层 y 单调。层号取自产品自己的 drawLayer：
      // 分层规则改了（比如新增「路面标线在铺装之上、立体件之下」这一层），这里跟着走。
      var worldItems = fx.sceneDrawItems(fx.S.world.models);
      var lastLayer = -1, lastLayerD = {};
      worldItems.forEach(function (it, idx) {
        var layer = fx.drawLayer(it.m);
        if (layer < lastLayer) issue("真实场景第 " + idx + " 项层序倒退");
        if (own(lastLayerD, layer) && it.d < lastLayerD[layer] - 1e-9) {
          issue("真实场景第 " + idx + " 项同层 y 倒退");
        }
        lastLayerD[layer] = it.d;
        lastLayer = layer;
      });
    })();

    // bounds / fitCamera：0/1/4/7 个项目都检查（7 个会跨到第二环）。
    [0, 1, 2, 4, 7].forEach(function (n) {
      fx.buildWorld({ projects: makeProjects(n), counters: {} });
      fx.S.bufW = 900;
      fx.S.bufH = 500;
      fx.S.cssW = 900;
      fx.S.cssH = 500;
      fx.fitCamera(false);
      var b = fx.S.world.bounds;
      var cam = fx.S.cam;
      var horizon = fx.sceneHorizon();
      fx.S.world.models.forEach(function (m) {
        if (!m.peripheral || (!m.h && !m.h0 && !m.h1) || m.paving) return;
        var height = (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0));
        var depth = m.hh || (m.r ? m.r * 0.5 : 5);
        (m.path || [m]).forEach(function (p) {
          var roofY = (p.y - height - depth - cam.y) * cam.zoom + fx.S.bufH / 2;
          if (roofY < horizon + 13) note(report.cameraIssues, "外围建筑压入天际线 " + n);
        });
      });
      [b.minX, b.maxX, b.minY, b.maxY, cam.x, cam.y, cam.zoom].forEach(function (value) {
        if (!finite(value)) note(report.cameraIssues, n + " 项目时 bounds/cam 出现非有限值: " + value);
      });
      if (!(b.minX < b.maxX) || !(b.minY < b.maxY)) {
        note(report.cameraIssues, n + " 项目时 bounds 不成区间: " + JSON.stringify(b));
      }
      var cx = (b.minX + b.maxX) / 2;
      var landOffset = Math.max(0, Math.min(fx.S.bufH * 0.11,
        (fx.S.bufH - (b.maxY - b.minY) * cam.zoom) / 2 - 16));
      var cy = (b.minY + b.maxY) / 2 - landOffset / cam.zoom;
      if (Math.abs(cam.x - cx) > 1e-6 || Math.abs(cam.y - cy) > 1e-6) {
        note(report.cameraIssues, n + " 项目时镜头中心未按取景公式对齐");
      }
      if (!(cam.zoom >= 0.01 && cam.zoom <= 1.45)) {
        note(report.cameraIssues, n + " 项目时 zoom 越界: " + cam.zoom);
      }
      fx.S.world.models.forEach(function (m) {
        if (m.bg) return;
        var points = m.path || [{ x: m.x, y: m.y }];
        var hw = m.hw || m.r || m.width || 8;
        var hh = m.hh || (m.r ? m.r * 0.5 : 5);
        var height = (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0));
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
        if (sx < pad - 2 || sx > fx.S.bufW - pad + 2 || sy < pad - 2 || sy > fx.S.bufH - 14) {
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
        if (!bridges.some(function (m) { return m.bg; })) {
          note(report.cameraIssues, "远景转运桥缺失或未标记 bg");
        }
      }
    });
    report.gridIssues = [];
    var fullGrid = fx.gridSlots(25);
    [0, 1, 3, 8, 9, 25].forEach(function (count) {
      var slots = fx.gridSlots(count), seen = {};
      fx.buildWorld({ projects: makeProjects(count), counters: {} });
      var world = fx.S.world, platform = world.plateau;
      if (world.hall.x !== 0 || world.hall.y !== 0 || platform.cx !== 0 || platform.cy !== 0) {
        report.gridIssues.push("hall/platform not centered: " + count);
      }
      slots.forEach(function (cell, i) {
        var key = cell.gx + "," + cell.gy, f = world.factories[i];
        if (seen[key] || key === "0,0") report.gridIssues.push("duplicate/center cell: " + key);
        seen[key] = true;
        if (i < 8 && Math.max(Math.abs(cell.gx), Math.abs(cell.gy)) !== 1) {
          report.gridIssues.push("not in 3x3 perimeter");
        }
        if (JSON.stringify(cell) !== JSON.stringify(fullGrid[i])) {
          report.gridIssues.push("existing slot moved");
        }
        if (f.x !== (cell.gx - cell.gy) * fx.GRID_X / 2 || f.y !== (cell.gx + cell.gy) * fx.GRID_Y / 2) {
          report.gridIssues.push("projection mismatch");
        }
        var corners = [[f.x - f.halfW, f.y], [f.x + f.halfW, f.y],
                       [f.x, f.y - f.halfH], [f.x, f.y + f.halfH]];
        corners.forEach(function (p) {
          if (Math.abs(p[0]) / platform.hw + Math.abs(p[1]) / platform.hh > 1.000001) {
            report.gridIssues.push("factory outside platform");
          }
        });
      });
    });
    report.scaleSizes = [];
    fx.dom.stage = {
      clientWidth: 1000, clientHeight: 700,
      getBoundingClientRect: function () { return { width: 900, height: 630 }; }
    };
    fx.dom.canvas = { width: 1, height: 1, getContext: function () { return makeCtx(); } };
    fx.S.mounted = false;
    fx.resizeScene(false);
    report.scaleSizes = [fx.S.cssW, fx.S.cssH, fx.S.bufW, fx.S.bufH];
    var cameraBefore = [fx.S.cam.x, fx.S.cam.y, fx.S.cam.tx, fx.S.cam.ty];
    fx.onPointerDown({ button: 0, pointerId: 1, clientX: 100, clientY: 100 });
    fx.onPointerMove({ pointerId: 1, clientX: 420, clientY: 310 });
    fx.onPointerUp({ pointerId: 1, clientX: 420, clientY: 310 });
    var cameraAfter = [fx.S.cam.x, fx.S.cam.y, fx.S.cam.tx, fx.S.cam.ty];
    var zoomBefore = fx.S.cam.tz, prevented = 0;
    fx.onWheel({ deltaY: -100, preventDefault: function () { prevented += 1; } });
    var zoomIn = fx.S.cam.tz;
    fx.onWheel({ deltaY: 100, preventDefault: function () { prevented += 1; } });
    report.fixedCamera = {
      before: cameraBefore, after: cameraAfter, prevented: prevented,
      zoomLocked: zoomIn === zoomBefore && fx.S.cam.tz === zoomBefore,
      centered: fx.S.cam.x === (fx.S.world.bounds.minX + fx.S.world.bounds.maxX) / 2 &&
                fx.S.cam.y <= (fx.S.world.bounds.minY + fx.S.world.bounds.maxY) / 2,
      afterWheel: [fx.S.cam.x, fx.S.cam.y, fx.S.cam.tx, fx.S.cam.ty]
    };
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


def test_factory_scene_hidden_grid_keeps_center_and_stable_perimeter(scene_report):
    assert scene_report["gridIssues"] == [], scene_report["gridIssues"]


def test_factory_scene_camera_stays_centered_without_mouse_zoom_or_pan(scene_report):
    camera = scene_report["fixedCamera"]
    assert camera["before"] == camera["after"] == camera["afterWheel"]
    assert camera["zoomLocked"] and camera["centered"]
    assert camera["prevented"] == 2


def test_factory_scene_labels_use_unscaled_css_dimensions(scene_report):
    """全局缩放为 0.9 时，标签与缓冲仍用逻辑尺寸而非 rect 物理尺寸。"""
    assert scene_report["scaleSizes"] == [1000, 700, 500, 350]


def test_factory_scene_storage_tanks_leave_the_front_road_clear(scene_report):
    tanks = {tank["side"]: tank for tank in scene_report["roadsideTanks"]}
    assert set(tanks) == {-1, 1}
    assert (tanks[-1]["x"], tanks[-1]["y"]) == (-300, 110)
    assert (tanks[1]["x"], tanks[1]["y"]) == (320, 95)
    for tank in tanks.values():
        road_y = 188 * (1 - abs(tank["x"]) / 350)
        assert tank["y"] - tank["h"] - tank["r"] * 0.5 > road_y + 8
        assert tank["bg"]


def test_factory_scene_facility_parts_keep_rigid_offsets_across_project_counts(scene_report):
    assert scene_report["facilityIssues"] == [], scene_report["facilityIssues"]


def test_factory_scene_upper_utilities_stay_independent_and_move_together(scene_report):
    for group in scene_report["upperUtilities"].values():
        assert group["count"] >= 8
        assert group["buildings"] >= 3
        assert group["independent"]
        assert group["placement"]
        assert group["rigidMove"]


def test_factory_scene_front_groups_are_near_and_move_with_the_road(scene_report):
    """前场料场 / 罐区：满色近景、直接落在沙地上（无平台）、位置按台地前缘公路算。"""
    groups = scene_report["frontGroups"]
    assert set(groups) == {"stockyard", "tankFarm"}, f"前场建筑群不齐：{sorted(groups)}"
    for name, info in groups.items():
        assert info["platforms"] == 0, (
            f"{name} 不该有自己的混凝土平台（实际 {info['platforms']} 块铺装）"
        )
        # 不标 bg 才是近景：bg 的零件会被 roleCols / 雾化调色板压成远景灰
        assert info["near"], f"{name} 的零件应是不标 bg 的近景件"
        # 贴路布置：位置按台地算，不能再叠一层按环数的位移（否则会飘离公路）
        assert info["plateauAligned"], f"{name} 的零件应标 plateauAligned"
        assert not info["anchored"], f"{name} 不该同时留 facilityAnchor（会双重位移）"
        assert info["count"] >= 8, f"{name} 零件太少：{info['count']}"


def _road_arm(plateau: dict, side: int) -> tuple[tuple[float, float], tuple[float, float]]:
    """公路前缘左/右臂的两个端点（与 drawTerrain 里的取值一致）。"""
    hw, hh = plateau["hw"], plateau["hh"]
    return (side * (hw + 14), 0.0), (0.0, hh + 8)


def _seg_intersect(p1, p2, p3, p4) -> bool:
    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1, d2 = cross(p3, p4, p1), cross(p3, p4, p2)
    d3, d4 = cross(p1, p2, p3), cross(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _near_road_distance(parts, plateau: dict, side: int) -> float:
    """一组建筑沿外法线到公路中线的最近距离（负=已经压上公路）。"""
    hw, hh = plateau["hw"], plateau["hh"]
    length = math.hypot(hw + 14, hh + 8)
    nx, ny = side * (hh + 8) / length, (hw + 14) / length
    ax, ay = _road_arm(plateau, side)[0]
    near = math.inf
    for part in parts:
        pts = part["path"] or [[part["x"], part["y"]]]
        # 轴系零件（煤棚 / 筒仓群）的纵深本来就沿外法线摆，支撑长度就是 hh；轴对齐零件
        # 要按两条轴在外法线上的投影合成——与 app-factory.js 的 alignYardToRoad 同一口径
        if part.get("axis"):
            extent = abs(part["hh"])
        else:
            extent = abs(part["hw"] * nx) + abs(part["hh"] * ny)
        for px, py in pts:
            near = min(near, (px - ax) * nx + (py - ay) * ny - extent)
    return near


def test_factory_scene_front_groups_hug_the_road_at_any_project_count(scene_report):
    """项目数变化时台地（连同公路）会变大，两组必须跟着挪，始终贴着公路外侧。

    「贴着」按最近那面墙到公路中线的距离衡量：落在 4~40 单位这条窄带里——太近会压上
    公路，太远就回到「摆在沙地中间」的老样子。同时整组必须在台地之外，不压园区坪。
    """
    placement = scene_report["frontPlacement"]
    counts = sorted(k for k in placement if not k.startswith("parts:"))
    assert counts == ["1", "4", "9"], f"没跑到全部项目数：{counts}"
    for count in counts:
        info = placement[count]
        hw, hh = info["plateau"]["hw"], info["plateau"]["hh"]
        for name, side in (("stockyard", -1), ("tankFarm", 1)):
            parts = info["groups"].get(name)
            assert parts, f"{count} 个项目时 {name} 没有零件"
            near = _near_road_distance(parts, info["plateau"], side)
            assert 4 <= near <= 40, (
                f"{count} 个项目时 {name} 最近那面墙离公路 {near:.1f}，应落在 4~40"
            )
            # 整组都在台地之外（任何零件都不许压回园区坪）
            for part in parts:
                for px, py in (part["path"] or [[part["x"], part["y"]]]):
                    assert abs(px) / hw + abs(py) / hh > 0.9, (
                        f"{count} 个项目时 {name} 有零件压回台地内：({px:.0f},{py:.0f})"
                    )


def test_factory_scene_front_stockyard_ranks_run_along_the_road(scene_report):
    """料场整组顺着公路排、最高的筒仓群退到最远的一排。

    老版把「纵深」当成世界 y 轴，而前缘公路是斜的：摆出来整排与马路不平行，最高的筒仓
    反而落到最靠路的一侧。这里锁两点——煤棚与筒仓群都按公路轴向摆（轴向量就是公路方向），
    且筒仓群的贴路垂距明显大于煤棚（高体量在后、装卸配套在前）。
    """
    info = scene_report["frontPlacement"]["1"]
    plateau = info["plateau"]
    length = math.hypot(plateau["hw"] + 14, plateau["hh"] + 8)
    along = ((plateau["hw"] + 14) / length, (plateau["hh"] + 8) / length)
    parts = info["groups"]["stockyard"]
    axis_parts = [p for p in parts if p.get("axis")]
    assert len(axis_parts) >= 2, "料场主体（煤棚 / 筒仓群）都该按公路轴向摆"
    assert "fence" in [p["kind"] for p in parts], "料场该有矮围栏把三排圈成一组"
    for part in axis_parts:
        ax, ay = part["axis"]
        assert abs(ax * along[0] + ay * along[1] - 1) < 1e-9, (
            f"{part['kind']} 的轴不是公路方向：{(ax, ay)}"
        )
    by_kind = {}
    for part in parts:
        by_kind.setdefault(part["kind"], part)
    silo = _near_road_distance([by_kind["siloBattery"]], plateau, -1)
    shed = _near_road_distance([by_kind["coalShed"]], plateau, -1)
    assert silo > shed + 20, (
        f"最高的筒仓群应退到煤棚之后（远离公路那侧）：筒仓 {silo:.1f} / 煤棚 {shed:.1f}"
    )


def test_factory_scene_stockyard_yard_is_furnished(scene_report):
    """料场围栏内要有场院内容：煤堆、地磅、排队卡车与两盏投光，不能只剩大棚和仓。"""
    parts = scene_report["frontPlacement"]["1"]["groups"]["stockyard"]
    kinds = [p["kind"] for p in parts]
    assert kinds.count("coalPile") >= 2, f"露天煤堆不足：{kinds}"
    assert "weighbridge" in kinds, f"料场缺少地磅：{kinds}"
    assert kinds.count("truck") >= 3, f"地磅与排队卡车不足：{kinds}"
    assert kinds.count("floodlight") >= 2, f"料场缺少冷白投光：{kinds}"


def test_factory_scene_front_yards_have_no_roads(scene_report):
    """两组前场都不许自带道路：那条场内道已按要求整条去掉（只剩园区主干道）。"""
    assert scene_report["frontRoads"]["models"] == 0, "前场又长出了 kind=road 的模型"


def test_factory_scene_front_buildings_are_present_and_sized(scene_report):
    """六种厂外建筑都真的进了场景，且占地 / 高度是正数（写错 kind 会整件消失）。"""
    found = scene_report["frontBuildings"]
    for kind in _FRONT_BUILDING_KINDS:
        assert kind in found, f"厂外建筑 {kind} 没进场景（分派或摆位丢了）"
        for item in found[kind]:
            assert item["group"] in _FRONT_GROUPS, f"{kind} 挂在不认识的建筑群里"
            assert item["z0"] == 0, f"{kind} 直接落在沙地上，基准高度应为 0：{item['z0']}"
            assert item["h"] > 0, f"{kind} 高度应为正：{item['h']}"
            # 占地：圆形件用 r，矩形件用 hw/hh
            assert item["r"] > 0 or (item["hw"] > 0 and item["hh"] > 0), f"{kind} 占地缺失"
    # 形状参数（写漏会让模型退化成一根杆 / 一块板）
    for item in found["siloBattery"]:
        assert item["count"] >= 3 and item["gapX"] > 0, "筒仓群至少三仓且仓距为正"
    for item in found["coalShed"]:
        assert item["rise"] > 0, "煤棚屋盖要有举高（rise）"
    for item in found["flare"]:
        assert item["h"] >= 40, "火炬塔要明显高于其它建筑，才起纵向坐标作用"
    for item in found["basin"]:
        assert item["hw"] >= 20 and item["hh"] >= 8, "循环水池要够大才看得出是水池"
    for item in found["loadingRack"]:
        assert item["hw"] >= 16 and item["hh"] >= 6, "装车台要能罩住一辆罐车"
    for kind in ("siloBattery", "gasholder", "basin", "loadingRack"):
        assert found[kind], f"{kind} 只登记了形状，没登记到任何零件"


def test_factory_scene_compounds_have_distinct_ground_layouts(scene_report):
    compounds = scene_report["compounds"]
    assert len({item["style"] for item in compounds.values()}) == 8
    assert len({item["signature"] for item in compounds.values()}) == 8
    assert scene_report["compoundIssues"] == [], scene_report["compoundIssues"]
    assert [compounds[str(v)]["tanks"] for v in range(8)] == [1, 0, 0, 0, 0, 0, 0, 4]
    assert compounds["0"]["chimneys"] == 2
    assert compounds["1"]["containers"] == 4
    for variant in ("4", "5", "6"):
        assert compounds[variant]["chimneys"] == 0
        assert compounds[variant]["containers"] == 0


def test_factory_scene_process_models_belong_to_matching_compounds(scene_report):
    compounds = scene_report["compounds"]
    assert [m["kind"] for m in compounds["0"]["processModels"]] == ["furnace"]
    columns = compounds["7"]["processModels"]
    assert [m["kind"] for m in columns] == ["distillation", "distillation"]
    assert len({m["h"] for m in columns}) == 2
    assert all(m["platforms"] == 3 for m in columns)
    for variant in ("1", "2", "3", "4", "5", "6"):
        assert compounds[variant]["processModels"] == []
    assert scene_report["compoundIssues"] == []


def test_factory_scene_builds_all_eight_plot_variants(scene_report):
    """八种厂区变体都要真实构建出来，而非仅有源码分支。"""
    assert scene_report["variantHeights"] == _VARIANT_MAIN_HEIGHTS, (
        f"八种厂区未全部覆盖或主厂房高度变化：{scene_report['variantHeights']}"
        f"（id: {scene_report['variantIds']}）"
    )
    for variant in ("0", "1", "2", "3", "4", "5", "6", "7"):
        counts = scene_report["variantNewKinds"][variant]
        assert set(counts) == {"cooling", "pipeRack", "transferBridge"}, (
            f"厂区变体 {variant} 的新增公用设施种类不全：{counts}"
        )
        # 具体数量可以随布景调整，但每种公用设施都必须真实出现。
        for kind, count in counts.items():
            assert count >= 1, f"厂区变体 {variant} 缺少 {kind}（{counts}）"


def test_factory_scene_new_models_have_finite_coordinates_and_paths(scene_report):
    """模型坐标全部有限；三件新设备的 path / 高度 / 宽度语义合理。"""
    assert scene_report["modelIssues"] == [], (
        "模型出现 NaN/Infinity 或坏 path：" + "; ".join(scene_report["modelIssues"])
    )
    g = scene_report["geometry"]

    cooling = g["cooling"]
    assert cooling["count"] >= 1, "至少应有一座冷却塔"
    assert all(r > 0 for r in cooling["radii"]), f"冷却塔半径应为正：{cooling['radii']}"
    assert all(h > 0 for h in cooling["heights"]), f"冷却塔高度应为正：{cooling['heights']}"
    assert all(z >= 0 for z in cooling["z0s"]), f"冷却塔基准高度应为非负：{cooling['z0s']}"
    assert all(len(pt) == 2 for pt in cooling["points"]), "冷却塔坐标应是 [x, y]"

    pipe_rack = g["pipeRack"]
    assert pipe_rack["count"] >= 1, "至少应有一组管廊"
    assert all(w > 0 for w in pipe_rack["widths"]), f"管廊宽度应为正：{pipe_rack['widths']}"
    assert all(h0 > 0 and h1 > 0 for h0, h1 in pipe_rack["heights"]), (
        f"管廊两端高度应为正：{pipe_rack['heights']}"
    )
    assert all(n >= 2 for n in pipe_rack["pathPoints"]), f"管廊 path 至少两点：{pipe_rack['pathPoints']}"
    assert all(length > 0 for length in pipe_rack["pathLengths"]), (
        f"管廊 path 不能为零长度：{pipe_rack['pathLengths']}"
    )

    bridge = g["transferBridge"]
    assert bridge["count"] >= 1, "至少应有一座转运桥"
    assert all(w > 0 for w in bridge["widths"]), f"转运桥宽度应为正：{bridge['widths']}"
    assert all(h0 > 0 and h1 > 0 for h0, h1 in bridge["heights"]), (
        f"转运桥两端高度应为正：{bridge['heights']}"
    )
    assert all(n >= 2 for n in bridge["pathPoints"]), f"转运桥 path 至少两点：{bridge['pathPoints']}"
    assert all(length > 0 for length in bridge["pathLengths"]), (
        f"转运桥 path 不能为零长度：{bridge['pathLengths']}"
    )
    assert any(bridge["bgFlags"]), "远景转运桥必须标记 bg（否则会被当成厂区主体参与排序）"


def test_factory_scene_saw_roof_faces_sit_on_top_face(scene_report):
    """锯齿屋顶：9 个面共边相连，全部底点落在菱形顶面边界/内部。"""
    roof = scene_report["roof"]
    assert roof["cases"], "没有跑任何屋顶用例"
    assert roof["issues"] == [], "锯齿屋顶几何问题：" + "; ".join(roof["issues"])
    for case in roof["cases"]:
        assert case["faces"] == 9, f"锯齿屋顶应返回 9 个面：{case}"
        assert case["sides"] == ["slope", "gable", "glazing"] * 3, (
            f"锯齿屋顶面序异常：{case['sides']}"
        )
        assert case["basePoints"] == 18, "每段坡面/山墙/采光立面各有 2 个底点"


def test_factory_scene_conveyor_slices_keep_path_and_height(scene_report):
    """传送带切片：保留原路径拐角，切片总长不变，高度沿总长连续。"""
    slicing = scene_report["slicing"]
    assert slicing["issues"] == [], "传送带切片问题：" + "; ".join(slicing["issues"])
    assert len(slicing["cases"]) == 2
    for case in slicing["cases"]:
        assert case["pieces"] == case["expectedPieces"] >= 1, f"切片数量异常：{case}"
        assert case["maxPieceLength"] <= 14 + 1e-9, f"切片超过 14 单位：{case}"
        assert abs(case["sumLength"] - case["totalLength"]) <= 1e-6, f"切片总长不一致：{case}"


def test_factory_scene_draw_order_paving_background_then_depth(scene_report):
    """绘制顺序：铺装先画、背景其次、主体按 y 与 z0 排序。"""
    ordering = scene_report["ordering"]
    assert ordering["issues"] == [], "绘制顺序问题：" + "; ".join(ordering["issues"])
    order = ordering["order"]
    assert len(order) == 10, f"绘制项数量异常：{order}"
    assert set(order[:2]) == {"paving-back", "paving-front"}, (
        f"铺装没有排在最前（不论 y）：{order}"
    )
    assert order.index("bg") < order.index("main-back"), (
        f"背景没有排在主体前（不论 y）：{order}"
    )
    # 相同 xy：z0 小的先画；输入顺序里 z-high 在前，必须被重排
    assert order.index("z-low") < order.index("z-high"), f"同 xy 未按 z0 排序：{order}"
    # 传送带切片按中点 y 参与主体排序
    belt = [i for i, item in enumerate(ordering["rows"]) if item["kind"] == "conveyor"]
    assert len(belt) == 2, f"传送带切片数量异常：{ordering['rows']}"


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
    # 4 个项目时：台地后缘（含冷却塔占地）与两翼厂区坪都要在取景范围内。
    # 环 0 的四个厂区落在 ±45° 槽位，翼侧外缘约 x=±203，bounds 还要带取景余量。
    assert bounds["minY"] <= -141.0, f"bounds.minY 没有覆盖台地后缘: {bounds}"
    assert bounds["minX"] <= -200.0 and bounds["maxX"] >= 200.0, (
        f"bounds 没有覆盖两翼厂区: {bounds}"
    )
