/* ============================================================
 * 工厂绘制层：场景的全部 Canvas 绘制（自 app-factory.js 拆出，零行为变化）。
 *
 * 零构建普通 script，在 app-factory.js 之后加载：共享状态 / 常量与业务函数
 * 经 window.FX 命名空间绑定（下方 var 均为活引用或加载时已就位的函数对象）；
 * 本文件向 FX 导出 render / viewPoint 等绘制入口，主文件经转发函数调用。
 * ============================================================ */
(function () {
  "use strict";

  var FX = window.FX;
  // 共享状态与常量（活引用：属性读写落在主文件创建的同一对象上）
  var S = FX.S, dom = FX.dom;
  var PAL_DAY = FX.PAL_DAY, PAL_NIGHT = FX.PAL_NIGHT;
  var PIPELINE_MAX_NODES = FX.PIPELINE_MAX_NODES;
  // 主文件的业务函数（先加载方已在 IIFE 尾部挂 FX，绑定即有效）
  var activeConfig = FX.activeConfig, activeState = FX.activeState;
  var hashKey = FX.hashKey, lineStall = FX.lineStall, clamp = FX.clamp;
  var pathLength = FX.pathLength, pointAt = FX.pointAt, ringPts = FX.ringPts;
  var drawBerths = FX.drawBerths, drawPackages = FX.drawPackages, drawUpcoming = FX.drawUpcoming;
  var collectPackageItems = FX.collectPackageItems;   // 实时包裹进动态画序
  var refreshFactoryAlerts = FX.refreshFactoryAlerts, refreshUpcoming = FX.refreshUpcoming;
  var refreshFactoryMood = FX.refreshFactoryMood;
  var updateLabels = FX.updateLabels;
  // 动态对象位姿（scene 层纯函数；scene 先于本文件加载，绑定即有效）
  var workerPose = FX.workerPose, tripPose = FX.tripPose, forkliftPose = FX.forkliftPose;
  var forkliftDutyPose = FX.forkliftDutyPose;
  // 装卸几何常量（scene 层导出）：叉臂伸出量、厂坪高度
  var FORK_REACH = FX.FORK_REACH, FORK_LIFT = FX.FORK_LIFT, YARD_Z = FX.YARD_Z;
  // 「痕迹」口径与厂区今日统计：成品码几箱、返工堆多高、沙漏看多远
  var nowMs = FX.nowMs, statFor = FX.statFor;
  var PALLET_SIZE = FX.PALLET_SIZE, PALLET_MAX = FX.PALLET_MAX;
  var REWORK_MAX = FX.REWORK_MAX, UPCOMING_GLYPH_S = FX.UPCOMING_GLYPH_S;


  /** 天气种类：只有开启且拿到 ok 快照才参与绘制；关闭 / 失败按晴天处理。 */
  function weatherKind() {
    var cfg = activeConfig();
    if (!cfg || cfg.weather !== true || S.demo) return "clear";
    var w = S.weather;
    if (!w || !w.ok) return "clear";
    return String(w.kind || "unknown");
  }

  /** 昼光系数：1 = 全昼（昼盘），0 = 全夜（夜盘），过渡带内为 0..1 的插值权重。

      过渡带以日出 / 日落为中心各 ±40 分钟：开现实天气且有日出日落时刻时按当地
      时刻，否则按本机 06:00 / 18:00；天气只有 is_day 布尔时没有时刻可插值，仍
      硬切。ambient 关闭恒为昼（与「场景不跟主题明暗」一致）。可传 now 便于测试。 */
  var DAYLIGHT_BAND_MIN = 40;   // 过渡带半宽（分钟）：日出日落各一段 80 分钟过渡

  function parseHHMM(s) {
    var m = /^(\d{1,2}):(\d{2})$/.exec(String(s || ""));
    return m ? (+m[1] + (+m[2]) / 60) : null;
  }

  function daylightFactor(now) {
    var cfg = activeConfig();
    if (cfg.ambient === false) return 1;
    var d = now || new Date();
    var h = d.getHours() + d.getMinutes() / 60;
    var sr = 6, ss = 18;
    var w = (cfg.weather && S.weather && S.weather.ok) ? S.weather : null;
    if (w) {
      var psr = parseHHMM(w.sunrise), pss = parseHHMM(w.sunset);
      if (psr != null && pss != null) { sr = psr; ss = pss; }
      else if (typeof w.is_day === "boolean") return w.is_day ? 1 : 0;
    }
    var hw = DAYLIGHT_BAND_MIN / 60;
    if (h < 12) return clamp((h - (sr - hw)) / (2 * hw), 0, 1);   // 日出：夜→昼
    return 1 - clamp((h - (ss - hw)) / (2 * hw), 0, 1);           // 日落：昼→夜
  }

  function parseColor(c) {
    if (c[0] === "#") {
      var n = parseInt(c.slice(1), 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1];
    }
    var m = /rgba?\(([^)]+)\)/.exec(c);
    var p = m[1].split(",").map(Number);
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1];
  }

  function lerpColor(a, b, t) {
    var ca = parseColor(a), cb = parseColor(b);
    var r = Math.round(ca[0] + (cb[0] - ca[0]) * t);
    var g = Math.round(ca[1] + (cb[1] - ca[1]) * t);
    var bl = Math.round(ca[2] + (cb[2] - ca[2]) * t);
    var al = ca[3] + (cb[3] - ca[3]) * t;
    return al >= 0.999 ? "rgb(" + r + "," + g + "," + bl + ")"
      : "rgba(" + r + "," + g + "," + bl + "," + Math.round(al * 1000) / 1000 + ")";
  }

  function palette() {
    var f = daylightFactor();
    S.daylightF = f;
    if (f >= 1) return PAL_DAY;
    if (f <= 0) return PAL_NIGHT;
    // 过渡带：按 1% 量化缓存（一分钟最多走一两格），不每帧重建 90 字段的对象
    var q = Math.round(f * 100);
    if (S.palCache && S.palCache.q === q) return S.palCache.pal;
    var pal = {};
    for (var k in PAL_DAY) pal[k] = lerpColor(PAL_NIGHT[k], PAL_DAY[k], f);
    S.palCache = { q: q, pal: pal };
    return pal;
  }

  // ---------- 绘制基础 ----------
  function fillDiamond(ctx, cx, cy, hw, hh, color) {
    cx = Math.round(cx); cy = Math.round(cy);
    hw = Math.max(1, Math.round(hw)); hh = Math.max(1, Math.round(hh));
    ctx.fillStyle = color;
    for (var dy = -hh; dy < hh; dy += 1) {
      var t = 1 - Math.abs(dy + 0.5) / hh;
      var w = Math.max(1, Math.round(hw * t));
      ctx.fillRect(cx - w, cy + dy, w * 2, 1);
    }
  }

  function fillCircle(ctx, cx, cy, r, color) {
    cx = Math.round(cx); cy = Math.round(cy);
    r = Math.max(1, Math.round(r));
    ctx.fillStyle = color;
    for (var dy = -r; dy <= r; dy += 1) {
      var w = Math.round(Math.sqrt(Math.max(0, r * r - dy * dy)));
      ctx.fillRect(cx - w, cy + dy, Math.max(1, w * 2), 1);
    }
  }

  // 像素椭圆：逐行扫，边缘保持硬像素
  function fillOval(ctx, cx, cy, rx, ry, color) {
    cx = Math.round(cx); cy = Math.round(cy);
    rx = Math.max(1, Math.round(rx)); ry = Math.max(1, Math.round(ry));
    ctx.fillStyle = color;
    for (var dy = -ry; dy <= ry; dy += 1) {
      var t = 1 - (dy * dy) / (ry * ry);
      if (t <= 0) continue;
      var w = Math.max(1, Math.round(rx * Math.sqrt(t)));
      ctx.fillRect(cx - w, cy + dy, w * 2, 1);
    }
  }

  // 像素多边形（屋顶、树冠三角）
  function fillPoly(ctx, pts, color) {
    if (!pts || pts.length < 3) return;
    ctx.fillStyle = color;
    var minY = Infinity, maxY = -Infinity;
    for (var i = 0; i < pts.length; i += 1) {
      minY = Math.min(minY, pts[i].y);
      maxY = Math.max(maxY, pts[i].y);
    }
    for (var y = Math.floor(minY); y <= Math.ceil(maxY); y += 1) {
      var xs = [];
      for (var j = 0; j < pts.length; j += 1) {
        var a = pts[j], b = pts[(j + 1) % pts.length];
        if ((y >= a.y && y < b.y) || (y >= b.y && y < a.y)) {
          xs.push(a.x + (b.x - a.x) * (y - a.y) / (b.y - a.y));
        }
      }
      xs.sort(function (p, q) { return p - q; });
      for (var k = 0; k + 1 < xs.length; k += 2) {
        var x0 = Math.round(xs[k]), x1 = Math.round(xs[k + 1]);
        ctx.fillRect(x0, y, Math.max(1, x1 - x0), 1);
      }
    }
  }

  function seeded(seed) {
    // 装饰布局用确定性伪随机（非安全用途）：每次刷新同一座工厂长得一样
    var s = seed >>> 0;
    return function () {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
  }

  function distToSeg(px, py, ax, ay, bx, by) {
    var dx = bx - ax, dy = by - ay;
    var len2 = dx * dx + dy * dy;
    if (!len2) return Math.hypot(px - ax, py - ay);
    var t = clamp(((px - ax) * dx + (py - ay) * dy) / len2, 0, 1);
    return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
  }

  function linePath(ctx, pts, width, color) {
    if (!pts || pts.length < 2) return;
    ctx.fillStyle = color;
    var w = Math.max(1, Math.round(width));
    for (var i = 1; i < pts.length; i += 1) {
      var a = pts[i - 1], b = pts[i];
      var steps = Math.max(1, Math.round(Math.hypot(b.x - a.x, b.y - a.y)));
      for (var s = 0; s <= steps; s += 1) {
        var k = s / steps;
        ctx.fillRect(Math.round(a.x + (b.x - a.x) * k) - (w >> 1),
                     Math.round(a.y + (b.y - a.y) * k) - (w >> 1), w, w);
      }
    }
  }

  // 世界坐标 → 视口（内部缓冲）坐标；全部取整，避免半像素抖动
  function viewPoint(x, y) {
    var z = S.cam.zoom;
    return {
      x: Math.round((x - S.cam.x) * z + S.bufW / 2),
      y: Math.round((y - S.cam.y) * z + S.bufH / 2),
    };
  }
  function vlen(v) { return Math.max(1, Math.round(v * S.cam.zoom)); }

  // ---------- 静态层离屏缓存 ----------
  // 地面、地面斑块与模型投影不随动画帧 / 事件变化（三函数零 ticks/rot/天气
  // 引用），每帧重画纯属浪费：按「世界引用 + 缓冲尺寸 + 昼夜」做 key，命中
  // 时整层 drawImage 复用；世界一重建（applyState）引用即变，缓存自然失效。
  function drawStaticLayer(ctx, pal) {
    // 相机（含聚焦/全景的缓动过程）会改变世界→屏幕投影：三个地面函数都用
    // viewPoint 画，相机一动整层就得重画——key 必须含相机三值，否则缓动期间
    // 地面/投影钉在旧位置，与建筑错位滑动。
    var c = S.staticLayer;
    var valid = c && c.world === S.world && c.w === S.bufW && c.h === S.bufH
      && c.pal === pal
      && c.camX === S.cam.x && c.camY === S.cam.y && c.zoom === S.cam.zoom;
    if (!valid) {
      if (!c) {
        var cv = document.createElement("canvas");
        c = S.staticLayer = { canvas: cv, ctx: cv ? cv.getContext("2d") : null };
      }
      if (!c.ctx) {
        // 离屏不可用的兜底：直画主画布，不进缓存
        drawTerrain(ctx, pal);
        drawGroundPatches(ctx, pal);
        if (S.world) drawShadows(ctx, pal);
        return;
      }
      c.canvas.width = S.bufW; c.canvas.height = S.bufH;
      var cctx = c.ctx;
      cctx.setTransform(1, 0, 0, 1, 0, 0);
      cctx.imageSmoothingEnabled = false;
      drawTerrain(cctx, pal);
      drawGroundPatches(cctx, pal);
      if (S.world) drawShadows(cctx, pal);
      c.world = S.world; c.w = S.bufW; c.h = S.bufH; c.pal = pal;
      c.camX = S.cam.x; c.camY = S.cam.y; c.zoom = S.cam.zoom;
    }
    ctx.drawImage(c.canvas, 0, 0);
  }

  // ---------- 渲染 ----------
  function render() {
    if (!S.mounted || !dom.canvas) return;
    // 窄屏（≤860px）画布被 CSS 隐藏：不白画，面板与抽屉仍是完整信息源
    if (S.cssW <= 1 || S.cssH <= 1) return;
    var cv = dom.canvas;
    // 2d context 对象在同一张画布上恒定（尺寸变化只重置其状态），缓存免每帧取
    var ctx = S.ctx;
    if (!ctx) {
      ctx = cv.getContext("2d");
      if (!ctx) return;
      S.ctx = ctx;
    }
    var pal = palette();
    var W = S.bufW, H = S.bufH;
    var z = S.cam.zoom;

    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.imageSmoothingEnabled = false;
    S.night = (S.daylightF != null ? S.daylightF : (pal === PAL_NIGHT ? 0 : 1)) < 0.5;

    drawSkyGround(ctx, pal);
    drawStaticLayer(ctx, pal);

    S.hits = [];
    if (S.world) {
      refreshFactoryAlerts();
      refreshFactoryMood();
      refreshUpcoming();
      drawAlertGround(ctx, pal);
      drawLightPools(ctx, pal);
      drawBerths(ctx, pal);
      drawModels(ctx, pal);
      drawUpcoming(ctx, pal);
      drawPackages(ctx, pal);
      drawEffects(ctx, pal);
      drawLighting(ctx, pal);
      drawWeather(ctx, pal);
      drawAlertBeacon(ctx, pal);
    }
    updateLabels();
    FX.refreshHover();
    // 信息牌自愈：hover 被重建 / 卸载清掉后，下一帧把残留的牌子收掉
    if (!S.hover && dom.tip && dom.tip.style.display !== "none") {
      dom.tip.style.display = "none";
    }
  }

  // ---------- 工业场景：三维模型渲染 ----------
  // 所有建筑/筒仓/传送带都是三维体：给出底面中心、半宽、半深、高度，按「顶面 + 左面 + 右面」
  // 三块面着色；渲染时按屏幕底线排序（画家算法），再叠投影、烟雾、粉尘与火花。
  function roleCols(pal, role, bg) {
    if (bg) {
      if (role === "rust") return { top: pal.bgRustLight, left: pal.bgRust, right: pal.bgRustDark };
      if (role === "glass") return { top: pal.bgSteelLight, left: pal.bgSteel, right: pal.bgSteelDark };
      if (role === "sand") return { top: pal.bgHaze, left: pal.bgHaze, right: pal.bgHaze };
      return { top: pal.bgSteelLight, left: pal.bgSteel, right: pal.bgSteelDark };
    }
    if (role === "rust") return { top: pal.rustLight, left: pal.rust, right: pal.rustDark };
    if (role === "glass") return { top: pal.glassLight, left: pal.glass, right: pal.glassDark };
    if (role === "concrete") return { top: pal.concreteLight, left: pal.concrete, right: pal.concreteDark };
    if (role === "sand") return { top: pal.groundLight, left: pal.ground, right: pal.groundDark };
    return { top: pal.steelLight, left: pal.steel, right: pal.steelDark };
  }

  /** 布景压后：把一套颜色整体往雾色拉近 k（0 = 原样，1 = 全雾）。

      工厂里现在有两层东西：会动、能点的（机台 / 包裹 / 成品垛 / 返工箱 / 工头）
      和布景（后场厂房、冷却塔、料场、罐区、远景）。以前两层同一套颜色，眼睛
      分不出「哪里在干活」。这里不改任何绘制函数，只给布景层换一份压淡的调色板，
      于是布景退后、机台跳出来，而建筑的轮廓与细节一点没丢。 */
  var DIM_CACHE = typeof WeakMap === "function" ? new WeakMap() : null;
  function dimPal(pal, k) {
    if (!k) return pal;
    if (!DIM_CACHE) return pal;
    var hit = DIM_CACHE.get(pal);
    if (hit && hit[k]) return hit[k];
    var out = {};
    for (var key in pal) {
      if (!Object.prototype.hasOwnProperty.call(pal, key)) continue;
      var c = pal[key];
      // 只处理颜色；bgHaze 这类雾色本身不压（压了会自我叠加）
      out[key] = (typeof c === "string" && (c.charAt(0) === "#" || c.indexOf("rgb") === 0))
        ? lerpColor(c, pal.bgHaze, k) : c;
    }
    // 状态色（绿 / 黄 / 红）不在布景层用，但调色板里带着，压淡反而更协调
    hit = hit || {};
    hit[k] = out;
    DIM_CACHE.set(pal, hit);
    return out;
  }

  function sceneHorizon() {
    var nominal = Math.max(22, Math.round(S.bufH * 0.22));
    if (!S.world) return nominal;
    var nearestRoof = S.bufH;
    S.world.models.forEach(function (m) {
      if (!m.peripheral || (!m.h && !m.h0 && !m.h1) || m.paving) return;
      var height = (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0));
      var depth = m.hh || (m.r ? m.r * 0.5 : 5);
      (m.path || [m]).forEach(function (p) {
        nearestRoof = Math.min(nearestRoof, viewPoint(p.x, p.y - height - depth).y);
      });
    });
    return Math.max(22, Math.min(nominal, nearestRoof - 14));
  }

  function drawSkyGround(ctx, pal) {
    var W = S.bufW, H = S.bufH;
    var skyH = sceneHorizon();
    var kind = weatherKind();
    var heavy = kind === "cloudy" || kind === "rain" || kind === "drizzle"
      || kind === "snow" || kind === "thunder" || kind === "fog";
    var sky = ctx.createLinearGradient(0, 0, 0, skyH + 26);
    sky.addColorStop(0, pal.skyTop);
    sky.addColorStop(0.62, pal.skyMid);
    sky.addColorStop(1, pal.skyLow);
    ctx.fillStyle = sky;
    ctx.fillRect(0, 0, W, skyH + 26);
    // 太阳/月亮放在左上：与墙面受光（左面中、右面暗）和右下投影同一方向
    var sunX = Math.round(W * 0.24);
    var sunY = Math.round(skyH * 0.30);
    var glow = ctx.createRadialGradient(sunX, sunY, 2, sunX, sunY, Math.round(W * 0.36));
    glow.addColorStop(0, pal.sun);
    glow.addColorStop(1, "rgba(255, 210, 130, 0)");
    ctx.globalAlpha = (S.night ? 0.14 : 0.38) * (heavy ? 0.25 : 1);
    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, W, skyH + 26);
    var sunBreath = S.reduced ? 0 : Math.sin(S.ticks * 0.025) * 0.025;
    ctx.globalAlpha = ((S.night ? 0.5 : 0.42) + sunBreath) * (heavy ? 0.35 : 1);
    var orbR = Math.max(2, Math.round(W * 0.010));
    fillOval(ctx, sunX, sunY, orbR, orbR, pal.sun);
    ctx.globalAlpha = 1;
    // 夜空星点：阴雨 / 雾天被云层挡住
    if (S.night && !heavy) {
      ctx.fillStyle = pal.glassLight;
      for (var st = 0; st < 30; st += 1) {
        var stx = (st * 97 + 31) % W;
        var sty = (st * 53 + 17) % Math.max(4, skyH - 4);
        ctx.globalAlpha = 0.22 + (st % 4) * 0.12;
        ctx.fillRect(stx, sty, 1, 1);
      }
      ctx.globalAlpha = 1;
    }
    drawSkyline(ctx, pal, skyH, W);
    if (!S.night && !heavy) {
      // 晴天 / 多云的薄云：低速漂移，给固定视角的天空一点远景呼吸感。
      ctx.globalAlpha = 0.15;
      var cloudDrift = S.reduced ? 0 : (S.ticks % 4800) * 0.025;
      for (var cl = 0; cl < 5; cl += 1) {
        var cw = Math.round(W * (0.05 + (cl % 2) * 0.03));
        var cx2 = ((cl * 211 + 70 + cloudDrift) % (W + cw * 2)) - cw;
        var cy2 = Math.round(skyH * (0.16 + (cl % 3) * 0.13));
        fillOval(ctx, cx2, cy2, cw, Math.max(2, Math.round(skyH * 0.05)), pal.sun);
        fillOval(ctx, cx2 + cw * 0.55, cy2 + 1, cw * 0.6, Math.max(2, Math.round(skyH * 0.038)), pal.sun);
      }
      ctx.globalAlpha = 1;
    }
    if (heavy) {
      // 阴雨 / 雾天：低垂的灰云压住天空，颜色用钢灰而不是暖沙
      ctx.globalAlpha = S.night ? 0.30 : 0.24;
      ctx.fillStyle = S.night ? pal.steelDark : pal.concreteLight;
      var clouds = kind === "fog" ? 5 : 8;
      for (var hc = 0; hc < clouds; hc += 1) {
        var hx = (hc * 179 + 40) % W;
        var hy = Math.round(skyH * (0.10 + (hc % 4) * 0.085));
        var hw = Math.round(W * (0.07 + (hc % 3) * 0.035));
        fillOval(ctx, hx, hy, hw, Math.max(3, Math.round(skyH * 0.085)), ctx.fillStyle);
        fillOval(ctx, hx + hw * 0.5, hy - 2, hw * 0.62,
                 Math.max(2, Math.round(skyH * 0.06)), ctx.fillStyle);
      }
      ctx.globalAlpha = 1;
    }
    // 沙地
    var ground = ctx.createLinearGradient(0, skyH, 0, H);
    ground.addColorStop(0, pal.groundLight);
    ground.addColorStop(0.24, pal.ground);
    ground.addColorStop(1, pal.groundDark);
    ctx.fillStyle = ground;
    ctx.fillRect(0, skyH, W, H - skyH);
    // 远景沙丘：屏幕空间、只压在地平线以下，给远处的仓库与储罐一个地平面
    for (var dune = 0; dune < 9; dune += 1) {
      var dx2 = (dune * 137 + 53) % W;
      var dy2 = skyH + 16 + ((dune * 71) % Math.max(8, Math.round(H * 0.14)));
      var dw2 = Math.round(W * (0.05 + (dune % 4) * 0.012));
      ctx.globalAlpha = 0.10 + (dune % 3) * 0.03;
      fillOval(ctx, dx2, dy2, dw2, Math.max(2, Math.round(H * 0.012)), pal.groundDark);
    }
    ctx.globalAlpha = 1;
    // 地平线雾带
    var haze = ctx.createLinearGradient(0, skyH - 4, 0, skyH + 28);
    haze.addColorStop(0, pal.bgHaze);
    haze.addColorStop(1, "rgba(0,0,0,0)");
    var hazeAlpha = kind === "fog"
      ? (S.night ? 0.34 : 0.42)
      : (S.night ? 0.16 : 0.22) * (heavy ? 1.3 : 1);
    ctx.globalAlpha = hazeAlpha;
    ctx.fillStyle = haze;
    ctx.fillRect(0, skyH - 4, W, 32);
    ctx.globalAlpha = 1;
    // 远处沙丘剪影
    ctx.globalAlpha = S.night ? 0.30 : 0.22;
    for (var i = 0; i < 7; i += 1) {
      var mx = Math.round((i + 0.5) * W / 7);
      var mw = Math.round(W / 12 + (i % 3) * 6);
      var mh = Math.round(5 + (i % 4) * 4);
      fillPoly(ctx, [
        { x: mx - mw, y: skyH + 2 },
        { x: mx, y: skyH - mh },
        { x: mx + mw, y: skyH + 2 },
      ], pal.groundDark);
    }
    ctx.globalAlpha = 1;
  }

  function drawTerrain(ctx, pal) {
    var z = S.cam.zoom;
    var pl = (S.world && S.world.plateau) || { cx: 0, cy: 0, hw: 260, hh: 125 };
    var c = viewPoint(pl.cx, pl.cy);
    var hw = Math.max(120, Math.round(pl.hw * z));
    var hh = Math.max(60, Math.round(pl.hh * z));
    fillDiamond(ctx, c.x, c.y, hw, hh, pal.ground);
    // 园区边界与外围道路处于同一地面，不画侧壁或下沉投影。
    var rim = [
      { x: c.x - hw, y: c.y }, { x: c.x, y: c.y - hh },
      { x: c.x + hw, y: c.y }, { x: c.x, y: c.y + hh },
      { x: c.x - hw, y: c.y },
    ];
    linePath(ctx, rim, 1, pal.stoneLight);
    // 干道贴着台地前缘两条边，两端再各自顺走向延长出去——出图的方向感
    var armL = { x: pl.cx - pl.hw - 14, y: pl.cy };
    var armR = { x: pl.cx + pl.hw + 14, y: pl.cy };
    var face = { x: pl.cx, y: pl.cy + pl.hh + 8 };
    function extendRoad(from, toward, len) {
      var dx = toward.x - from.x, dy = toward.y - from.y;
      var l = Math.hypot(dx, dy) || 1;
      return { x: from.x - dx / l * len, y: from.y - dy / l * len };
    }
    var road = [extendRoad(armL, face, 150), armL, face, armR, extendRoad(armR, face, 150)]
      .map(function (p) { return viewPoint(p.x, p.y); });
    linePath(ctx, road, vlen(10), pal.road);
    linePath(ctx, road, 1, pal.roadLight);
  }

  function drawGroundPatches(ctx, pal) {
    var site = S.world && S.world.site;
    if (!site) return;
    var z = S.cam.zoom;
    var pl = (S.world && S.world.plateau) || { cx: 0, cy: 0, hw: 260, hh: 125 };
    // 沙地风纹：几道很淡的横向长弧，压住大面积平涂
    var wr = seeded(0x6C0FFEE);
    ctx.globalAlpha = 0.06;
    for (var w = 0; w < 7; w += 1) {
      var wx = pl.cx + (wr() * 2 - 1) * pl.hw * 0.66;
      var wy = pl.cy + (wr() * 2 - 1) * pl.hh * 0.62;
      fillOval(ctx, viewPoint(wx, wy).x, viewPoint(wx, wy).y,
               Math.max(6, Math.round(pl.hw * (0.10 + wr() * 0.16) * z)),
               Math.max(1, Math.round(2 * z)), pal.groundHi);
    }
    ctx.globalAlpha = 0.16;
    site.patches.forEach(function (b) {
      var p = viewPoint(b.x, b.y);
      fillOval(ctx, p.x, p.y, Math.max(2, Math.round(b.rx * z)),
               Math.max(1, Math.round(b.ry * z)),
               b.light ? pal.groundHi : pal.groundDark);
    });
    // 砾石点：撒在台面范围内，近处更密
    var grit = seeded(0x5A17C0);
    for (var g = 0; g < 130; g += 1) {
      var gx = pl.cx + (grit() * 2 - 1) * pl.hw * 0.94;
      var gy = pl.cy + (grit() * 2 - 1) * pl.hh * 0.94;
      if (Math.abs(gx - pl.cx) / pl.hw + Math.abs(gy - pl.cy) / pl.hh > 0.98) continue;
      var gp = viewPoint(gx, gy);
      ctx.globalAlpha = 0.12 + grit() * 0.16;
      ctx.fillStyle = grit() < 0.55 ? pal.groundDark : pal.groundHi;
      ctx.fillRect(gp.x, gp.y, 1, 1);
    }
    ctx.globalAlpha = 1;
    // 传送带下方的土路与车辙
    S.world.factories.forEach(function (f) {
      var pts = f.belt.map(function (p) { return viewPoint(p.x, p.y); });
      linePath(ctx, pts, vlen(8), pal.roadDark);
      linePath(ctx, pts, vlen(6), pal.road);
      linePath(ctx, pts, vlen(1), pal.roadLight);
      var total = pathLength(pts);
      for (var d = 0; d < total; d += Math.max(5, vlen(16))) {
        var pt = pointAt(pts, total ? d / total : 0);
        ctx.fillStyle = pal.roadDark;
        ctx.fillRect(Math.round(pt.x) - 3, Math.round(pt.y) - 1, 2, 1);
        ctx.fillRect(Math.round(pt.x) + 1, Math.round(pt.y) + 1, 2, 1);
      }
    });
    (site.roads || []).forEach(function (rd) {
      var pts = rd.path.map(function (q) { return viewPoint(q.x, q.y); });
      linePath(ctx, pts, vlen(7), pal.roadDark);
      linePath(ctx, pts, vlen(5), pal.road);
      linePath(ctx, pts.map(function (q) { return { x: q.x - 1, y: q.y }; }),
               Math.max(1, vlen(1)), pal.roadLight);
      linePath(ctx, pts.map(function (q) { return { x: q.x + 1, y: q.y }; }),
               Math.max(1, vlen(1)), pal.roadLight);
    });
    (site.tracks || []).forEach(function (tr) {
      var pts = tr.path.map(function (q) { return viewPoint(q.x, q.y); });
      linePath(ctx, pts, vlen(3), pal.roadDark);
      linePath(ctx, pts, Math.max(1, vlen(1)), pal.roadLight);
    });
  }

  function drawShadows(ctx, pal) {
    var z = S.cam.zoom;
    S.world.models.forEach(function (m) {
      // 远景模型也要有接地影，否则雾里一排白色方块看着像浮在天上
      ctx.globalAlpha = m.bg ? 0.45 : 1;
      var p = viewPoint(m.x, m.y);
      if (m.kind === "box" || m.kind === "furnace" || m.kind === "distillation") {
        var hw = m.hw * z, hh = m.hh * z;
        var off = Math.min(20, (m.h || 10) * z * 0.22);
        fillDiamond(ctx, p.x + off * 0.7, p.y + off * 0.45, hw * 1.02, hh * 1.02, pal.shadow);
      } else if (m.kind === "cyl") {
        var rx = m.r * z, ry = Math.max(1, rx * 0.55);
        var off2 = Math.min(22, m.h * z * 0.2);
        fillOval(ctx, p.x + off2 * 0.7, p.y + off2 * 0.45, rx * 1.1, ry * 1.1, pal.shadow);
      } else if (m.kind === "cooling") {
        fillOval(ctx, p.x + vlen(9), p.y + vlen(4), vlen(m.r * 1.1), vlen(m.r * 0.45), pal.shadow);
      } else if (m.kind === "coalShed" || m.kind === "loadingRack") {
        if (m.axis) {
          // 煤棚是轴系零件：影子跟着转到「沿公路 / 纵深」两条轴上
          var shedFrame = yardFrame(m, p.x + vlen(6), p.y + vlen(3), z);
          fillPoly(ctx, [shedFrame(-m.hw * 1.02, -m.hh * 1.02), shedFrame(m.hw * 1.02, -m.hh * 1.02),
                         shedFrame(m.hw * 1.02, m.hh * 1.02), shedFrame(-m.hw * 1.02, m.hh * 1.02)],
                   pal.shadow);
        } else {
          fillDiamond(ctx, p.x + vlen(6), p.y + vlen(3), m.hw * z * 1.02, m.hh * z * 1.02, pal.shadow);
        }
      } else if (m.kind === "siloBattery") {
        // 一仓一影：筒仓群是并排几只仓，用整块影子会糊成一坨（仓列沿公路方向）
        var siloCount = Math.max(1, Math.round(m.count || 3));
        var siloFrame = yardFrame(m, p.x + vlen(7), p.y + vlen(3), z);
        for (var sk = 0; sk < siloCount; sk += 1) {
          var siloPt = siloFrame((sk - (siloCount - 1) / 2) * (m.gapX || 16), 0);
          fillOval(ctx, siloPt.x, siloPt.y, vlen(m.r * 1.12), vlen(m.r * 0.56), pal.shadow);
        }
      } else if (m.kind === "gasholder") {
        fillOval(ctx, p.x + vlen(8), p.y + vlen(4), vlen(m.r * 1.14), vlen(m.r * 0.57), pal.shadow);
      } else if (m.kind === "flare") {
        fillOval(ctx, p.x + vlen(3), p.y + vlen(2), vlen(m.hw || 7), vlen(m.hh || 3.5), pal.shadow);
      } else if (m.kind === "coalPile") {
        fillOval(ctx, p.x + vlen(3), p.y + vlen(2), vlen(m.hw * 1.05), vlen(m.hh * 0.7), pal.shadow);
      } else if (m.kind === "weighbridge") {
        fillDiamond(ctx, p.x + vlen(2), p.y + vlen(1), m.hw * z * 1.02, m.hh * z * 1.02, pal.shadow);
      } else if (m.kind === "floodlight") {
        fillOval(ctx, p.x + vlen(2), p.y + vlen(1), vlen(5), vlen(2.5), pal.shadow);
      } else if (m.kind === "gantry") {
        fillOval(ctx, p.x + vlen(2), p.y + vlen(1), vlen(m.w || 8), vlen(3), pal.shadow);
      } else if (m.path) {
        var pts = m.path.map(function (q) { return viewPoint(q.x, q.y); });
        linePath(ctx, pts, vlen(m.width || 6), pal.shadow);
      }
    });
    ctx.globalAlpha = 1;
  }

  /** 厂区级异常的地面层：场院外缘一圈脉冲光晕。

      画在模型之前（和光池同一层），所以只会从场坪边缘透出来，不会盖住机台
      与建筑；停机（root_ok=false）画成静止的红环 + 斜向封锁带，不闪。
      脉冲只改透明度、不改几何：reduced-motion 下自然退化为静止描边。 */
  function drawAlertGround(ctx, pal) {
    if (!S.world) return;
    var z = S.cam.zoom;
    S.world.factories.forEach(function (f) {
      var a = f.alert || { level: 0 };
      if (!a.level) return;
      var c = viewPoint(f.x, f.y);
      var hw = Math.max(40, Math.round(f.halfW * z * 0.94));
      var hh = Math.max(20, Math.round(f.halfH * z * 0.94));
      var color = a.level === 1 ? pal.warn : pal.bad;
      var pts = ringPts(c.x, c.y, hw, hh);
      if (a.level === 4) {
        // 停工：两道静止的环，像拉了封锁带，不随时间闪（它不是「正在出事」）
        ctx.globalAlpha = 0.34;
        linePath(ctx, pts, Math.max(1, Math.round(2 * z)), color);
        ctx.globalAlpha = 0.16;
        linePath(ctx, ringPts(c.x, c.y, hw * 0.88, hh * 0.88),
                 Math.max(1, Math.round(2 * z)), color);
        ctx.globalAlpha = 1;
        return;
      }
      // 脉冲：4 秒一轮；等级越高基线越亮，让严重的那座自己跳出来
      var base = a.level >= 3 ? 0.30 : (a.level === 2 ? 0.20 : 0.13);
      var pulse = S.reduced ? 0 : Math.sin(S.ticks * 0.16) * (a.level >= 3 ? 0.18 : 0.08);
      ctx.globalAlpha = clamp(base + pulse, 0.05, 0.55);
      linePath(ctx, pts, Math.max(1, Math.round(2 * z)), color);
      // 场坪内侧再压一层很淡的填充，远看是一整块警示色而不是一条细线
      ctx.globalAlpha = clamp(base * 0.35 + pulse * 0.3, 0.02, 0.2);
      fillDiamond(ctx, c.x, c.y, hw, hh, color);
      ctx.globalAlpha = 1;
    });
  }

  /** 厂区级的警报灯柱：立在场院后沿，高出厂房，全景下也能一眼看见。

      地面光晕解决「哪一块区域有问题」，灯柱解决「问题在哪座厂区」——等距俯视
      下光晕会被厂房压住，而竖直的柱子不会被任何前景挡住（它画在模型之后）。
      这是纯装饰层：不参与命中测试，交互仍走厂区名牌与左面板。 */
  function drawAlertBeacon(ctx, pal) {
    if (!S.world) return;
    var z = S.cam.zoom;
    S.world.factories.forEach(function (f) {
      var a = f.alert || { level: 0 };
      if (!a.level) return;
      var c = viewPoint(f.x, f.y);
      var groundY = c.y - Math.round(f.halfH * z * 0.72);
      var mastH = Math.max(14, Math.round(34 * z));
      var color = a.level === 1 ? pal.warn : pal.bad;
      var x = Math.round(c.x + f.halfW * z * 0.5);
      // 桅杆
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(x, groundY - mastH, Math.max(1, Math.round(z)), mastH);
      // 灯头：等级 3 画两颗，等级 4 画成熄灭的暗红（停工不是「正在发生」）
      var lampR = Math.max(2, Math.round(3 * z));
      var on = a.level !== 4;
      var blink = on && !S.reduced ? (Math.sin(S.ticks * 0.22) > -0.2 ? 1 : 0.35) : 1;
      ctx.globalAlpha = a.level === 4 ? 0.5 : blink;
      fillCircle(ctx, x, groundY - mastH - lampR, lampR, color);
      if (on) {
        // 光晕：夜里更亮，白天靠对比度也读得出来
        var halo = Math.max(5, Math.round((S.night ? 12 : 9) * z));
        ctx.globalAlpha = (S.night ? 0.26 : 0.16) * blink;
        fillCircle(ctx, x, groundY - mastH - lampR, halo, color);
      }
      if (a.level >= 3) {
        var r2 = Math.max(1, Math.round(2 * z));
        ctx.globalAlpha = a.level === 4 ? 0.5 : blink;
        fillCircle(ctx, x, groundY - mastH - lampR * 2 - r2, r2, color);
      }
      ctx.globalAlpha = 1;
    });
  }

  /** 夜里的地面光池：先于模型画在地面上，灯光才不会盖住建筑。 */
  function drawLightPools(ctx, pal) {
    if (!S.night) return;
    var lightIndex = 0;
    S.world.models.forEach(function (m) {
      var flicker = S.reduced ? 1 : 0.94 + 0.06 * Math.sin(S.ticks * 0.075 + lightIndex * 1.7);
      lightIndex += 1;
      if (m.bg) return;
      var p = viewPoint(m.x, m.y);
      var baseY = p.y - Math.round((m.z0 || 0) * S.cam.zoom);
      if (m.kind === "lamp") {
        var arm = Math.max(3, Math.round(4 * S.cam.zoom));
        ctx.globalAlpha = 0.30 * flicker;
        fillOval(ctx, p.x - arm, baseY + vlen(1), vlen(13), vlen(5.5), pal.glow);
        ctx.globalAlpha = 0.42 * flicker;
        fillOval(ctx, p.x - arm, baseY + vlen(1), vlen(8), vlen(3.5), pal.glow);
      } else if (m.kind === "floodlight") {
        ctx.globalAlpha = 0.34 * flicker;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(24), vlen(10), pal.glowCool);
        ctx.globalAlpha = 0.48;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(15), vlen(6), pal.glowCool);
        ctx.globalAlpha = 0.62;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(8), vlen(3.4), pal.glowCool);
      } else if (m.kind === "flare") {
        ctx.globalAlpha = 0.35;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(10), vlen(4.5), pal.glow);
      } else if (m.kind === "loadingRack") {
        ctx.globalAlpha = 0.28;
        fillOval(ctx, p.x, baseY + vlen(1), vlen(m.hw * 1.1), vlen(m.hh * 0.8), pal.glow);
      } else if (m.kind === "box" && m.door && !m.bg) {
        // 厂房门口的暖光落在场坪上
        var dhw = m.hw * S.cam.zoom, dhh = m.hh * S.cam.zoom;
        var dcx = p.x - dhw * 0.5, dcy = baseY + dhh * 0.5;
        ctx.globalAlpha = 0.18;
        fillOval(ctx, dcx, dcy, Math.max(3, dhw * 0.34), Math.max(2, dhh * 0.30), pal.glow);
        ctx.globalAlpha = 0.28;
        fillOval(ctx, dcx, dcy, Math.max(2, dhw * 0.20), Math.max(1, dhh * 0.17), pal.glow);
      }
    });
    ctx.globalAlpha = 1;
  }

  function sceneDrawItems(models) {
    var items = [];
    models.forEach(function (m) {
      if (m.dynamic) return;   // 动态对象（工头）不进静态画序缓存：每帧与静态合并排序
      if (m.kind === "conveyor") {
        var total = pathLength(m.path), acc = 0;
        for (var i = 1; i < m.path.length; i += 1) {
          var a = m.path[i - 1], b = m.path[i];
          var len = Math.hypot(b.x - a.x, b.y - a.y);
          var count = Math.max(1, Math.ceil(len / 14));
          for (var j = 0; j < count; j += 1) {
            var t0 = j / count, t1 = (j + 1) / count;
            var piece = Object.assign({}, m, {
              path: [{ x: a.x + (b.x - a.x) * t0, y: a.y + (b.y - a.y) * t0 },
                     { x: a.x + (b.x - a.x) * t1, y: a.y + (b.y - a.y) * t1 }],
              h0: m.h0 + (m.h1 - m.h0) * (total ? (acc + len * t0) / total : 0),
              h1: m.h0 + (m.h1 - m.h0) * (total ? (acc + len * t1) / total : 0),
              flowStart: acc + len * t0,
              flowEnd: acc + len * t1,
              flowTotal: total,
              support: m.support !== false && j % 2 === 0,
            });
            items.push(piece);
          }
          acc += len;
        }
      } else items.push(m);
    });
    return items.map(function (m, index) {
      var mid = m.path ? pointAt(m.path, 0.5) : m;
      return { m: m, d: mid.y, index: index };
    }).sort(function (a, b) {
      var layerA = drawLayer(a.m);
      var layerB = drawLayer(b.m);
      return layerA - layerB || a.d - b.d || (a.m.z0 || 0) - (b.m.z0 || 0) || a.index - b.index;
    });
  }

  /** 分层：铺装永远在最下，远景 bg 在中，立体件按 y 排在上面。 */
  function drawLayer(m) {
    if (m.paving) return 0;
    return m.bg ? 1 : 2;
  }

  /** 出车的动态绘制项（逐帧）：卡车按 tripPose，叉车按 forkliftDutyPose（常驻，
      装货相走往返）。位置只由 spawn 时的 t0 与位姿函数决定，既不读也不改运行状态。 */
  function drawTrips(pal, out, now) {
    (S.trips || []).forEach(function (trip) {
      var pose = tripPose(trip, now);
      if (pose.alpha <= 0) return;
      var vp = viewPoint(pose.x, pose.y);
      var hoveredTr = S.hover && S.hover.type === "truck"
        && String(S.hover.pid) === String(trip.pid);
      out.push({ d: pose.y, z0: pose.z0 || 0, draw: function (ctx) {
        drawTripTruck(ctx, pal, vp.x, vp.y, pose, hoveredTr);
      } });
      S.hits.push({ type: "truck", pid: trip.pid, x: vp.x, y: vp.y - vlen(4),
                    r: Math.max(8, vlen(13)) });
    });
  }

  /** 常驻叉车：每座厂区都有一台，装货时走完整往返，其余时间停在停靠位。
      位姿来自纯函数 forkliftDutyPose，逐帧随厂区 mood / 当前 trip 现算。 */
  function drawForklifts(pal, out, now) {
    S.world.factories.forEach(function (f) {
      var pose = forkliftDutyPose(f, now);
      if (!pose) return;
      var vp = viewPoint(pose.x, pose.y);
      var hovered = S.hover && S.hover.type === "forklift"
        && String(S.hover.pid) === String(f.id);
      out.push({ d: pose.y, z0: pose.z0 || 0, draw: function (ctx) {
        drawForkliftModel(ctx, pal, vp.x, vp.y, pose, hovered);
      } });
      S.hits.push({ type: "forklift", pid: f.id, x: vp.x, y: vp.y - vlen(3),
                    r: Math.max(6, vlen(8)) });
    });
  }

  /** 动态对象（工头 / 出车 / 叉车）逐帧收集成绘制项：位置来自纯位姿函数，
      画的时候才换算视口坐标；顺带登记点击热区与 hover 高亮。 */
  function collectDynamicItems(pal) {
    var out = [];
    var now = nowMs();
    if (S.world) {
      S.world.factories.forEach(function (f) {
        if (!f.worker) return;
        var pose = workerPose(f, now, S.reduced);
        var vp = viewPoint(pose.x, pose.y);
        var model = f.worker;
        var hovered = S.hover && S.hover.type === "sheep"
          && String(S.hover.pid) === String(f.id);
        out.push({ d: pose.y, z0: pose.z0 != null ? pose.z0 : YARD_Z, draw: function (ctx) {
          drawWorkerPose(ctx, pal, model, pose, hovered);
        } });
        if (vp.x > -20 && vp.x < S.bufW + 20) {
          S.hits.push({ type: "sheep", pid: f.id, x: vp.x, y: vp.y - vlen(4),
                        r: Math.max(6, vlen(7)) });
        }
      });
      drawForklifts(pal, out, now);
    }
    drawTrips(pal, out, now);
    if (collectPackageItems) collectPackageItems(pal, out, now);
    out.sort(function (a, b) { return a.d - b.d || a.z0 - b.z0; });
    return out;
  }

  function drawModels(ctx, pal) {
    // 画序缓存：models 与排序键（分层 / 底线 y / z0 / 序号）都是 buildWorld
    // 重建时定死的静态数据，每帧重切传送带 + 全量排序纯属浪费；
    // world 对象一换（applyState 重建）缓存自然失效，无需显式钩子。
    var world = S.world;
    var items = world.drawItems;
    if (!items) {
      items = sceneDrawItems(world.models);
      world.drawItems = items;
      world.drawHasBg = items.some(function (it) { return it.m.bg; });
    }
    // 动态对象与静态画序逐帧合并：按底线 y 插进主体层（铺装 / 背景之下不插），
    // 羊与车在机台 / 建筑之间穿行时前后遮挡关系与静态件一致，不穿模。
    var dyn = collectDynamicItems(pal);
    var di = 0;
    function flushDyn(beforeD, beforeZ0) {
      while (di < dyn.length &&
             (dyn[di].d < beforeD || (dyn[di].d === beforeD && dyn[di].z0 <= (beforeZ0 || 0)))) {
        dyn[di].draw(ctx);
        di += 1;
      }
    }
    var hazed = !world.drawHasBg;
    items.forEach(function (it) {
      if (!hazed && !it.m.bg) {
        // 远景画完、主体之前压一层薄雾：背景带整体退后，仍保留轮廓
        var W = S.bufW, H = S.bufH;
        var horizon = sceneHorizon();
        ctx.fillStyle = pal.bgHaze;
        // 阶梯式薄雾（不用渐变：假 Canvas / 老 WebView 都只认纯色填充）
        for (var hb = 0; hb < 6; hb += 1) {
          ctx.globalAlpha = (S.night ? 0.10 : 0.14) * (1 - hb / 6);
          ctx.fillRect(0, horizon - 18 + hb * Math.max(3, Math.round(H * 0.06)),
                       W, Math.max(3, Math.round(H * 0.06)) + 1);
        }
        ctx.globalAlpha = S.night ? 0.05 : 0.07;
        ctx.fillRect(0, 0, W, H);
        ctx.globalAlpha = 1;
        hazed = true;
      }
      if (drawLayer(it.m) >= 2) flushDyn(it.d, it.m.z0 || 0);
      drawModel(ctx, pal, it.m);
    });
    flushDyn(Infinity, 0);
  }

  function sawRoofFaces(x, y, hw, hh, rise) {
    function pt(u, v, lift) {
      return { x: x + (u - v) * hw, y: y + (u + v - 1) * hh - (lift || 0) };
    }
    var faces = [];
    for (var i = 0; i < 3; i += 1) {
      var a = i / 3, b = (i + 1) / 3;
      faces.push({ side: "slope", points: [pt(a, 0), pt(a, 1), pt(b, 1, rise), pt(b, 0, rise)] });
      faces.push({ side: "gable", points: [pt(a, 1), pt(b, 1), pt(b, 1, rise)] });
      faces.push({ side: "glazing", points: [pt(b, 0), pt(b, 0, rise), pt(b, 1, rise), pt(b, 1)] });
    }
    return faces;
  }

  function drawModel(ctx, pal, m) {
    // 厂区建筑（compoundPart）是布景：压淡 22%。机台 / 成品 / 返工不在其中，
    // 保持全饱和——这就是「交互层与布景层分色」的全部实现。
    if (m.compoundPart && !m.facility) pal = dimPal(pal, 0.16);
    if (m.kind === "box") drawBoxModel(ctx, pal, m);
    else if (m.kind === "cyl") drawCylinderModel(ctx, pal, m);
    else if (m.kind === "furnace") drawFurnaceModel(ctx, pal, m);
    else if (m.kind === "distillation") drawDistillationModel(ctx, pal, m);
    else if (m.kind === "conveyor") drawConveyorModel(ctx, pal, m);
    else if (m.kind === "machine") drawMachineModel(ctx, pal, m);
    else if (m.kind === "pallet") drawPalletModel(ctx, pal, m);
    else if (m.kind === "crate") drawCrateModel(ctx, pal, m);
    else if (m.kind === "barrel") drawBarrelModel(ctx, pal, m);
    else if (m.kind === "ore") drawOreModel(ctx, pal, m);
    else if (m.kind === "worker") drawWorkerModel(ctx, pal, m);
    else if (m.kind === "truck") drawTruckModel(ctx, pal, m);
    else if (m.kind === "radar") drawRadarModel(ctx, pal, m);
    else if (m.kind === "pylon") drawPylonModel(ctx, pal, m);
    else if (m.kind === "rail") drawRailModel(ctx, pal, m);
    else if (m.kind === "rock") drawRockModel(ctx, pal, m);
    else if (m.kind === "shrub") drawShrubModel(ctx, pal, m);
    else if (m.kind === "gantry") drawGantryModel(ctx, pal, m);
    else if (m.kind === "catwalk") drawCatwalkModel(ctx, pal, m);
    else if (m.kind === "figure") drawFigureModel(ctx, pal, m);
    else if (m.kind === "cooling") drawCoolingTowerModel(ctx, pal, m);
    else if (m.kind === "pipeRack") drawPipeRackModel(ctx, pal, m);
    else if (m.kind === "transferBridge") drawTransferBridgeModel(ctx, pal, m);
    else if (m.kind === "fence") drawFenceModel(ctx, pal, m);
    else if (m.kind === "coalShed") drawCoalShedModel(ctx, pal, m);
    else if (m.kind === "siloBattery") drawSiloBatteryModel(ctx, pal, m);
    else if (m.kind === "gasholder") drawGasholderModel(ctx, pal, m);
    else if (m.kind === "basin") drawBasinModel(ctx, pal, m);
    else if (m.kind === "loadingRack") drawLoadingRackModel(ctx, pal, m);
    else if (m.kind === "flare") drawFlareModel(ctx, pal, m);
    else if (m.kind === "coalPile") drawCoalPileModel(ctx, pal, m);
    else if (m.kind === "weighbridge") drawWeighbridgeModel(ctx, pal, m);
    else if (m.kind === "floodlight") drawFloodlightModel(ctx, pal, m);
    else if (m.kind === "lamp") drawLampModel(ctx, pal, m);
    else if (m.kind === "reel") drawReelModel(ctx, pal, m);
    else if (m.kind === "flag") drawFlagModel(ctx, pal, m);
    else if (m.kind === "gate") drawGateModel(ctx, pal, m);
    else if (m.kind === "rework") drawReworkModel(ctx, pal, m);
  }

  /** 大门道闸：两根柱 + 一根栏杆。开工时抬起，停工 / 出事时落下（出事时染红）。
      它站在成品托盘旁边——「这批货能出门吗」是这座厂区最直接的一句话。 */
  function drawGateModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var f = m.factory;
    var mood = (f && f.mood) || "idle";
    var open = mood === "working";
    var postH = Math.max(7, Math.round(13 * z));
    var w = Math.max(1, Math.round(z * 1.6));
    var gap = Math.max(5, Math.round(9 * z));
    // 门前后一小块混凝土 + 地面影子
    fillOval(ctx, p.x, baseY + 1, gap * 1.6, Math.max(1, Math.round(2 * z)), pal.shadow);
    ctx.fillStyle = pal.concreteDark;
    ctx.fillRect(p.x - gap, baseY - 1, gap * 2, Math.max(1, Math.round(z)));
    // 两根柱（左柱带门房顶）
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x - gap - w, baseY - postH, w * 2, postH);
    ctx.fillRect(p.x + gap - w, baseY - postH, w * 2, postH);
    ctx.fillStyle = pal.metalLight;
    ctx.fillRect(p.x - gap - w, baseY - postH, w * 2, Math.max(1, Math.round(z)));
    // 栏杆：抬起 = 竖起；落下 = 横在柱间，红白相间
    var armLen = Math.max(8, Math.round(16 * z));
    if (open) {
      ctx.fillStyle = pal.ok;
      ctx.fillRect(p.x - gap, baseY - postH - armLen, Math.max(1, w * 2), armLen);
      ctx.fillStyle = pal.ok;
    } else {
      var tone = mood === "trouble" ? pal.bad : pal.paint;
      ctx.fillStyle = tone;
      ctx.fillRect(p.x - gap, baseY - Math.round(postH * 0.78), gap * 2, Math.max(1, Math.round(z * 1.6)));
      // 红白警示段：落下时才有，抬起时不画（抬起是一根干净的绿杆）
      ctx.fillStyle = pal.mark;
      for (var sg = 1; sg < 4; sg += 2) {
        ctx.fillRect(p.x - gap + Math.round(gap * 2 * sg / 4), baseY - Math.round(postH * 0.78),
                     Math.max(1, Math.round(z * 1.2)), Math.max(1, Math.round(z * 1.6)));
      }
    }
    // 门顶小灯：开工绿 / 出事红闪 / 停工灭
    var lamp = mood === "working" ? pal.ok : (mood === "trouble" ? pal.bad : pal.idle);
    ctx.fillStyle = lamp;
    ctx.fillRect(p.x - gap - w, baseY - postH - Math.max(1, Math.round(2 * z)),
                 Math.max(1, Math.round(w * 2)), Math.max(1, Math.round(2 * z)));
    if (mood === "trouble" && !S.reduced && (S.ticks % 6) < 3) {
      ctx.globalAlpha = 0.35;
      fillOval(ctx, p.x - gap, baseY - postH, Math.max(2, Math.round(4 * z)),
               Math.max(1, Math.round(3 * z)), pal.bad);
      ctx.globalAlpha = 1;
    }
  }

  /** 返工区：一块划线的空地，按今日失败 / 被拦的轮次堆红箱。

      以前「没做成」只在顶上变成一个红色数字，看不见它有多大一坨。堆在地上的
      红箱是同一件事的另一种说法，而且它会长高、会被清掉——这是「生产场景」
      与「报表」的分界线。 */
  function drawReworkModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var st = statFor(m.projectId);
    var n = Math.min(REWORK_MAX, st.rework || 0);
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(6, Math.round(13 * z));
    var hh = Math.max(3, Math.round(6 * z));
    // 场地：黄漆虚线圈一块，空着的时候也看得见「这里本该堆东西」
    ctx.globalAlpha = 0.42;
    linePath(ctx, ringPts(p.x, baseY, hw, hh), Math.max(1, Math.round(z)), pal.paint);
    ctx.globalAlpha = 1;
    if (!n) {
      // 空场地：中间一个「返工」叉形地标（两条短斜线），低调但可辨
      ctx.fillStyle = pal.paintDark;
      ctx.fillRect(p.x - 2, baseY - 1, 4, 1);
      return;
    }
    var s = Math.max(3, Math.round(5 * z));
    for (var i = 0; i < n; i += 1) {
      var col = i % 2, row = Math.floor(i / 2);
      var bx = p.x - Math.round(s * 1.15) + col * (s * 2 + 1);
      var by = baseY - row * Math.round(s * 1.5);
      drawIsoBoxAt(ctx, bx, by, s, Math.max(1, s * 0.5), Math.max(2, Math.round(s * 1.6)),
                   pal.rustLight, pal.bad, pal.ink);
      // 封条：一道浅色胶带，让红箱不至于糊成一块
      ctx.fillStyle = pal.mark;
      ctx.fillRect(bx - s, by - Math.max(2, Math.round(s * 1.6)),
                   Math.max(1, Math.round(s * 0.6)), 1);
    }
  }

  function drawTruckModel(ctx, pal, m, flip) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var mx = flip ? -1 : 1;   // 车头朝向：默认 +x，flip 时整车镜像到 -x
    var L = Math.max(10, Math.round(26 * z));
    var W = Math.max(4, Math.round(9 * z));
    var steel = m.bg ? pal.bgSteel : pal.steel;
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var rust = m.bg ? pal.bgRust : pal.rust;
    var rustDark = m.bg ? pal.bgRustDark : pal.rustDark;
    var rustLight = m.bg ? pal.bgRustLight : pal.rustLight;
    var wheel = m.bg ? pal.bgSteelDark : pal.ink;
    // 车头侧的小矩形（车窗 / 灯）：按朝向铺在哪一侧
    function sideRect(frac, wz, hz, yOff, color, sideMul) {
      var wpx = Math.max(1, Math.round(wz));
      var x0 = sideMul > 0 ? Math.round(p.x + L * frac) : Math.round(p.x - L * frac) - wpx;
      ctx.fillStyle = color;
      ctx.fillRect(x0, Math.round(baseY - yOff), wpx, Math.max(1, Math.round(hz)));
    }
    fillOval(ctx, p.x, baseY + 1, L * 0.5, W * 0.4, pal.shadow);
    if (S.night && !m.bg) {
      ctx.globalAlpha = 0.12;
      fillOval(ctx, p.x + mx * Math.round(L * 0.66), baseY + 1,
               Math.max(3, Math.round(7 * z)), Math.max(1, Math.round(2.5 * z)), pal.window);
      ctx.globalAlpha = 1;
    }
    ctx.fillStyle = wheel;
    for (var i = 0; i < 3; i += 1) {
      fillOval(ctx, p.x - L * 0.3 + i * L * 0.28, baseY,
               Math.max(1, W * 0.28), Math.max(1, W * 0.16), wheel);
    }
    drawIsoBoxAt(ctx, p.x + mx * L * 0.08, baseY - 2, L * 0.34, W * 0.3,
                 Math.max(5, Math.round(9 * z)), steelLight, steel, steelDark);
    drawIsoBoxAt(ctx, p.x + mx * L * 0.38, baseY - 2, L * 0.15, W * 0.28,
                 Math.max(4, Math.round(7 * z)), rustLight, rust, rustDark);
    sideRect(0.45, 3 * z, 2 * z, 2 + 6 * z, pal.windowDark, mx);
    if (S.night && !m.bg) {
      sideRect(0.50, 2 * z, 1, 4 * z, pal.window, mx);
      sideRect(0.44, 1.5 * z, 1, 4 * z, pal.bad, -mx);
    }
  }

  function drawRadarModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(4, Math.round(7 * z));
    ctx.fillStyle = pal.steelDark;
    ctx.fillRect(p.x, baseY - h, 1, h);
    var a = S.rot * 2;
    var dx = Math.round(Math.cos(a) * 3 * z);
    var dy = Math.round(Math.sin(a) * 1.5 * z);
    var rx = Math.max(2, Math.round(4 * z));
    var ry = Math.max(1, Math.round(2 * z));
    fillOval(ctx, p.x + dx, baseY - h - ry, rx, ry, pal.steelLight);
    ctx.fillStyle = pal.steelDark;
    ctx.fillRect(p.x + dx, baseY - h - ry, Math.max(1, Math.abs(dx) + 1), 1);
    if (S.night) {
      ctx.fillStyle = pal.bad;
      ctx.fillRect(p.x - 1, baseY - h - ry * 2 - 2, 2, 2);
    }
  }

  function drawIsoBoxAt(ctx, x, y, hw, hh, h, top, left, right) {
    fillPoly(ctx, [
      { x: x - hw, y: y - h }, { x: x, y: y - h + hh },
      { x: x, y: y + hh }, { x: x - hw, y: y },
    ], left);
    fillPoly(ctx, [
      { x: x, y: y - h + hh }, { x: x + hw, y: y - h },
      { x: x + hw, y: y }, { x: x, y: y + hh },
    ], right);
    fillDiamond(ctx, x, y - h, hw, hh, top);
  }

  function drawBoxModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(2, Math.round(m.hw * z));
    var hh = Math.max(1, Math.round(m.hh * z));
    var h = Math.max(2, Math.round(m.h * z));
    var cols = roleCols(pal, m.role, m.bg);
    var top = cols.top, left = cols.left, right = cols.right;
    var frame = m.bg ? pal.bgSteelDark : pal.panelLine;

    if (m.paving) {
      // 场院坪保留整洁亮面，仅用边缘表现厚度。
      drawIsoBoxAt(ctx, base.x, baseY, hw, hh, h, top, left, right);
      var ty = baseY - h;
      // 坪缘石：上缘亮线、下缘暗线，平台不再是一片平涂
      linePath(ctx, [{ x: base.x - hw, y: ty }, { x: base.x, y: ty + hh },
                    { x: base.x + hw, y: ty }], Math.max(1, Math.round(z)), pal.mark);
      linePath(ctx, [{ x: base.x - hw, y: ty + 1 }, { x: base.x, y: ty + hh + 1 },
                    { x: base.x + hw, y: ty + 1 }], Math.max(1, Math.round(z)), pal.groundDark);
      if (m.curb) {
        var postH = Math.max(2, Math.round(4 * z));
        [[base.x - hw, ty], [base.x, ty + hh], [base.x + hw, ty]].forEach(function (cp) {
          ctx.fillStyle = pal.paint;
          ctx.fillRect(Math.round(cp[0]), Math.round(cp[1] - postH), 1, postH);
          ctx.fillStyle = pal.paintDark;
          ctx.fillRect(Math.round(cp[0]) - 1, Math.round(cp[1]) - 1, 3, 1);
        });
      }
      // 坪面污渍（确定性散布，不随帧变化）
      var rand = seeded(hashKey("pave" + m.x + ":" + m.y + ":" + hw));
      for (var st = 0; st < 5; st += 1) {
        var sx = base.x + (rand() * 2 - 1) * hw * 0.62;
        var sy = ty + (rand() * 2 - 1) * hh * 0.56;
        ctx.globalAlpha = 0.10 + rand() * 0.06;
        fillOval(ctx, sx, sy, Math.max(2, hw * (0.05 + rand() * 0.08)),
                 Math.max(1, hh * (0.03 + rand() * 0.05)), pal.stain);
      }
      ctx.globalAlpha = 1;
      drawYardMarkings(ctx, pal, m, base.x, baseY - h, hw, hh);
      // 围栏：前缘高栏 + 后缘低栏，围出完整的场院边界
      if (m.rail) {
        var ph = Math.max(2, Math.round(4 * z));
        for (var ri = 0; ri <= 8; ri += 1) {
          var rt = ri / 8;
          ctx.fillStyle = pal.steelDark;
          ctx.fillRect(Math.round(base.x - hw + hw * rt),
                       Math.round(ty + hh * rt - ph), 1, ph);
          ctx.fillRect(Math.round(base.x + hw - hw * rt),
                       Math.round(ty + hh * rt - ph), 1, ph);
        }
        linePath(ctx, [{ x: base.x - hw, y: ty }, { x: base.x, y: ty + hh },
                      { x: base.x + hw, y: ty }], 1, pal.steelLight);
        // 后缘：矮护栏，压住平台后侧，不挡建筑
        ctx.globalAlpha = 0.65;
        var bh = Math.max(1, Math.round(2 * z));
        for (var bi = 0; bi <= 8; bi += 1) {
          var bt = bi / 8;
          ctx.fillStyle = pal.steelDark;
          ctx.fillRect(Math.round(base.x - hw + hw * bt),
                       Math.round(ty - hh + hh * bt - bh), 1, bh);
          ctx.fillRect(Math.round(base.x + hw - hw * bt),
                       Math.round(ty - hh + hh * bt - bh), 1, bh);
        }
        linePath(ctx, [{ x: base.x - hw, y: ty }, { x: base.x, y: ty - hh },
                      { x: base.x + hw, y: ty }], 1, pal.steel);
        ctx.globalAlpha = 1;
      }
      return;
    }

    drawIsoBoxAt(ctx, base.x, baseY, hw, hh, h, top, left, right);
    // 勒脚：墙根压暗一档，接缝处补亮线，建筑稳稳落在地坪上
    var plinth = Math.max(2, Math.round(Math.min(h * 0.14, 5 * z)));
    fillPoly(ctx, [
      { x: base.x - hw, y: baseY - plinth }, { x: base.x, y: baseY + hh - plinth },
      { x: base.x, y: baseY + hh }, { x: base.x - hw, y: baseY },
    ], m.bg ? pal.bgSteelDark : (m.role === "rust" ? pal.rustDark : m.role === "glass" ? pal.glassDark : pal.steelDark));
    fillPoly(ctx, [
      { x: base.x, y: baseY + hh - plinth }, { x: base.x + hw, y: baseY - plinth },
      { x: base.x + hw, y: baseY }, { x: base.x, y: baseY + hh },
    ], m.bg ? pal.bgSteelDark : (m.role === "rust" ? pal.rustDark : m.role === "glass" ? pal.glassDark : pal.steelDark));
    linePath(ctx, [{ x: base.x - hw, y: baseY - plinth }, { x: base.x, y: baseY + hh - plinth },
                  { x: base.x + hw, y: baseY - plinth }], 1, top);

    // 墙面分缝：竖缝按面板宽度铺，横缝两道腰线
    if (h >= 10) {
      var step = m.bg ? 0.22 : (hw > 26 ? 0.09 : 0.14);
      var seamCol = m.bg ? pal.bgSteel : (m.role === "rust" ? pal.rustDark : pal.steelDark);
      for (var u = step; u < 0.96; u += step) {
        linePath(ctx, [
          { x: base.x - hw + hw * u, y: baseY - h + hh * u },
          { x: base.x - hw + hw * u, y: baseY + hh * u },
        ], 1, seamCol);
        linePath(ctx, [
          { x: base.x + hw * u, y: baseY - h + hh * (1 - u) },
          { x: base.x + hw * u, y: baseY + hh * (1 - u) },
        ], 1, m.bg ? pal.bgSteelDark : pal.steelMid);
      }
      ctx.globalAlpha = 0.55;
      [0.34, 0.64].forEach(function (vv) {
        linePath(ctx, [
          { x: base.x - hw, y: baseY - h + h * vv },
          { x: base.x, y: baseY - h + hh + h * vv },
          { x: base.x + hw, y: baseY - h + h * vv },
        ], 1, seamCol);
      });
      ctx.globalAlpha = 1;
      // 檐口：顶边一条亮线，把屋顶和墙面分开
      linePath(ctx, [{ x: base.x - hw, y: baseY - h }, { x: base.x, y: baseY - h + hh },
                    { x: base.x + hw, y: baseY - h }], Math.max(1, Math.round(z)),
                m.bg ? pal.bgSteelLight : pal.roofSlateLight);
    }

    // 墙面窗体：左面两排、右面一排；白天是冷灰玻璃，夜里亮暖窗
    if (m.windows && h >= 12) {
      var rows = h >= 40 ? 3 : h >= 26 ? 2 : 1;
      var n = Math.max(1, m.windows);
      var glassDay = m.bg ? pal.bgSteelDark : (m.role === "rust" ? pal.rustDark : pal.glass);
      var glassLit = m.bg ? pal.bgSteelLight : pal.window;
      function facePt(side, uu, vv) {
        if (side === 0) {
          return { x: base.x - hw + hw * uu, y: baseY - h + hh * uu + h * vv };
        }
        return { x: base.x + hw * uu, y: baseY - h + hh * (1 - uu) + h * vv };
      }
      function drawWin(side, u0, v0, du, dv) {
        fillPoly(ctx, [
          facePt(side, u0, v0), facePt(side, u0 + du, v0),
          facePt(side, u0 + du, v0 + dv), facePt(side, u0, v0 + dv),
        ], frame);
        fillPoly(ctx, [
          facePt(side, u0 + 0.012, v0 + 0.02), facePt(side, u0 + du - 0.012, v0 + 0.02),
          facePt(side, u0 + du - 0.012, v0 + dv - 0.02), facePt(side, u0 + 0.012, v0 + dv - 0.02),
        ], S.night ? glassLit : glassDay);
        // 玻璃上沿反光
        linePath(ctx, [facePt(side, u0 + 0.02, v0 + dv * 0.22),
                      facePt(side, u0 + du - 0.02, v0 + dv * 0.22)], 1,
                 S.night ? pal.window : pal.glassLight);
        // 窗台与中挺
        linePath(ctx, [facePt(side, u0, v0 + dv), facePt(side, u0 + du, v0 + dv)],
                 1, m.bg ? pal.bgSteelLight : pal.steelLight);
        linePath(ctx, [facePt(side, u0 + du / 2, v0), facePt(side, u0 + du / 2, v0 + dv)],
                 1, frame);
        if (S.night && !m.bg) {
          var wc = facePt(side, u0 + du / 2, v0 + dv / 2);
          ctx.globalAlpha = 0.20;
          fillOval(ctx, wc.x, wc.y, Math.max(3, hw * 0.20), Math.max(2, hw * 0.13), glassLit);
          ctx.globalAlpha = 1;
        }
      }
      for (var row = 0; row < rows; row += 1) {
        var v0 = rows === 1 ? 0.40 : rows === 2 ? (row === 0 ? 0.24 : 0.56) : (0.16 + row * 0.24);
        var dv = rows === 1 ? 0.20 : rows === 2 ? 0.16 : 0.12;
        for (var wi = 0; wi < n; wi += 1) {
          var u0 = n === 1 ? 0.44 : 0.14 + wi * (0.68 / (n - 1));
          drawWin(0, u0, v0, Math.min(0.12, 0.42 / n), dv);
        }
      }
      // 右面一列窗，数量减半，避免两面都花
      var rn = Math.max(1, Math.round(n / 2));
      for (var rj = 0; rj < rn; rj += 1) {
        var ru = rn === 1 ? 0.52 : 0.30 + rj * (0.40 / (rn - 1));
        drawWin(1, ru, 0.38, Math.min(0.10, 0.30 / rn), 0.18);
      }
    }

    // 大门 + 雨棚 + 门灯
    if (m.door) {
      var dpt = function (uu, vv) {
        return {
          x: base.x - hw + hw * uu,
          y: baseY - h + hh * uu + h * vv,
        };
      };
      fillPoly(ctx, [
        dpt(0.40, 0.58), dpt(0.60, 0.58), dpt(0.60, 1), dpt(0.40, 1),
      ], pal.ink);
      fillPoly(ctx, [
        dpt(0.415, 0.60), dpt(0.585, 0.60), dpt(0.585, 0.985), dpt(0.415, 0.985),
      ], pal.steelDark);
      // 卷帘门横条
      for (var sl = 0.64; sl < 0.97; sl += 0.055) {
        linePath(ctx, [dpt(0.42, sl), dpt(0.58, sl)], 1, pal.steelMid);
      }
      ctx.fillStyle = pal.paint;
      ctx.fillRect(Math.round(base.x - hw * 0.28), Math.round(baseY - h + h * 0.56 - 1),
                   Math.max(2, Math.round(hw * 0.12)), 1);
      // 雨棚（薄板挑出）
      fillPoly(ctx, [
        { x: base.x - hw * 0.34, y: baseY - h * 0.995 + hh * 0.34 - hh * 0.22 },
        { x: base.x - hw * 0.06, y: baseY - h * 0.995 + hh * 0.06 + hh * 0.42 },
        { x: base.x - hw * 0.06, y: baseY - h * 0.995 + hh * 0.06 + hh * 0.50 },
        { x: base.x - hw * 0.34, y: baseY - h * 0.995 + hh * 0.34 - hh * 0.14 },
      ], pal.roofSlateLight);
      // 门槛压条 + 门灯（夜里亮一盏暖光）
      linePath(ctx, [dpt(0.38, 0.985), dpt(0.62, 0.985)], 1, pal.concreteLight);
      if (S.night && !m.bg) {
        var dl = dpt(0.50, 0.615);
        ctx.fillStyle = pal.window;
        ctx.fillRect(Math.round(dl.x), Math.round(dl.y), 2, 1);
        ctx.globalAlpha = 0.22;
        fillOval(ctx, dl.x, dl.y, Math.max(3, Math.round(hw * 0.10)),
                 Math.max(2, Math.round(hw * 0.06)), pal.window);
        ctx.globalAlpha = 1;
      }
    }

    // 屋顶：锯齿 / 平顶 + 屋顶设备
    if (m.roof === "saw") {
      var roof = sawRoofFaces(base.x, baseY - h, hw, hh, Math.max(2, vlen(5)));
      roof.forEach(function (face) {
        var fc = face.side === "slope" ? (m.bg ? pal.bgSteelLight : pal.roofSlateLight)
          : face.side === "gable" ? (m.bg ? pal.bgSteel : pal.roofSlate)
            : (m.bg ? pal.bgSteelDark : (S.night ? pal.glassDark : pal.glass));
        fillPoly(ctx, face.points, fc);
        if (face.side === "slope") {
          // 压脊线与檐口线，再补两道檩条，坡面不再是一块平色
          linePath(ctx, [face.points[3], face.points[2]], 1,
                   m.bg ? pal.bgSteelLight : pal.roofSlateLight);
          linePath(ctx, [face.points[0], face.points[1]], 1,
                   m.bg ? pal.bgSteelDark : pal.roofSlateDark);
          ctx.globalAlpha = m.bg ? 0.32 : 0.30;
          for (var pr = 1; pr <= 2; pr += 1) {
            var pt = pr / 3;
            linePath(ctx, [
              { x: face.points[0].x + (face.points[3].x - face.points[0].x) * pt,
                y: face.points[0].y + (face.points[3].y - face.points[0].y) * pt },
              { x: face.points[1].x + (face.points[2].x - face.points[1].x) * pt,
                y: face.points[1].y + (face.points[2].y - face.points[1].y) * pt },
            ], 1, m.bg ? pal.bgSteelDark : pal.roofSlateDark);
          }
          ctx.globalAlpha = 1;
        } else if (face.side === "gable") {
          linePath(ctx, face.points.concat([face.points[0]]), 1,
                   m.bg ? pal.bgSteelDark : pal.roofSlateDark);
        }
        if (face.side === "glazing") {
          var mid = { x: (face.points[0].x + face.points[2].x) / 2,
                      y: (face.points[0].y + face.points[2].y) / 2 };
          ctx.fillStyle = S.night ? pal.window : pal.glassGlow;
          ctx.globalAlpha = S.night ? 0.28 : 0.35;
          ctx.fillRect(Math.round(mid.x) - 1, Math.round(mid.y) - 1, 2, 2);
          ctx.globalAlpha = 1;
        }
      });
    } else {
      // 平顶：檐口厚度 + 女儿墙 + 天窗 + 屋顶机组
      linePath(ctx, [{ x: base.x - hw, y: baseY - h }, { x: base.x, y: baseY - h - hh },
                    { x: base.x + hw, y: baseY - h }], Math.max(1, Math.round(z)),
                m.bg ? pal.bgSteelLight : pal.roofSlateLight);
      // 前檐压暗一档，内侧再补一条女儿墙亮线，顶板有厚度
      linePath(ctx, [{ x: base.x - hw, y: baseY - h + 1 },
                    { x: base.x, y: baseY - h + hh + 1 },
                    { x: base.x + hw, y: baseY - h + 1 }], 1,
                m.bg ? pal.bgSteelDark : pal.roofSlateDark);
      linePath(ctx, [{ x: base.x - hw + 1, y: baseY - h },
                    { x: base.x, y: baseY - h + hh - 1 },
                    { x: base.x + hw - 1, y: baseY - h }], 1,
                m.bg ? pal.bgSteelLight : pal.roofSlateLight);
      if (hw > 8) {
        var skx = base.x - hw * 0.28, sky = baseY - h + hh * 0.30;
        fillDiamond(ctx, skx, sky, Math.max(2, Math.round(hw * 0.18)),
                    Math.max(1, Math.round(hh * 0.16)), m.bg ? pal.bgSteelLight : pal.glassGlow);
        if (S.night && !m.bg) {
          ctx.globalAlpha = 0.16;
          fillOval(ctx, skx, sky, Math.max(3, Math.round(hw * 0.22)),
                   Math.max(2, Math.round(hh * 0.20)), pal.window);
          ctx.globalAlpha = 1;
        }
      }
      if (!m.bg && hw > 7) {
        drawIsoBoxAt(ctx, base.x + hw * 0.22, baseY - h + hh * 0.18 - hh * 0.20,
                     Math.max(2, Math.round(hw * 0.26)), Math.max(1, Math.round(hh * 0.26)),
                     Math.max(2, Math.round(3 * z)), pal.metalLight, pal.metal, pal.metalDark);
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(Math.round(base.x - hw * 0.40), Math.round(baseY - h - hh * 0.30),
                     Math.max(2, Math.round(hw * 0.16)), 1);
      }
      if (!m.bg && hw > 16) {
        // 第二台屋顶机组 + 跨顶管线
        drawIsoBoxAt(ctx, base.x - hw * 0.34, baseY - h + hh * 0.50,
                     Math.max(2, Math.round(hw * 0.12)), Math.max(1, Math.round(hh * 0.16)),
                     Math.max(2, Math.round(2.4 * z)), pal.metal, pal.metalDark, pal.metalDark);
        linePath(ctx, [{ x: base.x + hw * 0.02, y: baseY - h + hh * 0.20 },
                      { x: base.x + hw * 0.58, y: baseY - h + hh * 0.58 }], 1, pal.metalDark);
        linePath(ctx, [{ x: base.x + hw * 0.02, y: baseY - h + hh * 0.20 - 1 },
                      { x: base.x + hw * 0.58, y: baseY - h + hh * 0.58 - 1 }], 1, pal.metalLight);
      }
      if (m.bg && hw > 12) {
        // 远景大屋面也别是一块平板：补一个检修口/通风帽
        drawIsoBoxAt(ctx, base.x - hw * 0.24, baseY - h + hh * 0.24,
                     Math.max(2, Math.round(hw * 0.12)), Math.max(1, Math.round(hh * 0.12)),
                     Math.max(1, Math.round(2 * z)), pal.bgSteelLight, pal.bgSteel, pal.bgSteelDark);
      }
    }
    // 屋顶通风器 / 排气帽
    if (m.vent && !m.bg) {
      var ventCount = hw > 14 ? 3 : 2;
      for (var vv = 0; vv < ventCount; vv += 1) {
        var vx = base.x - hw * 0.36 + vv * hw * 0.34;
        var vy = baseY - h - hh * 0.42 + vv * hh * 0.30;
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(Math.round(vx), Math.round(vy - Math.max(2, 3 * z)),
                     Math.max(1, Math.round(2 * z)), Math.max(1, Math.round(3 * z)));
        ctx.fillStyle = pal.metalLight;
        ctx.fillRect(Math.round(vx), Math.round(vy - Math.max(2, 3 * z)), 2, 1);
      }
    }
    // 调度塔：控制室玻璃横带 + 顶冠竖梃
    if (m.tower) {
      var bandV0 = 0.27, bandV1 = 0.35;
      fillPoly(ctx, [
        { x: base.x - hw, y: baseY - h + h * bandV0 },
        { x: base.x, y: baseY - h + hh + h * bandV0 },
        { x: base.x, y: baseY - h + hh + h * bandV1 },
        { x: base.x - hw, y: baseY - h + h * bandV1 },
      ], pal.glassDark);
      linePath(ctx, [
        { x: base.x - hw, y: baseY - h + h * bandV0 },
        { x: base.x, y: baseY - h + hh + h * bandV0 },
      ], 1, pal.glassLight);
      for (var bw = 0; bw < 5; bw += 1) {
        var bu = 0.10 + bw * 0.19;
        ctx.fillStyle = S.night ? pal.window : pal.glassGlow;
        ctx.fillRect(Math.round(base.x - hw + hw * bu),
                     Math.round(baseY - h + h * bandV0 + hh * bu + h * 0.02), 2, 1);
        if (S.night) {
          ctx.globalAlpha = 0.18;
          fillOval(ctx, base.x - hw + hw * bu, baseY - h + h * bandV0 + hh * bu, 3, 2, pal.window);
          ctx.globalAlpha = 1;
        }
      }
    }
    if (m.crown) {
      for (var cu = 0.10; cu < 0.96; cu += 0.12) {
        linePath(ctx, [
          { x: base.x - hw + hw * cu, y: baseY - h + hh * cu },
          { x: base.x - hw + hw * cu, y: baseY + hh * cu },
        ], 1, pal.glassDark);
      }
      ctx.globalAlpha = S.night ? 0.30 : 0.20;
      fillDiamond(ctx, base.x, baseY - h, hw - 1, hh - 1, S.night ? pal.window : pal.glassGlow);
      ctx.globalAlpha = 1;
    }
    // 外露管线：沿左面下到勒脚
    if (m.pipes && !m.bg) {
      var pipeX = base.x - hw * 0.72;
      linePath(ctx, [{ x: pipeX, y: baseY - h * 0.96 }, { x: pipeX, y: baseY + hh * 0.28 }],
               1, pal.metalDark);
      linePath(ctx, [{ x: pipeX + 1, y: baseY - h * 0.96 }, { x: pipeX + 1, y: baseY + hh * 0.28 }],
               1, pal.metalLight);
      [0.20, 0.52, 0.84].forEach(function (pv) {
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(Math.round(pipeX - 1), Math.round(baseY - h * pv + hh * 0.10), 3, 1);
      });
    }
    // 附房雨棚 / 站台雨棚：加一道浅色挑檐
    if (m.canopy) {
      linePath(ctx, [{ x: base.x - hw, y: baseY - h + 1 }, { x: base.x, y: baseY - h + hh + 1 },
                    { x: base.x + hw, y: baseY - h + 1 }], 1, pal.mark);
    }
    // 站台围栏：两条前缘立短柱 + 横杆
    if (m.rail) {
      var railTop = baseY - h;
      var ph = Math.max(2, Math.round(4 * z));
      for (var ri = 0; ri <= 6; ri += 1) {
        var rt = ri / 6;
        var lx = base.x - hw + hw * rt;
        var ly = railTop + hh * rt;
        ctx.fillStyle = pal.steelDark;
        ctx.fillRect(Math.round(lx), Math.round(ly - ph), 1, ph);
        var rx2 = base.x + hw - hw * rt;
        ctx.fillRect(Math.round(rx2), Math.round(ly - ph), 1, ph);
      }
      linePath(ctx, [{ x: base.x - hw, y: railTop }, { x: base.x, y: railTop + hh }],
               1, pal.steelLight);
      linePath(ctx, [{ x: base.x, y: railTop + hh }, { x: base.x + hw, y: railTop }],
               1, pal.steelLight);
    }
    // 落水管 + 检修爬梯：给所有盒体建筑补一层工业立面细节
    if (h >= 9) {
      var trimDark = m.bg ? pal.bgSteelDark : pal.metalDark;
      var trimLight = m.bg ? pal.bgSteelLight : pal.metalLight;
      ctx.globalAlpha = m.bg ? 0.45 : 0.8;
      (m.bg ? [0.05] : [0.05, 0.95]).forEach(function (u) {
        var px = base.x - hw + hw * u;
        var yTop = baseY - h + hh * u + Math.max(1, Math.round(2 * z));
        var yBot = baseY + hh * u - 1;
        linePath(ctx, [{ x: px, y: yTop }, { x: px, y: yBot }], 1, trimDark);
        linePath(ctx, [{ x: px + 1, y: yTop }, { x: px + 1, y: yBot - 1 }], 1, trimLight);
        ctx.fillStyle = trimDark;
        ctx.fillRect(Math.round(px), Math.round(yBot - 1), 2, 1);
      });
      ctx.globalAlpha = 1;
      if (!m.bg && h >= 16 && hw >= 7) {
        // 高厂房右立面靠后侧：检修爬梯（压在窗列之外）
        var u0 = 0.84;
        var lx = base.x + hw * u0;
        var ins = hh * (1 - u0);
        var lt = baseY - h + ins + Math.max(1, Math.round(h * 0.09));
        var lb = baseY + ins - Math.max(1, Math.round(h * 0.02));
        ctx.fillStyle = trimDark;
        ctx.fillRect(Math.round(lx), Math.round(lt), 1, Math.round(lb - lt));
        ctx.fillRect(Math.round(lx) + 2, Math.round(lt), 1, Math.round(lb - lt));
        for (var rr = lt + 3; rr < lb; rr += Math.max(3, Math.round(4 * z))) {
          ctx.fillRect(Math.round(lx), Math.round(rr), 3, 1);
        }
        ctx.fillStyle = trimLight;
        ctx.fillRect(Math.round(lx) - 1, Math.round(lt) - 1, 5, 1);
      }
    }
  }

  // 坪面标线：安全区黄虚线、通道双黄线、轮胎痕与角落碎石。
  // 在铺装模型画完之后调用，写字楼/机台等主体随后覆盖其上，天然落在「地面」层。
  function drawYardMarkings(ctx, pal, m, baseX, topY, hw, hh) {
    var z = S.cam.zoom;
    if (m.hallBase) {
      ctx.globalAlpha = 0.30;
      linePath(ctx, [
        { x: baseX - hw * 0.88, y: topY }, { x: baseX, y: topY + hh * 0.88 },
        { x: baseX + hw * 0.88, y: topY }, { x: baseX, y: topY - hh * 0.88 },
        { x: baseX - hw * 0.88, y: topY },
      ], 1, pal.mark);
      for (var q = 0; q < 4; q += 1) {
        var ang = Math.PI / 2 * q + Math.PI / 4;
        linePath(ctx, [
          { x: baseX + Math.cos(ang) * hw * 0.62, y: topY + Math.sin(ang) * hh * 0.62 },
          { x: baseX + Math.cos(ang) * hw * 0.84, y: topY + Math.sin(ang) * hh * 0.84 },
        ], 2, pal.paint);
      }
      ctx.globalAlpha = 1;
      return;
    }
    var f = m.yard;
    if (!f) return;
    function yPoint(wx, wy) {
      var p = viewPoint(wx, wy);
      p.y -= Math.round(5 * z);
      return p;
    }
    function dashed(a, b, dash) {
      var len = Math.hypot(b.x - a.x, b.y - a.y) || 1;
      var n = Math.max(1, Math.floor(len / Math.max(3, dash)));
      for (var i = 0; i < n; i += 1) {
        var t0 = i / n, t1 = Math.min(1, (i + 0.52) / n);
        linePath(ctx, [
          { x: a.x + (b.x - a.x) * t0, y: a.y + (b.y - a.y) * t0 },
          { x: a.x + (b.x - a.x) * t1, y: a.y + (b.y - a.y) * t1 },
        ], 1, pal.paint);
      }
    }
    // 机台安全区：黄虚线框（收在菱形坪的前场内切矩形里）
    var zx0 = f.x - f.halfW * 0.42, zx1 = f.x + f.halfW * 0.42;
    var zy0 = f.y + f.halfH * 0.06, zy1 = f.y + f.halfH * 0.52;
    var c1 = yPoint(zx0, zy0), c2 = yPoint(zx1, zy0);
    var c3 = yPoint(zx1, zy1), c4 = yPoint(zx0, zy1);
    ctx.globalAlpha = 0.28;
    dashed(c1, c2, Math.max(6, 8 * z));
    dashed(c2, c3, Math.max(6, 8 * z));
    dashed(c3, c4, Math.max(6, 8 * z));
    dashed(c4, c1, Math.max(6, 8 * z));
    // 入口 → 分配枢纽：通道边线
    var e = yPoint(f.entrance.x, f.entrance.y);
    var hub = yPoint(f.hub.x, f.hub.y);
    linePath(ctx, [{ x: e.x - 1, y: e.y }, { x: hub.x - 1, y: hub.y }], 1, pal.paint);
    linePath(ctx, [{ x: e.x + 1, y: e.y }, { x: hub.x + 1, y: hub.y }], 1, pal.paint);
    // 通往主厂房的轮胎痕
    var door = yPoint(f.x - f.halfW * 0.30, f.y + f.halfH * 0.06);
    ctx.globalAlpha = 0.13;
    linePath(ctx, [{ x: e.x - 2, y: e.y }, { x: door.x - 2, y: door.y }], 1, pal.ink);
    linePath(ctx, [{ x: e.x + 2, y: e.y }, { x: door.x + 2, y: door.y }], 1, pal.ink);
    // 地井盖与排水格栅
    [[-0.34, 0.36], [0.12, 0.60]].forEach(function (o) {
      var hp = yPoint(f.x + f.halfW * o[0], f.y + f.halfH * o[1]);
      fillOval(ctx, hp.x, hp.y, Math.max(2, Math.round(3 * z)),
               Math.max(1, Math.round(2 * z)), pal.metalDark);
      ctx.fillStyle = pal.metal;
      ctx.fillRect(Math.round(hp.x - 1), Math.round(hp.y), 2, 1);
    });
    var gr = yPoint(f.x + f.halfW * 0.42, f.y + f.halfH * 0.46);
    for (var gi = 0; gi < 3; gi += 1) {
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(Math.round(gr.x - 3), Math.round(gr.y + gi), Math.max(3, Math.round(6 * z)), 1);
    }
    // 入口斑马线：黄黑相间的短斜杠
    ctx.globalAlpha = 0.55;
    for (var hz = 0; hz < 3; hz += 1) {
      linePath(ctx, [
        { x: e.x - 4 + hz * 4, y: e.y + 3 },
        { x: e.x + hz * 4, y: e.y - 3 },
      ], 1, hz % 2 ? pal.ink : pal.paint);
    }
    ctx.globalAlpha = 1;
    // 装货位：托盘位正前方一小块黄框（出车停靠的地方）
    if (f.dock) {
      var dk = yPoint(f.dock.x, f.dock.y);
      ctx.globalAlpha = 0.5;
      linePath(ctx, ringPts(dk.x, dk.y, Math.max(5, Math.round(9 * z)),
                            Math.max(2, Math.round(4.5 * z))), 1, pal.paint);
      ctx.globalAlpha = 1;
    }
    // 角落碎石与杂草点
    var rand = seeded(hashKey("yard" + f.id));
    ctx.globalAlpha = 0.45;
    for (var k = 0; k < 16; k += 1) {
      var wx = f.x + (rand() * 2 - 1) * f.halfW * 0.86;
      var wy = f.y + (rand() * 2 - 1) * f.halfH * 0.86;
      var pp = yPoint(wx, wy);
      ctx.fillStyle = rand() < 0.5 ? pal.soil : pal.groundDark;
      ctx.fillRect(Math.round(pp.x), Math.round(pp.y), 1, 1);
    }
    ctx.globalAlpha = 1;
  }

  function drawFurnaceModel(ctx, pal, m) {
    var z = S.cam.zoom, p = viewPoint(m.x, m.y);
    var y = p.y - (m.z0 || 0) * z, w = m.hw * z, d = m.hh * z, h = m.h * z;
    var c = roleCols(pal, m.role, m.bg);
    function at(u, v, lift) { return { x: p.x + u * w, y: y + v * d - lift * h }; }
    drawIsoBoxAt(ctx, p.x, y, w, d, h * 0.10, pal.concreteLight, pal.concrete, pal.concreteDark);
    // 各段共用同一组截面顶点，收腰炉身与上部炉喉之间不留裂缝。
    var levels = [[0.10, 0.64], [0.30, 0.86], [0.58, 0.74], [0.83, 0.38], [0.94, 0.38]];
    for (var i = 1; i < levels.length; i += 1) {
      var a = levels[i - 1], b = levels[i];
      fillPoly(ctx, [at(-a[1], 0, a[0]), at(0, a[1], a[0]),
                     at(0, b[1], b[0]), at(-b[1], 0, b[0])], c.left);
      fillPoly(ctx, [at(0, a[1], a[0]), at(a[1], 0, a[0]),
                     at(b[1], 0, b[0]), at(0, b[1], b[0])], c.right);
      linePath(ctx, [at(-b[1], 0, b[0]), at(0, b[1], b[0]), at(b[1], 0, b[0])], 1, c.top);
    }
    drawIsoBoxAt(ctx, p.x, y - h * 0.94, w * 0.40, d * 0.40, h * 0.06,
                 pal.metalLight, pal.metal, pal.metalDark);
    fillDiamond(ctx, p.x, y - h, w * 0.24, d * 0.24, pal.ink);
    [0.32, 0.65].forEach(function (height) {
      var deck = y - h * height;
      var inner = height < 0.5 ? 0.85 : 0.64;
      [-1, 1].forEach(function (side) {
        fillPoly(ctx, [{ x: p.x + side * w * 0.93, y: deck },
                       { x: p.x, y: deck + d * 0.93 },
                       { x: p.x, y: deck + d * inner },
                       { x: p.x + side * w * inner, y: deck }], pal.metalLight);
      });
      // 平台内侧炉身继续向上，前缘护栏立在走道外沿。
      for (var j = 0; j <= 4; j += 1) {
        var t = j / 4;
        var x = p.x - w * 0.93 + w * 0.93 * t, py = deck + d * 0.93 * t;
        linePath(ctx, [{ x: x, y: py }, { x: x, y: py - 3 * z }], 1, pal.metalDark);
      }
      linePath(ctx, [{ x: p.x - w * 0.93, y: deck - 3 * z },
                    { x: p.x, y: deck + d * 0.93 - 3 * z }], 1, pal.metalLight);
    });
    var lx = p.x + w * 0.80;
    linePath(ctx, [{ x: lx, y: y - h * 0.12 }, { x: lx, y: y - h * 0.72 }], 1, pal.metalDark);
    linePath(ctx, [{ x: lx + 2 * z, y: y - h * 0.12 }, { x: lx + 2 * z, y: y - h * 0.72 }], 1, pal.metalDark);
    for (var rung = 0.16; rung < 0.72; rung += 0.10) {
      linePath(ctx, [{ x: lx, y: y - h * rung }, { x: lx + 2 * z, y: y - h * rung }], 1, pal.metalLight);
    }
    linePath(ctx, [at(-0.75, 0, 0.25), at(0, 0.75, 0.25), at(0.75, 0, 0.25)],
             Math.max(1, 2 * z), pal.metalDark);
    [-0.48, 0, 0.48].forEach(function (u) {
      var nozzle = at(u, 0.64 - Math.abs(u) * 0.3, 0.23);
      fillOval(ctx, nozzle.x, nozzle.y, Math.max(1, z), Math.max(1, z), pal.metalLight);
    });
    var tap = at(-0.35, 0.35, 0.13);
    drawIsoBoxAt(ctx, tap.x, tap.y, w * 0.22, d * 0.22, h * 0.09,
                 pal.metalDark, pal.ink, pal.ink);
    // 炉口是装饰性余热，不表示任务正在运行。
    fillOval(ctx, tap.x, tap.y - h * 0.04, Math.max(1, w * 0.14), Math.max(1, h * 0.025), pal.rustLight);
    if (S.night) {
      ctx.globalAlpha = 0.24;
      fillOval(ctx, tap.x, tap.y, w * 0.45, d * 0.5, pal.window);
      ctx.globalAlpha = 1;
    }
  }

  function drawDistillationModel(ctx, pal, m) {
    var z = S.cam.zoom, p = viewPoint(m.x, m.y);
    var y = p.y - (m.z0 || 0) * z, r = m.r * z, h = m.h * z;
    var c = roleCols(pal, m.role, m.bg);
    var foot = h * 0.08, top = y - h * 0.94;
    drawIsoBoxAt(ctx, p.x, y, m.hw * z, m.hh * z, foot,
                 pal.concreteLight, pal.concrete, pal.concreteDark);
    fillOval(ctx, p.x, y - foot, r, r * 0.5, c.right);
    ctx.fillStyle = c.left;
    ctx.fillRect(Math.round(p.x - r), Math.round(top), Math.max(1, r * 2), h * 0.86);
    ctx.fillStyle = c.top;
    ctx.fillRect(Math.round(p.x - r * 0.65), Math.round(top), Math.max(1, r * 0.35), h * 0.86);
    ctx.fillStyle = c.right;
    ctx.fillRect(Math.round(p.x + r * 0.4), Math.round(top), Math.max(1, r * 0.6), h * 0.86);
    for (var seam = 0.16; seam < 0.94; seam += 0.13) {
      linePath(ctx, [{ x: p.x - r, y: y - h * seam },
                    { x: p.x, y: y - h * seam + r * 0.42 },
                    { x: p.x + r, y: y - h * seam }], 1, c.right);
    }
    fillOval(ctx, p.x, top, r, r * 0.5, c.top);
    fillOval(ctx, p.x, top - h * 0.025, r * 0.72, r * 0.34, c.top);
    linePath(ctx, [{ x: p.x, y: top - h * 0.02 }, { x: p.x, y: y - h }], Math.max(1, z), pal.metalLight);
    var count = m.platforms || 3;
    for (var i = 0; i < count; i += 1) {
      var dy = y - h * (0.28 + i * 0.25), width = m.hw * z;
      for (var arc = 0; arc < 10; arc += 1) {
        var a0 = Math.PI * arc / 10, a1 = Math.PI * (arc + 1) / 10;
        fillPoly(ctx, [
          { x: p.x + Math.cos(a0) * width, y: dy + Math.sin(a0) * width * 0.46 },
          { x: p.x + Math.cos(a1) * width, y: dy + Math.sin(a1) * width * 0.46 },
          { x: p.x + Math.cos(a1) * r, y: dy + Math.sin(a1) * r * 0.5 },
          { x: p.x + Math.cos(a0) * r, y: dy + Math.sin(a0) * r * 0.5 }
        ], pal.metalDark);
      }
      linePath(ctx, [{ x: p.x - width, y: dy }, { x: p.x, y: dy + width * 0.46 },
                    { x: p.x + width, y: dy }], Math.max(1, z), pal.metalLight);
      [-1, 0, 1].forEach(function (side) {
        var px = p.x + side * width, py = dy + (side ? 0 : width * 0.46);
        linePath(ctx, [{ x: px, y: py }, { x: px, y: py - 2.5 * z }], 1, pal.metalDark);
      });
      linePath(ctx, [{ x: p.x - width, y: dy - 2.5 * z },
                    { x: p.x, y: dy + width * 0.46 - 2.5 * z },
                    { x: p.x + width, y: dy - 2.5 * z }], 1, pal.metalLight);
    }
    var lx = p.x + r * 0.45, ly = y - h * 0.86;
    [0, 2 * z].forEach(function (off) {
      linePath(ctx, [{ x: lx + off, y: y - foot }, { x: lx + off, y: ly }], 1, pal.metalDark);
    });
    for (var step = 4; step < m.h * 0.80; step += 4) {
      linePath(ctx, [{ x: lx, y: y - step * z }, { x: lx + 2 * z, y: y - step * z }], 1, pal.metalLight);
    }
    var pipeX = p.x - r * 1.25;
    linePath(ctx, [{ x: p.x - r * 0.65, y: y - h * 0.65 },
                  { x: pipeX, y: y - h * 0.65 }, { x: pipeX, y: y - foot }], Math.max(1, z * 1.5), pal.metalDark);
    fillOval(ctx, pipeX, y - h * 0.2, Math.max(1, 1.5 * z), Math.max(1, z), pal.rustLight);
    if (S.night) {
      ctx.fillStyle = pal.window;
      ctx.fillRect(Math.round(p.x - r), Math.round(y - h * 0.78), 1, 1);
    }
  }

  function drawCylinderModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var rx = Math.max(2, Math.round(m.r * z));
    var ry = Math.max(1, Math.round(rx * 0.5));
    var h = Math.max(3, Math.round(m.h * z));
    var cols = roleCols(pal, m.role, m.bg);
    var top = cols.top, left = cols.left, right = cols.right;
    // 接地弧影 + 底座环
    fillOval(ctx, base.x, baseY + 1, Math.round(rx * 1.16), Math.round(ry * 1.16), pal.shadow);
    ctx.fillStyle = m.bg ? pal.bgSteelDark : pal.stoneDark;
    ctx.fillRect(base.x - rx, baseY - 2, rx * 2, 2);
    // 筒身：左中右三段受光 + 竖瓦楞
    fillOval(ctx, base.x, baseY, rx, ry, right);
    ctx.fillStyle = left;
    ctx.fillRect(base.x - rx, baseY - h, rx * 2, h);
    ctx.fillStyle = top;
    ctx.fillRect(base.x - Math.round(rx * 0.72), baseY - h, Math.round(rx * 0.44), h);
    ctx.fillStyle = right;
    ctx.fillRect(base.x + Math.round(rx * 0.30), baseY - h,
                 Math.max(1, Math.round(rx * 0.70)), h);
    if (rx > 4 && h > 8) {
      for (var c = 1; c < 6; c += 1) {
        var cx = base.x - rx + Math.round(rx * 2 * c / 6);
        linePath(ctx, [{ x: cx, y: baseY - h }, { x: cx, y: baseY }], 1,
                 c < 3 ? top : right);
      }
    }
    // 箍圈
    if (m.bands) {
      for (var i = 1; i <= m.bands; i += 1) {
        var by = Math.round(baseY - h * i / (m.bands + 1));
        ctx.fillStyle = right;
        ctx.fillRect(base.x - rx, by, rx * 2, 1);
        ctx.fillStyle = top;
        ctx.fillRect(base.x - Math.round(rx * 0.5), by, Math.round(rx * 0.52), 1);
        ctx.fillStyle = left;
        ctx.fillRect(base.x - rx, by + 1, rx * 2, 1);
      }
    }
    // 顶盖：穹顶带环口；烟囱是开口 + 警示环
    if (m.chimney) {
      fillOval(ctx, base.x, baseY - h, rx, ry, top);
      fillOval(ctx, base.x, baseY - h, Math.max(1, rx - 1), Math.max(1, ry - 1), pal.ink);
      linePath(ctx, [{ x: base.x - rx, y: baseY - h },
                    { x: base.x, y: baseY - h + ry },
                    { x: base.x + rx, y: baseY - h }], 1, pal.steelLight);
      ctx.fillStyle = pal.bad;
      ctx.fillRect(base.x - rx, baseY - Math.round(h * 0.86), rx * 2, Math.max(1, Math.round(z)));
      ctx.fillStyle = pal.mark;
      ctx.fillRect(base.x - rx, baseY - Math.round(h * 0.80), rx * 2, Math.max(1, Math.round(z)));
    } else {
      fillOval(ctx, base.x, baseY - h, rx, ry, top);
      fillOval(ctx, base.x, baseY - h, Math.max(1, rx - 2), Math.max(1, ry - 1), left);
      linePath(ctx, [{ x: base.x - Math.round(rx * 0.78), y: baseY - h },
                    { x: base.x, y: baseY - h - Math.round(ry * 0.72) },
                    { x: base.x + Math.round(rx * 0.78), y: baseY - h }], 1, top);
      // 顶部人孔与呼吸阀
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(base.x - 1, baseY - h - 1, 2, 2);
    }
    // 爬梯 + 护笼
    if (m.ladder) {
      var lx = base.x - Math.round(rx * 0.18);
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(lx, baseY - h + 2, 1, h - 3);
      ctx.fillRect(lx + Math.max(2, Math.round(rx * 0.22)), baseY - h + 2, 1, h - 3);
      for (var l = 3; l < h - 2; l += Math.max(3, Math.round(4 * z))) {
        ctx.fillRect(lx, baseY - h + l, Math.max(2, Math.round(rx * 0.22)), 1);
      }
      if (h > 18) {
        ctx.globalAlpha = 0.7;
        for (var cg = 6; cg < h - 4; cg += 8) {
          ctx.fillRect(lx - 1, baseY - h + cg, Math.max(4, Math.round(rx * 0.30)), 1);
        }
        ctx.globalAlpha = 1;
      }
    }
    // 侧向出料管：筒仓脚边一根短管
    if (m.silo) {
      linePath(ctx, [{ x: base.x + Math.round(rx * 0.55), y: baseY - Math.round(h * 0.12) },
                    { x: base.x + rx + Math.round(5 * z), y: baseY - Math.round(h * 0.12) },
                    { x: base.x + rx + Math.round(5 * z), y: baseY + 1 }],
               Math.max(1, Math.round(2 * z)), pal.metalDark);
      ctx.fillStyle = pal.metalLight;
      ctx.fillRect(base.x + rx + Math.round(5 * z), baseY - Math.round(h * 0.12), 1, 1);
    }
    if (S.night) {
      ctx.fillStyle = pal.window;
      ctx.fillRect(base.x - 1, baseY - h - ry - 1, 2, 2);
      ctx.globalAlpha = 0.25;
      fillOval(ctx, base.x, baseY - h - ry - 1, 4, 3, pal.window);
      ctx.globalAlpha = 1;
    }
  }

  function drawConveyorModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var pts = m.path.map(function (p) { return viewPoint(p.x, p.y); });
    var total = pathLength(pts);
    if (!total) return;
    var w = Math.max(2, Math.round(m.width * z));
    var bg = !!m.bg;
    var steel = bg ? pal.bgSteel : pal.steel;
    var steelDark = bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = bg ? pal.bgSteelLight : pal.steelLight;
    var beltCol = bg ? pal.bgSteel : pal.belt;
    var beltDark = bg ? pal.bgSteelDark : pal.beltDark;
    var beltMark = bg ? pal.bgSteelLight : pal.beltMark;
    var ore = bg ? pal.bgRust : pal.ore;
    var oreDark = bg ? pal.bgRustDark : pal.oreDark;
    // 支腿：双肢 + 斜撑 + 脚板；低矮支线不画腿，避免像栅栏
    var step = Math.max(8, vlen(20));
    for (var d = 0; m.support !== false && d < total; d += step) {
      var t = d / total;
      var pt = pointAt(pts, t);
      var hh = (m.h0 + (m.h1 - m.h0) * t) * z;
      if (hh < 3) continue;
      var legW = Math.max(2, Math.round(w * 0.62));
      ctx.fillStyle = steelDark;
      ctx.fillRect(Math.round(pt.x - legW), Math.round(pt.y - hh), 1, Math.max(2, Math.round(hh)));
      ctx.fillRect(Math.round(pt.x + legW), Math.round(pt.y - hh), 1, Math.max(2, Math.round(hh)));
      ctx.fillStyle = steel;
      ctx.fillRect(Math.round(pt.x), Math.round(pt.y - hh * 0.55), 1, 1);
      // 斜撑
      linePath(ctx, [{ x: pt.x - legW, y: pt.y - 2 },
                    { x: pt.x + legW, y: pt.y - hh + 1 }], 1, steel);
      linePath(ctx, [{ x: pt.x + legW, y: pt.y - 2 },
                    { x: pt.x - legW, y: pt.y - hh + 1 }], 1, steel);
      // 脚板
      ctx.fillStyle = steelLight;
      ctx.fillRect(Math.round(pt.x - legW - 1), Math.round(pt.y - 1), 4, 1);
    }
    // 带面：侧裙 + 皮带 + 滚轮刻线 + 边梁；刻线随帧缓慢移动
    for (var i = 1; i < pts.length; i += 1) {
      var a = pts[i - 1], b = pts[i];
      var ta = (i - 1) / Math.max(1, pts.length - 1);
      var tb = i / Math.max(1, pts.length - 1);
      var ya = a.y - (m.h0 + (m.h1 - m.h0) * ta) * z;
      var yb = b.y - (m.h0 + (m.h1 - m.h0) * tb) * z;
      var dx = b.x - a.x, dy = b.y - a.y;
      var len = Math.hypot(dx, dy) || 1;
      var nx = -dy / len * w / 2, ny = dx / len * w / 2;
      // 侧裙
      linePath(ctx, [{ x: a.x, y: ya + Math.max(2, Math.round(w * 0.42)) },
                    { x: b.x, y: yb + Math.max(2, Math.round(w * 0.42)) }],
               Math.max(1, Math.round(w * 0.66)), beltDark);
      // 带面
      fillPoly(ctx, [
        { x: a.x + nx, y: ya + ny }, { x: a.x - nx, y: ya - ny },
        { x: b.x - nx, y: yb - ny }, { x: b.x + nx, y: yb + ny },
      ], beltCol);
      // 皮带中缝与移动刻线
      linePath(ctx, [{ x: a.x + nx * 0.15, y: ya + ny * 0.15 },
                    { x: b.x + nx * 0.15, y: yb + ny * 0.15 }],
               Math.max(1, Math.round(w * 0.24)), m.ore ? steelDark : beltDark);
      if (!m.spur && w >= 3) {
        var phase = ((S.ticks * 0.7 + i * 3) % 14) / 14;
        var tick = pointAt([a, b], phase);
        var ty = ya + (yb - ya) * phase;
        ctx.fillStyle = beltMark;
        ctx.fillRect(Math.round(tick.x - nx * 0.55), Math.round(ty - ny * 0.55),
                     Math.max(1, Math.round(w * 0.34)), 1);
      }
      // 矿石
      if (m.ore) {
        var orePos = pointAt([a, b], 0.45);
        ctx.fillStyle = ore;
        ctx.fillRect(Math.round(orePos.x - w * 0.22),
                     Math.round((ya + yb) / 2 - Math.max(1, w * 0.22)),
                     Math.max(1, Math.round(w * 0.44)), Math.max(1, Math.round(w * 0.34)));
        ctx.fillStyle = oreDark;
        ctx.fillRect(Math.round(orePos.x - w * 0.10),
                     Math.round((ya + yb) / 2), Math.max(1, Math.round(w * 0.22)), 1);
        ctx.fillStyle = steelLight;
        ctx.fillRect(Math.round(orePos.x + w * 0.06),
                     Math.round((ya + yb) / 2 - Math.max(1, w * 0.22)), 1, 1);
      }
      // 边梁亮线
      linePath(ctx, [{ x: a.x + nx, y: ya + ny }, { x: b.x + nx, y: yb + ny }], 1, steelLight);
      linePath(ctx, [{ x: a.x - nx, y: ya - ny }, { x: b.x - nx, y: yb - ny }], 1, steelLight);
    }
    // 流向高光：只在主干带面上跑两枚低密度像素，给「调度厅 → 厂区」的方向感。
    // flowStart / flowEnd 来自切片时的原始路径距离，保证拐弯处不会跳回起点。
    if (!S.reduced && !m.spur && m.projectId && S.flowingProjects.has(m.projectId)
        && m.flowTotal > 0 && total > 2) {
      var flowTotal = m.flowTotal;
      var flowSpeed = m.ore ? 3.6 : 2.6;
      var flowPhase = (S.ticks * flowSpeed) % flowTotal;
      [flowPhase, (flowPhase + flowTotal * 0.5) % flowTotal].forEach(function (mark) {
        if (mark < m.flowStart || mark > m.flowEnd) return;
        var mt = (mark - m.flowStart) / Math.max(1, m.flowEnd - m.flowStart);
        var mp = pointAt(pts, mt);
        var mh = (m.h0 + (m.h1 - m.h0) * mt) * z;
        var mx = Math.round(mp.x), my = Math.round(mp.y - mh - 1);
        ctx.fillStyle = m.ore ? pal.paintLight : pal.glassGlow;
        ctx.fillRect(mx, my, Math.max(1, Math.round(z)), Math.max(1, Math.round(z)));
        ctx.fillRect(mx - Math.max(1, Math.round(z * 0.5)), my + 1,
                     Math.max(1, Math.round(z * 0.5)), 1);
      });
    }
    // 夜间边灯：沿带面每隔一段一颗暖黄小灯
    if (S.night && !bg) {
      for (var nd = 0; nd <= total; nd += Math.max(10, vlen(26))) {
        var nt = total ? nd / total : 0;
        var np = pointAt(pts, nt);
        np.y -= (m.h0 + (m.h1 - m.h0) * nt) * z;
        ctx.fillStyle = pal.window;
        ctx.fillRect(Math.round(np.x), Math.round(np.y) + 1, 1, 1);
      }
    }
  }

  function drawMachineModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(3, Math.round(m.hw * z));
    var hh = Math.max(1, Math.round(m.hh * z));
    var h = Math.max(4, Math.round(m.h * z));
    var line = m.line;
    var skid = Math.max(2, Math.round(2 * z));
    // 滑撬底座 + 主体
    fillDiamond(ctx, base.x, baseY, hw + Math.max(1, Math.round(z)), hh + 1, pal.ink);
    drawIsoBoxAt(ctx, base.x, baseY - skid, hw, hh, skid,
                 pal.steelLight, pal.metal, pal.metalDark);
    drawIsoBoxAt(ctx, base.x, baseY - skid, hw, hh, h,
                 pal.steelLight, pal.steel, pal.steelDark);
    // 侧板与散热格栅
    ctx.fillStyle = pal.steelMid;
    for (var gr = 1; gr <= 3; gr += 1) {
      linePath(ctx, [{ x: base.x - hw + hw * 0.18, y: baseY - h + hh * 0.18 + gr * h * 0.18 },
                    { x: base.x - hw + hw * 0.70, y: baseY - h + hh * 0.70 + gr * h * 0.18 }],
               1, pal.steelDark);
    }
    // 产线色带（顶沿）+ 反光：加粗到 2 格，远看是「一排机器」而不是「一排箱子」
    var tones = [pal.rust, pal.glassLight, pal.ok, pal.product];
    var tone = tones[Math.abs(hashKey(m.rec.key)) % tones.length];
    var bandH = Math.max(2, Math.round(z * 2));
    ctx.fillStyle = tone;
    ctx.fillRect(base.x - hw, baseY - skid - h, Math.max(1, hw * 2), bandH);
    ctx.fillStyle = pal.steelLight;
    ctx.fillRect(base.x - hw, baseY - skid - h, Math.max(1, hw * 2), 1);
    // 团队旗标：mode=team 的产线机台顶角挂一面小旗——「这台上是一支队伍在
    // 干活」。旗是身份标记，静态常挂（色带四色不含 paint，任何色带下都可辨）；
    // 旗面只在「本厂区有包裹在流」（S.flowingProjects，厂区级动效闸门，与
    // 传送带流向高光同一闸）且未开「减少动态效果」时两帧轻摆，其余时刻静止。
    if (line.is_team) {
      var flagX = base.x - hw;
      var flagY = baseY - skid - h;
      var flagH = Math.max(3, Math.round(5 * z));
      var flagW = Math.max(2, Math.round(4 * z));
      ctx.fillStyle = pal.metalDark;
      ctx.fillRect(flagX, flagY - flagH, 1, flagH + 1);
      var flutter = !S.reduced && line.status === "running"
        && m.rec && m.rec.project && S.flowingProjects.has(m.rec.project.id)
        && (Math.floor(S.ticks * 0.25) % 2 === 1);
      ctx.fillStyle = pal.paint;
      if (flutter) {
        ctx.fillRect(flagX + 1, flagY - flagH, flagW, 1);
        ctx.fillRect(flagX + 1, flagY - flagH + 1, Math.max(1, flagW - 1), 1);
      } else {
        ctx.fillRect(flagX + 1, flagY - flagH, flagW, 2);
      }
    }
    // 作业窗：左面一块玻璃。待机是暗玻璃，跑起来就亮成暖色——机台的「眼睛」，
    // 也是全景下唯一能一眼扫出「哪几台在干活」的落点。
    if (hw >= 5) {
      var winW = Math.max(2, Math.round(hw * 0.46));
      var winH = Math.max(2, Math.round(h * 0.26));
      var winX = base.x - hw + Math.round(hw * 0.2);
      var winY = baseY - skid - Math.round(h * 0.68);
      ctx.fillStyle = pal.ink;
      ctx.fillRect(winX - 1, winY - 1, winW + 2, winH + 2);
      ctx.fillStyle = line.status === "running" ? pal.product : pal.glassDark;
      ctx.fillRect(winX, winY, winW, winH);
      if (line.status === "running") {
        ctx.fillStyle = pal.sun;
        ctx.fillRect(winX, winY, Math.max(1, winW - 1), 1);
      }
    }
    // 排气管 + 顶罩：给机台一个「不是箱子」的剪影。
    // 放大到 23×12 之后，机台与场院上的成品箱、料箱在等高视角下轮廓太像，
    // 一根竖起来的排气管是最省像素的区分办法（跑起来还会冒一缕）。
    var pipeW = Math.max(1, Math.round(z * 1.5));
    var pipeH = Math.max(4, Math.round(h * 0.55));
    var pipeX = base.x + Math.round(hw * 0.42);
    var pipeTop = baseY - skid - h - pipeH;
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(pipeX, pipeTop, pipeW, pipeH);
    ctx.fillStyle = pal.steelMid;
    ctx.fillRect(pipeX, pipeTop, pipeW, 1);
    if (line.status === "running" && !stall && !S.reduced) {
      // 排气：一小团往上飘的暖白，节奏比烟囱快
      for (var pf = 0; pf < 2; pf += 1) {
        var pt2 = ((S.ticks * 0.09 + pf * 0.5) % 1);
        ctx.globalAlpha = (1 - pt2) * 0.5;
        fillOval(ctx, pipeX, pipeTop - pt2 * Math.max(4, 8 * z),
                 Math.max(1, (1 + pt2 * 2) * z), Math.max(1, (0.8 + pt2 * 1.6) * z), pal.smoke);
      }
      ctx.globalAlpha = 1;
    }
    // 右面控制柜：屏幕 + 按钮
    var cabW = Math.max(2, Math.round(hw * 0.42));
    var cabH = Math.max(2, Math.round(h * 0.44));
    var cabX = base.x + Math.max(1, Math.round(hw * 0.28));
    var cabY = baseY - skid - Math.max(1, Math.round(h * 0.82));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(cabX, cabY, cabW, cabH);
    ctx.fillStyle = line.status === "running" ? pal.glassGlow : pal.steelDark;
    ctx.fillRect(cabX + 1, cabY + 1, Math.max(1, cabW - 2), Math.max(1, Math.round(cabH * 0.42)));
    ctx.fillStyle = line.status === "running" ? pal.ok : pal.idle;
    ctx.fillRect(cabX + 1, cabY + cabH - 2, 1, 1);
    ctx.fillStyle = pal.paint;
    ctx.fillRect(cabX + cabW - 2, cabY + cabH - 2, 1, 1);
    // 状态灯柱。运行中被判成停滞时灯从绿转暗黄：这是「在跑但不健康」，
    // 与「失败」（红）和「正常在跑」（绿）都不同——它还没失败，只是不动了。
    var stall = line.status === "running" ? lineStall(line) : "";
    var lamp = pal.idle;
    if (line.status === "paused" || line.status === "stopped") lamp = pal.warn;
    else if (line.status === "failed" || line.last_status === "error") lamp = pal.bad;
    else if (line.status === "running") lamp = stall ? pal.warn : pal.ok;
    else if (line.last_status === "empty"
             || (line.health && line.health.last_outcome === "partial")) lamp = pal.warn;
    var poleH = Math.max(4, Math.round(6 * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(base.x + hw - 1, baseY - skid - h - poleH, 1, poleH + 1);
    ctx.fillStyle = lamp;
    ctx.fillRect(base.x + hw - 1, baseY - skid - h - poleH, 2, 2);
    if (line.status === "running") {
      // 正常运行的指示灯轻轻呼吸；停滞灯保持克制，减弱动态时两者都静止。
      var glowPulse = !S.reduced && !stall ? 0.82 + 0.18 * Math.sin(S.ticks * 0.10 + m.index) : 1;
      ctx.globalAlpha = (S.night ? 0.32 : 0.16) * glowPulse;
      fillOval(ctx, base.x + hw, baseY - skid - h - poleH, Math.max(3, 4 * z),
               Math.max(2, 3 * z), lamp);
      ctx.globalAlpha = 1;
    }
    // 快超时 / 已超时：机台顶部冒烟。烟从机台正上方走，不与状态灯抢位置。
    if (stall === "nearTimeout" || stall === "timeout") {
      var puff = stall === "timeout" ? 4 : 2;
      for (var sp = 0; sp < puff; sp += 1) {
        var st2 = ((S.ticks * (stall === "timeout" ? 0.06 : 0.045) + sp * 0.25) % 1);
        var sx2 = base.x + Math.sin(S.ticks * 0.05 + sp) * Math.max(1, 2.5 * z) - st2 * 2;
        var sy2 = baseY - skid - h - st2 * Math.max(8, 18 * z);
        var sr = Math.max(1, (1.4 + st2 * 3) * z);
        ctx.globalAlpha = (1 - st2) * (stall === "timeout" ? 0.55 : 0.32);
        fillOval(ctx, Math.round(sx2), Math.round(sy2), sr, sr * 0.75,
                 stall === "timeout" ? pal.ink : pal.smoke);
      }
      ctx.globalAlpha = 1;
    }
    // 运行中的活塞臂：小工位有节奏地往复
    if (line.status === "running") {
      var arm = Math.round(Math.sin(S.ticks * 0.9) * Math.max(1, hw * 0.22));
      ctx.fillStyle = pal.metalLight;
      ctx.fillRect(base.x - hw - Math.max(1, Math.round(z)) + arm,
                   baseY - skid - Math.max(2, Math.round(h * 0.52)), Math.max(2, Math.round(z * 2)), 1);
    }
    // ---- 在制：火花 + 料口灯带 ----
    // 只在「真的在跑且没卡住」时出现。以前机台在跑与待机的差别只有一盏 2px 的灯，
    // 全景下等于没有；火花是把「它在干活」喊出来的最便宜的像素。
    if (line.status === "running" && !stall) {
      for (var sk = 0; sk < 5; sk += 1) {
        var st3 = ((S.ticks * 0.14 + sk * 0.2) % 1);
        var sx4 = base.x - hw * 0.55 + Math.sin(S.ticks * 0.21 + sk * 1.7) * hw * 0.35;
        var sy4 = baseY - skid - h * 0.55 - st3 * Math.max(5, 9 * z);
        ctx.globalAlpha = (1 - st3) * 0.85;
        ctx.fillStyle = sk % 2 ? pal.sun : pal.rustLight;
        ctx.fillRect(Math.round(sx4), Math.round(sy4), 1, 1);
      }
      ctx.globalAlpha = 1;
      // 料口灯带：四格从右往左依次亮，像在「过料」
      var lit = 1 + (Math.floor(S.ticks / 3) % 4);
      for (var lb = 0; lb < 4; lb += 1) {
        ctx.fillStyle = lb < lit ? pal.product : pal.steelDark;
        ctx.fillRect(base.x - hw + Math.round(hw * 0.3 * lb) + Math.round(hw * 0.1),
                     baseY - skid - Math.round(h * 0.28), Math.max(1, Math.round(hw * 0.18)), 1);
      }
    }
    // ---- 待发沙漏：还有多久到点 ----
    // 「它自己会开工」这件事以前只在面板里有一行灰字。沙漏挂在机台左前角，
    // 越接近到点，上格沙越少、灯越急——不用读数字也知道「快来了」。
    if (line.kind === "cron" && line.status === "idle" && line.next_run_at) {
      var eta = Number(line.next_run_at) - nowMs() / 1000;
      if (eta > 0 && eta <= UPCOMING_GLYPH_S) {
        var fx0 = base.x - hw - Math.max(2, Math.round(3 * z));
        var fy0 = baseY - Math.max(6, Math.round(11 * z));
        var gw = Math.max(3, Math.round(3 * z));
        var gh = Math.max(4, Math.round(5 * z));
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(fx0 - gw, fy0 - gh, gw * 2, 1);
        ctx.fillRect(fx0 - gw, fy0 + gh, gw * 2, 1);
        ctx.fillRect(fx0 - gw, fy0 - gh, 1, gh * 2);
        ctx.fillRect(fx0 + gw - 1, fy0 - gh, 1, gh * 2);
        // 上格沙：越接近到点越少
        var frac = clamp(eta / UPCOMING_GLYPH_S, 0, 1);
        var fillH = Math.max(0, Math.round((gh - 1) * frac));
        ctx.fillStyle = frac < 0.12 ? pal.bad : pal.product;
        if (fillH > 0) ctx.fillRect(fx0 - gw + 1, fy0 - gh + 1, Math.max(1, gw * 2 - 2), fillH);
        // 下格沙：积起来的那些
        var accH = Math.max(0, Math.round((gh - 1) * (1 - frac) * 0.8));
        if (accH > 0) ctx.fillRect(fx0 - gw + 1, fy0 + gh - accH, Math.max(1, gw * 2 - 2), accH);
        // 最近 5 分钟：外圈跳一下，视线才会被拽过去
        if (frac < 0.17 && !S.reduced && (S.ticks % 8) < 4) {
          ctx.globalAlpha = 0.5;
          fillOval(ctx, fx0, fy0, Math.max(2, Math.round(4 * z)),
                   Math.max(1, Math.round(3 * z)), pal.warn);
          ctx.globalAlpha = 1;
        }
      }
    }
    // 出料滑槽 + 小成品箱
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(base.x - hw - Math.max(1, Math.round(2 * z)), baseY + hh - 1,
                 Math.max(2, Math.round(3 * z)), Math.max(1, Math.round(z)));
    if (hw > 4) {
      drawIsoBoxAt(ctx, base.x - hw - Math.max(1, Math.round(2 * z)), baseY + hh + Math.max(1, Math.round(z)),
                   Math.max(1, Math.round(2 * z)), Math.max(1, Math.round(z)),
                   Math.max(2, Math.round(3 * z)), pal.crateLight, pal.crate, pal.crateDark);
    }
    // 装配线：工位串在后沿短带上，带面本身就是依赖流向
    if (line.kind === "pipeline" && (line.nodes || []).length) {
      drawPipelineFlow(ctx, pal, m, base, baseY, hw, hh);
    }
    // hover 高亮：悬停这台机台时描一圈浅色轮廓（点击详情不变，无副作用）
    if (S.hover && S.hover.type === "line" && m.rec && S.hover.key === m.rec.key) {
      drawHoverRing(ctx, pal, base.x, baseY + 1, hw + vlen(3), hh + vlen(2));
    }
    m.rec._vx = base.x;
    m.rec._vy = baseY - skid - h - poleH - Math.max(4, vlen(6));
  }

  /** 装配线的工位流向：带面 + 逐工位的料状态。

      以前只画三颗灯珠，看得出「有几个工位」，看不出「谁在等谁」——而依赖流向
      正是装配线区别于单机产线的地方。现在：
      - 带面分三段着色：上游已出料的段是亮的，被失败 / 等待卡住的段是暗的；
      - 每个工位上方站一个小料箱，只有 `done` 的工位才有料流到下一段；
      - `blocked`（等待依赖）画成空位 + 暗灯，`ready` 画成待料的小空箱，
        `running` 画成料箱 + 活塞抖动，`done` 画成成品箱 + 亮灯；
      - 任一工位 `error`：它之后的整段带面画成断开（红色缺口），下游亮「等料」。
      以上全是表现层，节点状态机仍以 nodeStatusText 为准。 */
  function drawPipelineFlow(ctx, pal, m, base, baseY, hw, hh) {
    var z = S.cam.zoom;
    var nodes = m.line.nodes || [];
    var n = Math.min(PIPELINE_MAX_NODES, nodes.length);
    if (!n) return;
    var x0 = base.x - hw * 1.15;
    var span = hw * 2.3;
    var y = baseY + hh - Math.max(1, Math.round(2 * z));
    var stepPx = n > 1 ? span / (n - 1) : 0;
    // 找到第一个失败的工位：它之后的带面全部断开
    var breakAt = -1;
    for (var b = 0; b < n; b += 1) {
      if (nodes[b].status === "error" || nodes[b].status === "cancelled") { breakAt = b; break; }
    }
    // 带面分段：上游 done 越多，亮段越长
    for (var seg = 0; seg < n; seg += 1) {
      var ax = x0 + stepPx * seg;
      var bx2 = x0 + stepPx * (seg + 1);
      var lit = seg < n - 1 && nodes[seg].status === "done" && (breakAt < 0 || seg < breakAt);
      var col = lit ? pal.beltMark : pal.beltDark;
      if (seg === breakAt) col = pal.bad;   // 断口
      linePath(ctx, [{ x: ax, y: y }, { x: bx2, y: y }], Math.max(1, Math.round(z * 1.2)), col);
      // 卡料：断口前堆一小撮，表示料过不去
      if (seg === breakAt - 1 && breakAt > 0) {
        ctx.fillStyle = pal.crateDark;
        ctx.fillRect(Math.round(bx2 - Math.max(1, z)), Math.round(y - Math.max(1, z * 1.5)),
                     Math.max(1, Math.round(z * 1.5)), Math.max(1, Math.round(z * 1.5)));
      }
    }
    for (var i = 0; i < n; i += 1) {
      var sx = Math.round(x0 + stepPx * i);
      var node = nodes[i];
      var st = String(node.status || "");
      var blockedDown = breakAt >= 0 && i > breakAt;
      // 支柱
      ctx.fillStyle = pal.steelDark;
      ctx.fillRect(sx - Math.max(1, Math.round(z)), Math.round(y - Math.max(2, 3 * z)),
                   Math.max(2, Math.round(2 * z)), Math.max(2, Math.round(3 * z)));
      // 工位料箱
      var bw = Math.max(1, Math.round(2.2 * z));
      var boxY = y - Math.max(3, 4 * z);
      if (st === "done") {
        drawIsoBoxAt(ctx, sx, boxY, bw, Math.max(1, Math.round(bw * 0.5)), bw,
                     pal.product, pal.crate, pal.crateDark);
      } else if (st === "running") {
        var shake = S.reduced ? 0 : Math.round(Math.sin(S.ticks * 0.9 + i) * Math.max(0.6, z * 0.7));
        drawIsoBoxAt(ctx, sx, boxY + shake, bw, Math.max(1, Math.round(bw * 0.5)), bw,
                     pal.crateLight, pal.crate, pal.crateDark);
      } else if (st === "ready") {
        // 待料：空箱（只有框）
        linePath(ctx, ringPts(sx, boxY, bw, Math.max(1, Math.round(bw * 0.5))),
                 Math.max(1, Math.round(z)), pal.metalDark);
      }
      // 状态灯
      var nc = st === "running" ? pal.ok
        : st === "error" || st === "cancelled" ? pal.bad
          : st === "done" ? pal.steelLight
            : blockedDown ? pal.warn : pal.idle;
      ctx.fillStyle = nc;
      ctx.fillRect(sx, boxY - Math.max(2, 3 * z), Math.max(1, Math.round(z)), Math.max(1, Math.round(z)));
      // 下游等料：灯下挂一小段红色虚线，指着断口方向
      if (blockedDown && !S.reduced) {
        ctx.globalAlpha = 0.6;
        ctx.fillStyle = pal.warn;
        for (var d = 0; d < 2; d += 1) {
          ctx.fillRect(sx - Math.round(stepPx * 0.3) - d * Math.max(1, Math.round(2 * z)),
                       boxY - Math.max(3, 4 * z), Math.max(1, Math.round(z)), 1);
        }
        ctx.globalAlpha = 1;
      }
    }
  }

  function isToday(sec) {
    if (!sec) return false;
    var d = new Date(sec * 1000);
    var n = new Date();
    return d.getFullYear() === n.getFullYear() &&
      d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  }

  function drawPalletModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var w = Math.max(8, Math.round(15 * z));
    var h = Math.max(2, Math.round(3 * z));
    var hw = Math.round(w * 0.5), hh = Math.max(1, Math.round(h * 0.5));
    // 停放框：黄漆虚线 + 四角短标（纵向做窄：托盘本体只有一层板厚，
    // 停靠位就在它以南，框不能占掉叉车的落位）
    if (m.bay) {
      ctx.globalAlpha = 0.55;
      var bayHW = hw + Math.max(2, Math.round(2 * z));
      var bayHH = hh + Math.max(1, Math.round(1.5 * z));
      linePath(ctx, [{ x: base.x - bayHW, y: baseY }, { x: base.x, y: baseY + bayHH },
                    { x: base.x + bayHW, y: baseY }, { x: base.x, y: baseY - bayHH },
                    { x: base.x - bayHW, y: baseY }],
               1, pal.paint);
      ctx.globalAlpha = 1;
    }
    // 木托盘：底档 + 板条
    ctx.fillStyle = pal.crateDark;
    ctx.fillRect(base.x - hw, baseY - h, w, h);
    ctx.fillStyle = pal.crate;
    ctx.fillRect(base.x - hw + 1, baseY - h + 1, Math.max(1, w - 2), Math.max(1, h - 1));
    for (var slat = 1; slat <= 4; slat += 1) {
      ctx.fillStyle = pal.crateDark;
      ctx.fillRect(Math.round(base.x - hw + w * slat / 5), baseY - h + 1, 1, Math.max(1, h - 1));
    }
    ctx.fillStyle = pal.crateLight;
    ctx.fillRect(base.x - hw + 1, baseY - h, Math.max(1, w - 2), 1);
    // 取货托盘（pick）：只放一只可取箱——它是叉车的取货锚点，不摆成品堆，
    // 免得叉车与两垛高箱糊成一片、车斗也被遮住。
    if (m.pick) {
      var qb = Math.max(3, Math.round(5 * z));
      drawIsoBoxAt(ctx, base.x, baseY - h - Math.max(2, Math.round(qb * 1.4)), qb,
                   Math.max(1, Math.round(qb * 0.5)), Math.max(2, Math.round(qb * 1.4)),
                   pal.product, pal.crate, pal.crateDark);
      ctx.fillStyle = pal.crateLight;
      ctx.fillRect(base.x - qb, baseY - h - Math.max(4, Math.round(qb * 2.8)),
                   Math.max(1, qb * 2 - 1), 1);
      return;
    }
    // 成品垛：今天做成的件数 → 码几只托盘、每只码多高。
    // 以前一律 Math.min(5, done) 封顶 5 箱，做成 13 件和 5 件长得一样；
    // 现在一只托盘 6 箱（2 列 × 3 层），满一只就往左摆下一只，满到上限
    // 就插一面小绿旗——「今天拉走了多少」在场景里有了体积。
    var done = statFor(m.projectId).done || 0;
    var fullPallets = Math.min(PALLET_MAX, Math.floor(done / PALLET_SIZE));
    var partial = done % PALLET_SIZE;
    var stacks = [];
    for (var pi = 0; pi < fullPallets; pi += 1) stacks.push(PALLET_SIZE);
    if (partial && fullPallets < PALLET_MAX) stacks.push(partial);
    if (!stacks.length) stacks.push(0);
    // 成品垛只摆一只：菱形坪里空间留给装卸线，多出的件数在面板里报（不再向左铺第二垛）
    stacks = stacks.slice(0, 1);
    var bw = Math.max(3, Math.round(5 * z));
    stacks.forEach(function (count, si) {
      var px = base.x - si * Math.round(w * 0.95);
      var py = baseY - h;
      if (si > 0) {
        // 第二只托盘：底档单独画一小片，免得两台托盘粘成一块
        ctx.fillStyle = pal.crateDark;
        ctx.fillRect(px - Math.round(w * 0.42), py - Math.max(1, Math.round(2 * z)),
                     Math.round(w * 0.84), Math.max(1, Math.round(2 * z)));
      }
      for (var i = 0; i < count; i += 1) {
        var col = i % 2, row = Math.floor(i / 2);
        var bx = px - Math.round(bw * 1.15) + col * (bw + 1);
        var by = py - row * Math.round(bw * 1.35);
        drawIsoBoxAt(ctx, bx, by, bw, Math.max(1, Math.round(bw * 0.5)), Math.max(2, Math.round(bw * 1.4)),
                     pal.product, pal.crate, pal.crateDark);
        // 胶带 + 面单：这批货「有主」的感觉，也给成品一个高对比的顶面
        ctx.fillStyle = pal.crateLight;
        ctx.fillRect(bx - bw, by - Math.max(2, Math.round(bw * 1.4)), Math.max(1, bw * 2 - 1), 1);
        ctx.fillStyle = pal.mark;
        ctx.fillRect(bx + Math.max(1, Math.round(bw * 0.35)),
                     by - Math.max(2, Math.round(bw * 1.4)) + 1, Math.max(1, Math.round(bw * 0.5)), 1);
      }
      // 满垛小旗：满一托盘就插一面，一眼数得出「今天几车货」
      if (count >= PALLET_SIZE) {
        var flagH = Math.max(5, Math.round(9 * z));
        ctx.fillStyle = pal.metalDark;
        ctx.fillRect(px + Math.round(w * 0.34), py - Math.round(bw * 4.2) - flagH,
                     Math.max(1, Math.round(z)), flagH);
        ctx.fillStyle = pal.ok;
        ctx.fillRect(px + Math.round(w * 0.34) + 1, py - Math.round(bw * 4.2) - flagH,
                     Math.max(2, Math.round(4 * z)), Math.max(2, Math.round(3 * z)));
      }
    });
    // 小旗 + 托盘编号牌
    var poleX = base.x + Math.round(w / 2) - 1;
    var poleH = Math.max(4, Math.round(9 * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(poleX, baseY - h - poleH, 1, poleH);
    ctx.fillStyle = pal.ok;
    ctx.fillRect(poleX + 1, baseY - h - poleH, Math.max(2, Math.round(3 * z)),
                 Math.max(1, Math.round(2 * z)));
    ctx.fillStyle = pal.paint;
    ctx.fillRect(base.x - hw + 1, baseY - h - Math.max(1, Math.round(2 * z)),
                 Math.max(2, Math.round(3 * z)), 1);
  }

  function drawCrateModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var s = Math.max(2, Math.round(4 * z)) * (m.s || 1);
    fillOval(ctx, base.x, baseY + 1, s + 1, Math.max(1, Math.round(2 * z)), pal.shadow);
    drawIsoBoxAt(ctx, base.x, baseY, s, Math.max(1, s * 0.5), s * 2,
                 pal.crateLight, pal.crate, pal.crateDark);
    ctx.fillStyle = pal.crateDark;
    ctx.fillRect(base.x - 1, baseY - s * 2, 1, s * 2);
  }

  function drawBarrelModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var base = viewPoint(m.x, m.y);
    var baseY = base.y - Math.round((m.z0 || 0) * z);
    var s = Math.max(2, Math.round(3 * z)) * (m.s || 1);
    var h = Math.max(4, Math.round(7 * z));
    fillOval(ctx, base.x, baseY + 1, s + 1, Math.max(1, Math.round(2 * z)), pal.shadow);
    ctx.fillStyle = pal.crateDark;
    ctx.fillRect(base.x - s, baseY - h, s * 2, h);
    ctx.fillStyle = pal.crate;
    ctx.fillRect(base.x - s + 1, baseY - h + 1, Math.max(1, s * 2 - 2), Math.max(1, h - 2));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(base.x - s, baseY - Math.round(h * 0.7), s * 2, 1);
    ctx.fillRect(base.x - s, baseY - Math.round(h * 0.25), s * 2, 1);
    fillOval(ctx, base.x, baseY - h, s, Math.max(1, s * 0.5), pal.crateLight);
  }

  function drawOreModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var s = Math.max(3, Math.round(7 * z)) * (m.s || 1);
    var ore = m.bg ? pal.bgRust : pal.ore;
    var oreDark = m.bg ? pal.bgRustDark : pal.oreDark;
    var oreHi = m.bg ? pal.bgRustLight : pal.rustLight;
    fillOval(ctx, p.x, p.y, s * 1.4, s * 0.6, pal.shadow);
    fillOval(ctx, p.x, p.y - 1, s, s * 0.6, oreDark);
    fillOval(ctx, p.x - s * 0.3, p.y - 2, s * 0.7, s * 0.45, ore);
    ctx.fillStyle = oreHi;
    ctx.fillRect(Math.round(p.x - s * 0.2), Math.round(p.y - s * 0.7), 1, 1);
    ctx.fillRect(Math.round(p.x + s * 0.3), Math.round(p.y - s * 0.4), 1, 1);
  }

  /** hover 高亮：一圈静态虚线环（不闪，reduced 下同样可用）。 */
  function drawHoverRing(ctx, pal, x, y, rx, ry) {
    var pts = [];
    for (var a = 0; a < 12; a += 1) {
      var ang = Math.PI * 2 * a / 12;
      pts.push({ x: x + Math.cos(ang) * rx, y: y + Math.sin(ang) * ry });
    }
    pts.push(pts[0]);
    ctx.globalAlpha = 0.85;
    linePath(ctx, pts, 1, pal.mark);
    ctx.globalAlpha = 1;
  }

  function drawWorkerModel(ctx, pal, m) {
    // drawModel 分派入口（测试逐模型绘制走这里）：位姿按工厂 mood 现算
    var pose = m.factory
      ? workerPose(m.factory, nowMs(), S.reduced)
      : { x: m.x, y: m.y, state: "idle", wave: false, facing: 1 };
    drawWorkerPose(ctx, pal, m, pose, false);
  }

  /** 工头小羊：只按 workerPose 给的位姿渲染，绘制层零偏移——位移与画序
      （与机台 / 车辆的前后遮挡）由动态合并排序保证。wave 时举前蹄小幅摆动。 */
  function drawWorkerPose(ctx, pal, m, pose, hovered) {
    var z = S.cam.zoom;
    var sc = m.s || 1;
    var p = viewPoint(pose.x, pose.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var u = clamp(Math.round(2.8 * z), 2, 4);
    var size = Math.round(6 * u * sc);
    var fx0 = (pose.facing || 1) >= 0 ? 1 : -1;   // 脸 / 背心朝向（水平镜像）
    if (hovered) drawHoverRing(ctx, pal, p.x, baseY, size * 0.7, size * 0.34);
    fillOval(ctx, p.x, baseY + 1, Math.round(size * 0.5), Math.max(1, u), pal.shadow);
    ctx.save();
    ctx.translate(Math.round(p.x), Math.round(baseY));
    var top = -size;
    ctx.fillStyle = pal.sheepFace;
    ctx.fillRect(-Math.round(size * 0.30), -Math.round(size * 0.40), u, Math.round(size * 0.40));
    ctx.fillRect(Math.round(size * 0.18), -Math.round(size * 0.40), u, Math.round(size * 0.40));
    ctx.fillStyle = pal.sheep;
    ctx.fillRect(-Math.round(size * 0.52), top, Math.round(size * 0.9), Math.round(size * 0.5));
    ctx.fillStyle = pal.sheepShade;
    ctx.fillRect(-Math.round(size * 0.52), top + Math.round(size * 0.42),
                 Math.round(size * 0.9), Math.max(1, u));
    // 反光背心：让工头在灰坪上有个暖色落点
    ctx.fillStyle = pal.paint;
    ctx.fillRect(-Math.round(size * 0.40), top + Math.round(size * 0.28),
                 Math.round(size * 0.64), Math.max(1, u));
    ctx.fillStyle = pal.sheepFace;
    ctx.fillRect(fx0 * Math.round(size * 0.26), top + Math.round(size * 0.06),
                 Math.round(size * 0.42), Math.round(size * 0.34));
    ctx.fillStyle = pal.warn;
    ctx.fillRect(fx0 * Math.round(size * 0.08), top - Math.round(u * 0.6),
                 Math.round(size * 0.62), Math.max(1, u));
    ctx.fillRect(fx0 * Math.round(size * 0.18), top - Math.round(u * 1.6),
                 Math.round(size * 0.4), Math.max(1, u));
    // 挥手：前蹄举起小幅左右摆（reduced 不进入 wave，位姿层已挡住）
    if (pose.wave) {
      var sway = Math.round(Math.sin(S.ticks * 0.8) * u);
      var armX = fx0 * Math.round(size * 0.42) + sway;
      ctx.fillStyle = pal.sheep;
      ctx.fillRect(armX, top - Math.round(u * 2.2), Math.max(1, u - 1), Math.round(u * 1.8));
      ctx.fillStyle = pal.sheepFace;
      ctx.fillRect(armX, top - Math.round(u * 2.2) - 1, Math.max(1, u - 1), 1);
    }
    ctx.restore();
  }

  /** 出车（动态层）：按 tripPose 的 alpha 淡入淡出，朝向随 heading 镜像（全程恒定，
      倒车段由 reversing 标记，不做 180° 翻面）。装车箱码在 f.dockBed 锚点上——
      与叉车放下的那箱是同一个世界点，画出来不会错位。 */
  function drawTripTruck(ctx, pal, x, y, pose, hovered) {
    var z = S.cam.zoom;
    var z0 = Math.round((pose.z0 || 0) * z);
    ctx.globalAlpha = clamp(pose.alpha, 0, 1);
    if (hovered) {
      drawHoverRing(ctx, pal, x, y - z0, Math.max(10, Math.round(14 * z)),
                    Math.max(4, Math.round(5 * z)));
    }
    var wx = (x - S.bufW / 2) / S.cam.zoom + S.cam.x;
    var wy = (y - z0 - S.bufH / 2) / S.cam.zoom + S.cam.y;
    drawTruckModel(ctx, pal, { kind: "truck", x: wx, y: wy }, pose.heading < 0);
    var mx = pose.heading < 0 ? -1 : 1;
    var L = Math.max(10, Math.round(26 * z));
    // 装车箱：锚点 = 车斗近侧边缘（f.dockBed）的地面点，高度 = 车斗面（YARD_Z + FORK_LIFT）
    // ——与叉车放下的那箱同 y 同 z，落到斗上就是落在斗上，不会先落地再弹上车。
    var deckY = y + Math.round((pose.bedDy || 0) * z)
      - Math.round((YARD_Z + FORK_LIFT) * z);
    for (var i = 0; i < (pose.loaded || 0); i += 1) {
      var bx = x + mx * Math.round(L * 0.08) + (i % 2 ? Math.round(6 * z) : -Math.round(2 * z));
      var by = deckY - Math.round((i > 1 ? 4.5 : 0) * z);
      drawIsoBoxAt(ctx, bx, by, Math.max(2, Math.round(3.4 * z)),
                   Math.max(1, Math.round(1.7 * z)), Math.max(2, Math.round(4 * z)),
                   pal.product, pal.crate, pal.crateDark);
    }
    ctx.globalAlpha = 1;
  }

  /** 叉车（常驻动态层）：黄色小车身 + 门架 + 前叉，facing 定叉臂指向，车体按 pose.z0
      坐在厂坪上。载货箱不自己算位置——直接读 pose.cargo 的 (x, y, z)，投影时减去 z，
      因此货箱每帧都贴在叉尖上（升降是竖直量，不改地面 x/y）。不使用 ctx.scale。 */
  function drawForkliftModel(ctx, pal, x, y, pose, hovered) {
    var z = S.cam.zoom;
    var up = pose.facing === "north" ? -1 : 1;
    var bw = Math.max(3, Math.round(6 * z));
    var bh = Math.max(2, Math.round(3 * z));
    var baseY = y - Math.round((pose.z0 != null ? pose.z0 : YARD_Z) * z);
    if (hovered) drawHoverRing(ctx, pal, x, baseY, bw + vlen(3), bh + vlen(2));
    fillOval(ctx, x, baseY + 1, bw, Math.max(1, Math.round(2 * z)), pal.shadow);
    drawIsoBoxAt(ctx, x, baseY - Math.round(2 * z), bw, bh, Math.max(3, Math.round(4 * z)),
                 pal.paintLight, pal.paint, pal.paintDark);
    // 顶棚：两根立柱 + 一片棚
    var mastH = Math.max(3, Math.round(5 * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(x - bw + 1, baseY - Math.round(2 * z) - mastH, 1, mastH);
    ctx.fillRect(x + bw - 2, baseY - Math.round(2 * z) - mastH, 1, mastH);
    ctx.fillStyle = pal.warn;
    ctx.fillRect(x - bw + 1, baseY - Math.round(2 * z) - mastH - 1, bw * 2 - 1, 1);
    // 门架 + 前叉：伸向 facing 那一侧（地面点 = 车体 + up*FORK_REACH）
    var forkY = baseY + up * Math.round(FORK_REACH * z);
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(x - 1, forkY - Math.max(1, Math.round(z)), 2, Math.max(2, Math.round(2 * z)));
    ctx.fillStyle = pal.steelLight;
    ctx.fillRect(x - bw + 1, forkY, 2, 1);
    ctx.fillRect(x + bw - 3, forkY, 2, 1);
    // 载货：直接读 pose.cargo 的 (x, y, z)——地面 xy 不随升降变，z 是竖直抬升
    if (pose.carrying && pose.cargo) {
      var cp = viewPoint(pose.cargo.x, pose.cargo.y);
      var cy = cp.y - Math.round((pose.cargo.z || 0) * z);
      drawIsoBoxAt(ctx, cp.x, cy, Math.max(2, Math.round(bw * 0.6)),
                   Math.max(1, Math.round(bh * 0.5)), Math.max(2, Math.round(3 * z)),
                   pal.product, pal.crate, pal.crateDark);
    }
    ctx.fillStyle = pal.ink;
    ctx.fillRect(x - bw, baseY + Math.round(bh * 0.6), 2, 1);
    ctx.fillRect(x + bw - 2, baseY + Math.round(bh * 0.6), 2, 1);
  }

  function drawPylonModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var h = Math.max(10, Math.round(34 * z));
    var w = Math.max(3, Math.round(9 * z));
    var steel = pal.bgSteel, dark = pal.bgSteelDark;
    linePath(ctx, [{ x: p.x - w, y: p.y }, { x: p.x - Math.round(w * 0.35), y: p.y - h }], 1, dark);
    linePath(ctx, [{ x: p.x + w, y: p.y }, { x: p.x + Math.round(w * 0.35), y: p.y - h }], 1, dark);
    for (var i = 1; i <= 4; i += 1) {
      var yy = p.y - Math.round(h * i / 5);
      var ww = Math.round(w * (1 - i / 5) + 1);
      ctx.fillStyle = steel;
      ctx.fillRect(p.x - ww, yy, ww * 2, 1);
    }
    ctx.fillStyle = steel;
    ctx.fillRect(p.x - Math.round(w * 1.6), p.y - h + Math.round(h * 0.12), Math.round(w * 3.2), 1);
    ctx.fillStyle = pal.bgRust;
    ctx.fillRect(p.x - Math.round(w * 1.2), p.y - h + Math.round(h * 0.12) + 1, 1, 2);
    ctx.fillRect(p.x + Math.round(w * 1.2), p.y - h + Math.round(h * 0.12) + 1, 1, 2);
  }

  function drawRailModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var pts = m.path.map(function (q) { return viewPoint(q.x, q.y); });
    linePath(ctx, pts, Math.max(1, Math.round(2 * z)), pal.bgSteelDark);
    var total = pathLength(pts);
    for (var d = 0; d < total; d += Math.max(4, Math.round(8 * z))) {
      var pt = pointAt(pts, total ? d / total : 0);
      ctx.fillStyle = pal.bgSteel;
      ctx.fillRect(Math.round(pt.x - 2), Math.round(pt.y), 4, 1);
    }
  }

  function drawRockModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var s = Math.max(2, Math.round((m.s || 1) * 4 * z));
    fillOval(ctx, p.x, p.y + 1, s, Math.max(1, Math.round(s * 0.45)), pal.shadow);
    ctx.fillStyle = pal.bgSteelDark;
    ctx.fillRect(p.x - s, p.y - s, s * 2, s);
    ctx.fillStyle = pal.bgSteel;
    ctx.fillRect(p.x - s + 1, p.y - s, Math.max(1, s), Math.max(1, s - 1));
  }

  function drawShrubModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var s = Math.max(1, Math.round((m.s || 1) * 3 * z));
    ctx.fillStyle = pal.bgRustDark;
    ctx.fillRect(p.x - s, p.y - 1, s * 2, 1);
    ctx.fillStyle = pal.bgRust;
    ctx.fillRect(p.x - s + 1, p.y - 2, Math.max(1, s), 1);
    ctx.fillRect(p.x, p.y - 3, Math.max(1, s), 1);
  }

  function drawSkyline(ctx, pal, skyH, W) {
    var rand = seeded(0x51C1E);
    ctx.globalAlpha = S.night ? 0.45 : 0.38;
    ctx.fillStyle = pal.bgHaze;
    for (var i = 0; i < 16; i += 1) {
      var x = Math.round(i * W / 15 + rand() * 10);
      var w = Math.round(10 + rand() * 26);
      var h = Math.round(5 + rand() * 14);
      var shape = rand();
      if (shape > 0.8) {
        fillPoly(ctx, [
          { x: x, y: skyH }, { x: x + Math.round(w * 0.28), y: skyH - h },
          { x: x + Math.round(w * 0.72), y: skyH - h }, { x: x + w, y: skyH },
        ], pal.bgHaze);
      } else if (shape > 0.62) {
        fillPoly(ctx, [
          { x: x, y: skyH - h }, { x: x + Math.round(w / 2), y: skyH - h - 5 },
          { x: x + w, y: skyH - h },
        ], pal.bgHaze);
        ctx.fillRect(x, skyH - h, w, h);
      } else {
        ctx.fillRect(x, skyH - h, w, h);
      }
      if (rand() > 0.55) ctx.fillRect(x + Math.round(w * 0.3), skyH - h - 7, 1, 7);
    }
    ctx.globalAlpha = S.night ? 0.6 : 0.5;
    ctx.fillStyle = pal.bgSteel;
    for (var j = 0; j < 10; j += 1) {
      var x2 = Math.round(j * W / 9 + rand() * 16);
      var w2 = Math.round(14 + rand() * 34);
      var h2 = Math.round(8 + rand() * 20);
      ctx.fillRect(x2, skyH - h2, w2, h2);
      if (rand() > 0.55) {
        fillOval(ctx, x2 + Math.round(w2 * 0.68), skyH - h2 - 3,
                 Math.round(w2 * 0.16), 3, pal.bgRust);
      }
      if (S.night) {
        ctx.fillStyle = pal.window;
        for (var k = 0; k < 3; k += 1) ctx.fillRect(x2 + 4 + k * 6, skyH - h2 + 4, 1, 1);
        ctx.fillStyle = pal.bgSteel;
      }
    }
    ctx.globalAlpha = 1;
  }

  function drawGantryModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var w = Math.max(6, Math.round(m.w * z));
    var h = Math.max(8, Math.round(m.h * z));
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x - w, baseY - h, 2, h);
    ctx.fillRect(p.x + w - 2, baseY - h, 2, h);
    ctx.fillStyle = pal.steelLight;
    ctx.fillRect(p.x - w, baseY - h, w * 2, 2);
    ctx.fillStyle = pal.metal;
    for (var i = 1; i < 4; i += 1) {
      ctx.fillRect(p.x - w, baseY - h + Math.round(h * i / 4), w * 2, 1);
    }
    ctx.fillStyle = pal.warn;
    ctx.fillRect(p.x - 3, baseY - h + 2, 6, 2);
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x, baseY - h + 4, 1, Math.round(h * 0.4));
    ctx.fillStyle = pal.warn;
    ctx.fillRect(p.x - 1, baseY - h + 4 + Math.round(h * 0.4), 2, 2);
  }

  function drawCatwalkModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(6, Math.round(m.hw * z));
    var hh = Math.max(3, Math.round(m.hh * z));
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, Math.max(1, Math.round(2 * z)),
                 pal.steelLight, pal.steel, pal.steelDark);
    var ph = Math.max(2, Math.round(4 * z));
    for (var k = 0; k <= 4; k += 1) {
      var t = k / 4;
      ctx.fillStyle = pal.steelDark;
      ctx.fillRect(Math.round(p.x - hw + hw * t), Math.round(baseY + hh * t - ph), 1, ph);
      ctx.fillRect(Math.round(p.x + hw - hw * t), Math.round(baseY + hh * t - ph), 1, ph);
    }
    linePath(ctx, [{ x: p.x - hw, y: baseY }, { x: p.x, y: baseY + hh }], 1, pal.steelLight);
    linePath(ctx, [{ x: p.x, y: baseY + hh }, { x: p.x + hw, y: baseY }], 1, pal.steelLight);
  }

  function drawCoolingTowerModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - (m.z0 || 0) * z;
    var r = m.r * z, h = m.h * z;
    var cols = roleCols(pal, "concrete", m.bg);
    var levels = [
      { t: 0, r: 1 }, { t: 0.12, r: 0.90 }, { t: 0.30, r: 0.69 },
      { t: 0.52, r: 0.55 }, { t: 0.69, r: 0.53 }, { t: 0.84, r: 0.59 }, { t: 1, r: 0.72 },
    ];
    drawIsoBoxAt(ctx, p.x, baseY + 3 * z, r * 1.18, r * 0.52, 4 * z,
                 pal.stoneLight, pal.stone, pal.stoneDark);
    fillOval(ctx, p.x, baseY, r, r * 0.35, cols.right);
    for (var leg = 0; leg < 10; leg += 1) {
      var a = Math.PI * leg / 9;
      var lx = p.x + Math.cos(a) * r * 0.88;
      var ly = baseY + Math.sin(a) * r * 0.3;
      linePath(ctx, [{ x: lx, y: ly }, { x: lx - Math.cos(a) * 2 * z, y: ly - 8 * z }],
               Math.max(1, vlen(2)), cols.top);
    }
    var shellBase = baseY - 7 * z;
    // 每个圆环分成受光、过渡与背光面，轮廓收腰而非直筒。
    for (var band = 0; band < levels.length - 1; band += 1) {
      var lo = levels[band], hi = levels[band + 1];
      for (var face = 0; face < 12; face += 1) {
        var a0 = Math.PI * face / 12, a1 = Math.PI * (face + 1) / 12;
        function shellPoint(level, angle) {
          return { x: p.x + Math.cos(angle) * r * level.r,
                   y: shellBase - level.t * (h - 7 * z) + Math.sin(angle) * r * level.r * 0.32 };
        }
        fillPoly(ctx, [shellPoint(lo, a0), shellPoint(lo, a1), shellPoint(hi, a1), shellPoint(hi, a0)],
                 face < 3 ? cols.right : face < 7 ? cols.left : cols.top);
      }
    }
    for (var rib = 1; rib < 12; rib += 1) {
      var angle = Math.PI * rib / 12;
      var pts = levels.map(function (lv) {
        return { x: p.x + Math.cos(angle) * r * lv.r,
                 y: shellBase - lv.t * (h - 7 * z) + Math.sin(angle) * r * lv.r * 0.32 };
      });
      ctx.globalAlpha = rib % 3 === 0 ? 0.55 : 0.25;
      linePath(ctx, pts, 1, rib < 6 ? cols.right : cols.left);
    }
    ctx.globalAlpha = 1;
    var topY = baseY - h, rimR = r * 0.72;
    fillOval(ctx, p.x, topY, rimR, rimR * 0.34, cols.top);
    fillOval(ctx, p.x, topY, Math.max(1, rimR - 2 * z), Math.max(1, rimR * 0.34 - z), pal.ink);
    linePath(ctx, [{ x: p.x - rimR, y: topY }, { x: p.x - rimR * 0.6, y: topY + rimR * 0.24 },
                  { x: p.x + rimR * 0.6, y: topY + rimR * 0.24 }, { x: p.x + rimR, y: topY }], 1, cols.top);
    var ladderX = p.x + r * 0.28;
    linePath(ctx, [{ x: ladderX, y: shellBase + 2 * z }, { x: ladderX, y: topY + 3 * z }], 1, pal.metalDark);
    linePath(ctx, [{ x: ladderX + 3 * z, y: shellBase + 2 * z }, { x: ladderX + 3 * z, y: topY + 3 * z }], 1, pal.metalDark);
    for (var rung = 5; rung < m.h - 6; rung += 5) {
      linePath(ctx, [{ x: ladderX, y: baseY - rung * z }, { x: ladderX + 3 * z, y: baseY - rung * z }], 1, pal.metalLight);
    }
    if (S.night) fillOval(ctx, p.x - rimR * 0.8, topY, Math.max(1, z), Math.max(1, z), pal.bad);
  }

  function elevatedPoint(m, t, offset, lift) {
    var a = m.path[0], b = m.path[m.path.length - 1];
    var dx = b.x - a.x, dy = b.y - a.y;
    var length = Math.hypot(dx, dy) || 1;
    var p = viewPoint(a.x + dx * t - dy / length * offset, a.y + dy * t + dx / length * offset);
    p.y -= ((m.h0 + (m.h1 - m.h0) * t) + (lift || 0)) * S.cam.zoom;
    return p;
  }

  // 远景公用设施用雾化色，别让远处的桥和管廊比近景还抢眼
  function mutedPalette(pal) {
    var pc = Object.create(pal);
    pc.steel = pal.bgSteel; pc.steelDark = pal.bgSteelDark; pc.steelLight = pal.bgSteelLight;
    pc.rust = pal.bgRust; pc.rustDark = pal.bgRustDark; pc.rustLight = pal.bgRustLight;
    pc.metal = pal.bgSteel; pc.metalDark = pal.bgSteelDark; pc.metalLight = pal.bgSteelLight;
    return pc;
  }

  function drawRackSupports(ctx, pal, m) {
    var length = pathLength(m.path);
    var bays = Math.max(1, Math.ceil(length / 24));
    for (var i = 0; i <= bays; i += 1) {
      var t = i / bays;
      var left = elevatedPoint(m, t, -m.width * 0.58, 0);
      var right = elevatedPoint(m, t, m.width * 0.58, 0);
      var height = (m.h0 + (m.h1 - m.h0) * t) * S.cam.zoom;
      [left, right].forEach(function (pt) {
        linePath(ctx, [{ x: pt.x, y: pt.y + height }, pt], Math.max(1, vlen(2)), pal.steelDark);
        drawIsoBoxAt(ctx, pt.x, pt.y + height + 1, vlen(3), vlen(1.5), vlen(2),
                     pal.stoneLight, pal.stone, pal.stoneDark);
      });
      linePath(ctx, [left, right], Math.max(1, vlen(2)), pal.steelLight);
      if (i < bays) {
        var next = elevatedPoint(m, (i + 1) / bays, m.width * 0.58, 0);
        linePath(ctx, [{ x: right.x, y: right.y + height * 0.75 }, next], 1, pal.steel);
        linePath(ctx, [right, { x: next.x, y: next.y + height * 0.75 }], 1, pal.steelDark);
      }
    }
    return bays;
  }

  function drawPipeRackModel(ctx, pal, m) {
    var pc = m.bg ? mutedPalette(pal) : pal;
    var bays = drawRackSupports(ctx, pc, m);
    for (var pipe = 0; pipe < 3; pipe += 1) {
      var offset = (pipe - 1) * m.width * 0.30;
      var pts = [elevatedPoint(m, 0, offset, 3), elevatedPoint(m, 1, offset, 3)];
      linePath(ctx, pts, vlen(3), pipe === 1 ? pc.rustDark : pc.metalDark);
      linePath(ctx, pts.map(function (p) { return { x: p.x, y: p.y - 1 }; }),
               Math.max(1, vlen(1)), pipe === 1 ? pc.rustLight : pc.metalLight);
      for (var flange = 0; flange <= bays; flange += 1) {
        var pt = elevatedPoint(m, flange / bays, offset, 3);
        linePath(ctx, [{ x: pt.x - 1, y: pt.y - 2 }, { x: pt.x + 1, y: pt.y + 2 }], 1, pc.steelLight);
      }
    }
    var elbow = elevatedPoint(m, 1, 0, 3);
    var down = elevatedPoint(m, 1, 0, -m.h1 + 4);
    linePath(ctx, [elbow, down], vlen(3), pc.rustDark);
    linePath(ctx, [{ x: elbow.x - 1, y: elbow.y }, { x: down.x - 1, y: down.y }], 1, pc.rustLight);
    var valve = elevatedPoint(m, 0.52, 0, 5);
    linePath(ctx, [valve, { x: valve.x, y: valve.y - vlen(5) }], 1, pc.metalDark);
    fillOval(ctx, valve.x, valve.y - vlen(5), vlen(3), vlen(1.5), pc.rust);
  }

  function drawTransferBridgeModel(ctx, pal, m) {
    var pc = m.bg ? mutedPalette(pal) : pal;
    var bays = drawRackSupports(ctx, pc, m) * 2;
    var a = elevatedPoint(m, 0, 0, 0), b = elevatedPoint(m, 1, 0, 0);
    linePath(ctx, [{ x: a.x, y: a.y + vlen(3) }, { x: b.x, y: b.y + vlen(3) }], vlen(m.width), pc.steelDark);
    fillPoly(ctx, [elevatedPoint(m, 0, -m.width / 2, 0), elevatedPoint(m, 1, -m.width / 2, 0),
                   elevatedPoint(m, 1, m.width / 2, 0), elevatedPoint(m, 0, m.width / 2, 0)], pc.metalDark);
    for (var side = -1; side <= 1; side += 2) {
      var off = side * m.width / 2;
      linePath(ctx, [elevatedPoint(m, 0, off, 0), elevatedPoint(m, 1, off, 0)], 1, pc.steelLight);
      linePath(ctx, [elevatedPoint(m, 0, off, 11), elevatedPoint(m, 1, off, 11)], 1, pc.steelLight);
      for (var i = 0; i < bays; i += 1) {
        var t0 = i / bays, t1 = (i + 1) / bays;
        linePath(ctx, [elevatedPoint(m, t0, off, 0), elevatedPoint(m, t1, off, 11)], 1, pc.rust);
        linePath(ctx, [elevatedPoint(m, t0, off, 11), elevatedPoint(m, t1, off, 0)], 1, pc.steel);
      }
    }
    fillPoly(ctx, [elevatedPoint(m, 0, -m.width * 0.64, 12), elevatedPoint(m, 1, -m.width * 0.64, 12),
                   elevatedPoint(m, 1, 0, 15), elevatedPoint(m, 0, 0, 15)], pc.steelLight);
    fillPoly(ctx, [elevatedPoint(m, 0, 0, 15), elevatedPoint(m, 1, 0, 15),
                   elevatedPoint(m, 1, m.width * 0.64, 12), elevatedPoint(m, 0, m.width * 0.64, 12)], pc.steel);
    for (var panel = 1; panel < bays; panel += 1) {
      linePath(ctx, [elevatedPoint(m, panel / bays, -m.width * 0.64, 12),
                    elevatedPoint(m, panel / bays, 0, 15), elevatedPoint(m, panel / bays, m.width * 0.64, 12)], 1, pc.steelDark);
    }
    if (S.night) {
      for (var lamp = 0; lamp <= bays; lamp += 2) {
        var lp = elevatedPoint(m, lamp / bays, m.width * 0.58, 8);
        fillOval(ctx, lp.x, lp.y, 1, 1, pc.window);
      }
    }
  }

  // ---------- 厂外建筑：料场与罐区（前场两组，见 buildIndustrialSite） ----------
  // 约定与冷却塔 / 管廊一致：模型只记占地（hw/hh 或 r）与高度（h / z0），形态全在这里画。
  // 前场这两组不标 bg——bg 会被 roleCols 与雾化调色板压成远景色，摆在近景会发灰。

  /** 场院轴向取点：把组内坐标 (u 沿公路, v 纵深) 折到屏幕坐标。
      `m.axis` 是「沿公路方向」的世界单位向量（缺省 = 屏幕 x 轴向右）；纵深方向取它
      逆时针 90°，`v` 越大越远离公路那一侧。返回的点以 (x, baseY) 为原点，`up` 是
      模型自身的高度方向（往上为屏幕 -y），按像素给。 */
  function yardFrame(m, x, baseY, z) {
    var ax = m.axis ? m.axis.x : 1;
    var ay = m.axis ? m.axis.y : 0;
    var nx = -ay, ny = ax;
    return function (u, v, up) {
      return {
        x: x + Math.round((u * ax + v * nx) * z),
        y: baseY + Math.round((u * ay + v * ny) * z) - (up || 0),
      };
    };
  }

  /** 矮围栏：沿 path 立柱 + 两道横杆。料场用它把三排圈成一组——不铺场坪，
      范围靠这道线标出来、底面仍是沙地；靠公路那侧留出入口，门口另立一段高门柱。 */
  function drawFenceModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var h = Math.max(2, Math.round((m.h || 4) * z));
    var cols = roleCols(pal, m.role || "steel", m.bg);
    var postW = Math.max(1, Math.round(1.4 * z));
    var gap = Math.max(4, m.postGap || 6);
    var base = m.path.map(function (q) { var p = viewPoint(q.x, q.y); return { x: p.x, y: p.y }; });
    // 横杆先画（顶杆 + 腰杆），立柱压在上面
    [[h, cols.top], [Math.round(h * 0.55), cols.right]].forEach(function (bar) {
      linePath(ctx, base.map(function (p) { return { x: p.x, y: p.y - bar[0] }; }),
               Math.max(1, Math.round(1.6 * z)), bar[1]);
    });
    // 立柱：按世界步长沿 path 均布
    for (var i = 0; i < base.length - 1; i += 1) {
      var a = base[i], b = base[i + 1];
      var n = Math.max(1, Math.round(
        Math.hypot(b.x - a.x, b.y - a.y) / Math.max(1, gap * z)));
      for (var k = 0; k <= n; k += 1) {
        var t = k / n;
        var px = Math.round(a.x + (b.x - a.x) * t);
        var py = Math.round(a.y + (b.y - a.y) * t);
        ctx.fillStyle = k % 2 ? cols.right : cols.left;
        ctx.fillRect(px, py - h, postW, h);
      }
    }
  }

  /** 拱顶煤棚：高柱 + 平檐双坡屋盖（前侧全开）+ 后缘挡煤墙 + 棚下煤堆。
      檐口要比煤堆顶高出约一个坪深，煤堆才露得出来（否则屋盖把料整个盖住）。 */
  function drawCoalShedModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = m.hw || 30, hh = m.hh || 12;
    var h = Math.max(8, Math.round(m.h * z));
    var rise = Math.max(3, Math.round((m.rise || m.h * 0.22) * z));
    var cols = roleCols(pal, m.role || "steel", m.bg);
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var postW = Math.max(1, Math.round(1.8 * z));
    var at = yardFrame(m, p.x, baseY, z);
    // 菱形四角：L / R 是沿公路的两端，B 是后缘（远离公路那侧）、F 是敞口（朝公路）
    var L = at(-hw, 0), R = at(hw, 0), B = at(0, hh), F = at(0, -hh);
    // 后缘挡煤墙：两片矮墙，遮住煤堆后侧（前侧全开，料从这儿铲出去）
    var wallH = Math.max(3, Math.round(h * 0.42));
    fillPoly(ctx, [at(-hw, 0), at(0, hh), at(0, hh, wallH), at(-hw, 0, wallH)], cols.left);
    fillPoly(ctx, [at(0, hh), at(hw, 0), at(hw, 0, wallH), at(0, hh, wallH)], cols.right);
    linePath(ctx, [at(-hw, 0, wallH), at(0, hh, wallH), at(hw, 0, wallH)], 1, steelLight);
    // 煤堆：压在棚前半部（靠公路那侧），脊线在棚内、坡脚落到前檐外，正好露在开口里
    var ridgeUp = Math.max(3, Math.round(h * 0.30));
    var crownUp = Math.max(2, Math.round(3 * z));
    fillPoly(ctx, [
      at(-hw * 0.9, -hh * 1.02), at(hw * 0.9, -hh * 1.02),
      at(hw * 0.62, -hh * 0.42, ridgeUp), at(-hw * 0.62, -hh * 0.42, ridgeUp),
    ], pal.oreDark);
    fillPoly(ctx, [
      at(-hw * 0.62, -hh * 0.42, ridgeUp), at(hw * 0.62, -hh * 0.42, ridgeUp),
      at(hw * 0.40, -hh * 0.42, ridgeUp + crownUp),
      at(-hw * 0.40, -hh * 0.42, ridgeUp + crownUp),
    ], pal.ore);
    // 散落的煤块（确定性散布，不随帧变化）
    var rand = seeded(hashKey("coal" + m.x + ":" + m.y));
    for (var lump = 0; lump < 5; lump += 1) {
      var lumpPt = at((rand() * 2 - 1) * hw * 0.86, -hh * (0.55 + rand() * 0.62));
      ctx.fillStyle = lump % 2 ? pal.oreDark : pal.ink;
      ctx.fillRect(lumpPt.x, lumpPt.y, Math.max(1, Math.round(2 * z)), 1);
      ctx.fillRect(lumpPt.x + 1, lumpPt.y - 1, 1, 1);
    }
    // 六根立柱：四个角 + 左右两侧中点；檐口齐平（平檐双坡）
    [L, at(-hw * 0.5, hh * 0.5), B, R, at(hw * 0.5, hh * 0.5), F].forEach(function (node, ni) {
      ctx.fillStyle = ni === 0 || ni === 4 || ni === 5 ? steelDark : steelLight;
      ctx.fillRect(node.x, node.y - h, postW, h);
      ctx.fillStyle = steelDark;
      ctx.fillRect(node.x - 1, node.y - 1, postW + 2, 1);
    });
    // 屋盖：双坡（脊线沿公路方向，前后檐齐平），只画可见的两片坡面 + 一道檐口厚度
    var ridgeV = hh * 0.08, eaveV = hh * 0.92, ridgeUp = h + rise;
    fillPoly(ctx, [at(-hw * 0.94, ridgeV, ridgeUp), at(hw * 0.94, ridgeV, ridgeUp),
                   at(hw, eaveV, h), at(-hw, eaveV, h)], cols.top);
    fillPoly(ctx, [at(-hw * 0.94, ridgeV, ridgeUp), at(hw * 0.94, ridgeV, ridgeUp),
                   at(hw, -eaveV, h), at(-hw, -eaveV, h)], cols.left);
    linePath(ctx, [at(-hw * 0.94, ridgeV, ridgeUp), at(hw * 0.94, ridgeV, ridgeUp)],
             Math.max(1, Math.round(z)), steelLight);
    linePath(ctx, [at(-hw, -eaveV, h), at(hw, -eaveV, h)],
             Math.max(1, Math.round(z)), steelDark);
    linePath(ctx, [at(-hw, eaveV, h), at(hw, eaveV, h)],
             Math.max(1, Math.round(z)), cols.right);
    // 屋面檩条：前后两坡各两道，压住大片屋面色
    ctx.globalAlpha = 0.32;
    [1 / 3, 2 / 3].forEach(function (pt) {
      var up = h + rise * (1 - pt);
      linePath(ctx, [at(-hw * 0.94, ridgeV + (-eaveV - ridgeV) * pt, up),
                    at(hw * 0.94, ridgeV + (-eaveV - ridgeV) * pt, up)],
               1, steelDark);
      linePath(ctx, [at(-hw * 0.94, ridgeV + (eaveV - ridgeV) * pt, up),
                    at(hw * 0.94, ridgeV + (eaveV - ridgeV) * pt, up)],
               1, cols.right);
    });
    ctx.globalAlpha = 1;
    // 屋顶采光带：两块斜置天窗，夜里透出一点暖光
    [-0.34, 0.30].forEach(function (u0) {
      function rp(uu, tt) {
        return at(uu, ridgeV + (-eaveV - ridgeV) * tt, ridgeUp + (h - ridgeUp) * tt);
      }
      var u1 = u0 + 0.13;
      fillPoly(ctx, [rp(hw * u0, 0.30), rp(hw * u1, 0.30),
                     rp(hw * u1, 0.42), rp(hw * u0, 0.42)],
               S.night ? pal.glassDark : pal.glassGlow);
      linePath(ctx, [rp(hw * u0, 0.30), rp(hw * u1, 0.30)], 1, steelLight);
      if (S.night) {
        var sg = rp(hw * (u0 + 0.065), 0.36);
        ctx.globalAlpha = 0.16;
        fillOval(ctx, sg.x, sg.y, vlen(5), vlen(2.4), pal.window);
        ctx.globalAlpha = 1;
      }
    });
    // 脊部通风帽
    var ridgePt = at(0, ridgeV, ridgeUp);
    drawIsoBoxAt(ctx, ridgePt.x, ridgePt.y,
                 Math.max(2, Math.round(4 * z)), Math.max(1, Math.round(2 * z)),
                 Math.max(1, Math.round(2 * z)), steelLight, cols.left, steelDark);
    if (S.night) {
      var glow = at(0, 0, Math.max(3, Math.round(h * 0.34)));
      ctx.globalAlpha = 0.3;
      fillOval(ctx, glow.x, glow.y, vlen(10), vlen(5), pal.window);
      ctx.globalAlpha = 1;
      // 棚口再补一层暖光，让敞开的料堆在夜里读得出来
      var mouth = at(0, -hh * 0.7, h * 0.42);
      ctx.globalAlpha = 0.22;
      fillOval(ctx, mouth.x, mouth.y, vlen(9), vlen(4.5), pal.window);
      ctx.globalAlpha = 1;
      var edge = at(-hw, -hh * 0.9, h);
      ctx.fillStyle = pal.window;
      ctx.fillRect(edge.x + 2, edge.y, 2, 1);
    }
  }

  /** 筒仓群 + 斗式提升机塔：原料/成品仓，塔顶机房与筒仓顶用一条输送管连通。 */
  function drawSiloBatteryModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var count = Math.max(1, Math.round(m.count || 3));
    var gap = m.gapX || 16;                 // 沿公路方向的仓距（世界单位，见 yardFrame）
    var r = Math.max(3, Math.round(m.r * z));
    var h = Math.max(10, Math.round(m.h * z));
    var cols = roleCols(pal, m.role || "steel", m.bg);
    var body = m.bg ? pal.bgSteel : pal.steel;
    var bodyLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var bodyDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var leg = Math.max(2, Math.round(5 * z));
    var hopper = Math.max(3, Math.round(6 * z));
    var at = yardFrame(m, p.x, baseY, z);
    // 塔（斗式提升机）立在仓列沿公路方向的右端：细高 + 顶部机房
    var towerU = ((count - 1) / 2) * gap + (m.r || 0) * 2.1 + (m.towerGap || 0);
    var towerPt = at(towerU, 0);
    var towerW = Math.max(3, Math.round(7 * z));
    var towerH = h + Math.max(6, Math.round(12 * z));
    for (var i = 0; i < count; i += 1) {
      var c = at((i - (count - 1) / 2) * gap, (i % 2) * 2);
      var cx = c.x, cy = c.y;
      // 筒仓腿 + 锥斗：仓底悬空，物料从锥口落到下面的皮带
      fillOval(ctx, cx, cy + 1, Math.round(r * 1.1), Math.max(1, Math.round(r * 0.34)), pal.shadow);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(cx - Math.round(r * 0.8), cy - leg, Math.max(1, Math.round(1.5 * z)), leg);
      ctx.fillRect(cx + Math.round(r * 0.6), cy - leg, Math.max(1, Math.round(1.5 * z)), leg);
      fillPoly(ctx, [
        { x: cx - Math.round(r * 0.9), y: cy - leg }, { x: cx + Math.round(r * 0.9), y: cy - leg },
        { x: cx + Math.round(r * 0.34), y: cy - leg - hopper },
        { x: cx - Math.round(r * 0.34), y: cy - leg - hopper },
      ], bodyDark);
      // 仓身：三段受光 + 竖瓦楞 + 箍圈
      var bodyTop = cy - leg - hopper - h;
      var rx = r;
      fillOval(ctx, cx, cy - leg - hopper, rx, Math.max(1, Math.round(rx * 0.5)), body);
      ctx.fillStyle = body;
      ctx.fillRect(cx - rx, bodyTop, rx * 2, cy - leg - hopper - bodyTop);
      ctx.fillStyle = bodyLight;
      ctx.fillRect(cx - Math.round(rx * 0.66), bodyTop, Math.max(1, Math.round(rx * 0.42)),
                   cy - leg - hopper - bodyTop);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(cx + Math.round(rx * 0.36), bodyTop, Math.max(1, Math.round(rx * 0.64)),
                   cy - leg - hopper - bodyTop);
      for (var band = 1; band <= 3; band += 1) {
        var by = Math.round(bodyTop + (cy - leg - hopper - bodyTop) * band / 4);
        ctx.fillStyle = bodyDark;
        ctx.fillRect(cx - rx, by, rx * 2, 1);
        ctx.fillStyle = bodyLight;
        ctx.fillRect(cx - Math.round(rx * 0.6), by, Math.round(rx * 0.62), 1);
      }
      // 仓顶：穹顶 + 人孔 + 除尘帽
      fillOval(ctx, cx, bodyTop, rx, Math.max(1, Math.round(rx * 0.5)), bodyLight);
      fillOval(ctx, cx, bodyTop, Math.max(1, rx - Math.max(1, Math.round(2 * z))),
               Math.max(1, Math.round(rx * 0.5) - 1), body);
      linePath(ctx, [{ x: cx - Math.round(rx * 0.7), y: bodyTop },
                    { x: cx, y: bodyTop - Math.max(2, Math.round(rx * 0.5)) },
                    { x: cx + Math.round(rx * 0.7), y: bodyTop }], 1, bodyLight);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(cx - 1, bodyTop - Math.max(2, Math.round(rx * 0.5)) - 1, 3, 2);
      if (S.night) {
        // 冷白投光在仓身左侧留一道窄高光
        ctx.globalAlpha = 0.30;
        ctx.fillStyle = pal.glassGlow;
        ctx.fillRect(cx - rx, bodyTop + 1, 1,
                     Math.max(2, Math.round((cy - leg - hopper - bodyTop) * 0.55)));
        ctx.globalAlpha = 1;
        ctx.fillStyle = pal.bad;
        ctx.fillRect(cx - 1, bodyTop - Math.max(3, Math.round(rx * 0.5)) - 2, 2, 1);
      }
      // 仓身检修梯：放大了才画，全景时保持干净
      if (rx >= 4) {
        var lx2 = cx - rx + 1;
        var lt = bodyTop + 2;
        var lb = cy - leg - hopper - 1;
        ctx.fillStyle = bodyDark;
        ctx.fillRect(lx2, lt, 1, lb - lt);
        ctx.fillRect(lx2 + 2, lt, 1, lb - lt);
        for (var lr = lt + 3; lr < lb; lr += 3) ctx.fillRect(lx2, lr, 3, 1);
      }
    }
    // 顶部输送管：机房 → 各仓顶（略高于仓顶，接在塔身上段，沿公路方向）
    var pipeUp = leg + hopper + h + Math.max(3, Math.round(6 * z));
    var pipeRoot = at(towerU, 0, pipeUp);
    var pipeEnd = at(-(count - 1) / 2 * gap, 0, pipeUp);
    linePath(ctx, [{ x: pipeRoot.x - Math.round(towerW * 0.5), y: pipeRoot.y }, pipeEnd],
             Math.max(1, Math.round(2 * z)), bodyDark);
    linePath(ctx, [{ x: pipeRoot.x - Math.round(towerW * 0.5), y: pipeRoot.y - 1 },
                  { x: pipeEnd.x, y: pipeEnd.y - 1 }],
             1, bodyLight);
    // 提升机塔
    fillOval(ctx, towerPt.x, towerPt.y + 1, Math.round(towerW * 1.3), Math.max(1, Math.round(towerW * 0.5)), pal.shadow);
    ctx.fillStyle = bodyDark;
    ctx.fillRect(towerPt.x - towerW, towerPt.y - towerH, towerW * 2, towerH);
    ctx.fillStyle = bodyLight;
    ctx.fillRect(towerPt.x - Math.round(towerW * 0.6), towerPt.y - towerH, Math.max(1, Math.round(towerW * 0.6)), towerH);
    ctx.fillStyle = body;
    ctx.fillRect(towerPt.x + Math.round(towerW * 0.4), towerPt.y - towerH, Math.max(1, Math.round(towerW * 0.6)), towerH);
    for (var seg = 1; seg <= 4; seg += 1) {
      var sy = Math.round(towerPt.y - towerH * seg / 5);
      ctx.fillStyle = bodyDark;
      ctx.fillRect(towerPt.x - towerW, sy, towerW * 2, 1);
    }
    // 机房（顶部大一号的箱体）
    var houseH = Math.max(4, Math.round(10 * z));
    drawIsoBoxAt(ctx, towerPt.x, towerPt.y - towerH, Math.round(towerW * 1.5), Math.round(towerW * 0.75), houseH,
                 m.bg ? pal.bgSteelLight : pal.roofSlateLight, body, bodyDark);
    if (S.night) {
      // 塔顶平台灯 + 机房窗 + 航空障碍灯
      ctx.globalAlpha = 0.5;
      ctx.fillStyle = pal.glassGlow;
      ctx.fillRect(towerPt.x - towerW, towerPt.y - towerH + 1, towerW * 2, 1);
      ctx.globalAlpha = 1;
      ctx.fillStyle = pal.window;
      ctx.fillRect(towerPt.x - Math.round(towerW * 0.4), towerPt.y - towerH - houseH + 1, 2, 2);
      ctx.fillStyle = pal.bad;
      ctx.fillRect(towerPt.x + 1, towerPt.y - towerH - houseH - 2, 2, 1);
    }
    if (towerW >= 4) {
      // 塔身外梯
      var tlx = towerPt.x - towerW - 1;
      ctx.fillStyle = bodyDark;
      ctx.fillRect(tlx, towerPt.y - towerH + 4, 1, towerH - 5);
      ctx.fillRect(tlx + 2, towerPt.y - towerH + 4, 1, towerH - 5);
      for (var tly = towerPt.y - towerH + 6; tly < towerPt.y - 2;
           tly += Math.max(3, Math.round(4 * z))) {
        ctx.fillRect(tlx, tly, 3, 1);
      }
      // 塔脚控制箱
      drawIsoBoxAt(ctx, towerPt.x + towerW + 3, towerPt.y - 1,
                   Math.max(2, Math.round(3 * z)), Math.max(1, Math.round(1.6 * z)),
                   Math.max(2, Math.round(3 * z)),
                   pal.steelLight, pal.steel, pal.steelDark);
      if (S.night) {
        ctx.fillStyle = pal.window;
        ctx.fillRect(towerPt.x + towerW + 3, towerPt.y - Math.max(2, Math.round(3 * z)), 2, 1);
      }
    }
    // 侧向出料管：塔脚一根落地管
    linePath(ctx, [{ x: towerPt.x + towerW, y: towerPt.y - Math.round(towerH * 0.24) },
                  { x: towerPt.x + towerW + Math.max(2, Math.round(4 * z)), y: towerPt.y - Math.round(towerH * 0.24) },
                  { x: towerPt.x + towerW + Math.max(2, Math.round(4 * z)), y: towerPt.y + 1 }],
             Math.max(1, Math.round(2 * z)), cols.right);
  }

  /** 露天煤堆：截顶四棱锥，顶面与正面受光，散几块煤。 */
  function drawCoalPileModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(4, Math.round((m.hw || 12) * z));
    var hh = Math.max(2, Math.round((m.hh || 6) * z));
    var h = Math.max(2, Math.round((m.h || 8) * z));
    var top = Math.max(1, Math.round(hw * 0.52));
    var topH = Math.max(1, Math.round(hh * 0.52));
    var topY = baseY - h;
    var W = { x: p.x - hw, y: baseY }, N = { x: p.x, y: baseY - hh };
    var E = { x: p.x + hw, y: baseY }, SP = { x: p.x, y: baseY + hh };
    var WT = { x: p.x - top, y: topY }, NT = { x: p.x, y: topY - topH };
    var ET = { x: p.x + top, y: topY }, ST = { x: p.x, y: topY + topH };
    fillDiamond(ctx, p.x, baseY + 1, Math.round(hw * 1.04), Math.round(hh * 1.04), pal.oreDark);
    fillPoly(ctx, [W, N, NT, WT], pal.oreDark);
    fillPoly(ctx, [N, E, ET, NT], pal.oreDark);
    fillPoly(ctx, [W, SP, ST, WT], pal.ore);
    fillPoly(ctx, [SP, E, ET, ST], pal.ore);
    fillDiamond(ctx, p.x, topY, top, topH, pal.ore);
    linePath(ctx, [WT, ST, ET], 1, pal.oreLight);
    // 散落的煤块（确定性散布，不随帧变化）
    var rand = seeded(hashKey("coalpile" + m.x + ":" + m.y));
    for (var i = 0; i < 7; i += 1) {
      var lu = rand() * 2 - 1;
      var lv = rand() * 2 - 1;
      var lx = p.x + Math.round(lu * hw * 0.8);
      var ly = topY + Math.round(lv * topH * 0.8);
      ctx.fillStyle = i % 3 ? pal.ink : pal.oreLight;
      ctx.fillRect(lx, ly, Math.max(1, Math.round(2 * z)), 1);
    }
  }

  /** 地磅：低混凝土台 + 深色秤板 + 黄黑端线 + 计量立杆。 */
  function drawWeighbridgeModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(6, Math.round((m.hw || 15) * z));
    var hh = Math.max(3, Math.round((m.hh || 7) * z));
    var h = Math.max(1, Math.round((m.h || 1.6) * z));
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, h,
                 pal.concreteLight, pal.concrete, pal.concreteDark);
    fillDiamond(ctx, p.x, baseY - h, Math.round(hw * 0.88), Math.round(hh * 0.82), pal.metalDark);
    linePath(ctx, [{ x: p.x - hw + 1, y: baseY - h },
                  { x: p.x, y: baseY - h + hh - 1 },
                  { x: p.x + hw - 1, y: baseY - h }], 1, pal.warn);
    linePath(ctx, [{ x: p.x - Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.28) },
                  { x: p.x + Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.28) }],
             1, pal.metal);
    linePath(ctx, [{ x: p.x - Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.66) },
                  { x: p.x + Math.round(hw * 0.8), y: baseY - h + Math.round(hh * 0.66) }],
             1, pal.metal);
    // 两端黄黑警示条：做大一档，卡车压上去后仍能从台缘读出来
    [-1, 1].forEach(function (side) {
      var ex = p.x + Math.round(hw * 0.9) * side;
      for (var s = 0; s < 4; s += 1) {
        ctx.fillStyle = s % 2 ? pal.ink : pal.warn;
        ctx.fillRect(ex - Math.round(hw * 0.18) + s * Math.max(2, Math.round(3 * z)),
                     baseY - h + Math.round(hh * 0.18), Math.max(2, Math.round(3 * z)),
                     Math.max(1, Math.round(2 * z)));
      }
    });
    // 计量立杆（台侧）：细杆 + 状态灯
    var postX = p.x + Math.round(hw * 0.94);
    var postY = baseY - h + Math.round(hh * 0.5);
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(postX, postY - Math.round(8 * z), 1, Math.round(8 * z));
    ctx.fillStyle = pal.warn;
    ctx.fillRect(postX - 1, postY - Math.round(9 * z), 3, 2);
    if (S.night) {
      ctx.globalAlpha = 0.45;
      fillOval(ctx, postX, postY - Math.round(9 * z), vlen(4), vlen(2), pal.window);
      ctx.globalAlpha = 1;
    }
  }

  /** 冷白投光灯：细高杆 + 横臂双灯头，地面光池由 drawLightPools 统一画。 */
  function drawFloodlightModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(12, Math.round((m.h || 32) * z));
    var arm = Math.max(4, Math.round(6 * z));
    ctx.fillStyle = pal.stoneDark;
    ctx.fillRect(p.x - 2, baseY - 1, 4, 2);
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x, baseY - h, Math.max(1, Math.round(1.6 * z)), h);
    ctx.fillStyle = pal.metal;
    ctx.fillRect(p.x - 1, baseY - Math.round(h * 0.45), Math.max(2, Math.round(2.6 * z)), 1);
    ctx.fillRect(p.x - arm, baseY - h, arm * 2 + 1, Math.max(1, Math.round(1.4 * z)));
    [-arm, arm - Math.max(2, Math.round(3 * z))].forEach(function (dx) {
      var hx = p.x + dx;
      ctx.fillStyle = pal.metal;
      ctx.fillRect(hx, baseY - h + 1, Math.max(2, Math.round(3 * z)), Math.max(1, Math.round(2 * z)));
      ctx.fillStyle = S.night ? pal.glassGlow : pal.metalLight;
      ctx.fillRect(hx, baseY - h + 2, Math.max(2, Math.round(2 * z)), 1);
      if (S.night) {
        ctx.globalAlpha = 0.28;
        fillOval(ctx, hx + 1, baseY - h + 2, vlen(4), vlen(2.5), pal.glassGlow);
        ctx.globalAlpha = 1;
      }
    });
  }

  /** 湿式气柜：三节套筒互相套着升降，外套导架 + 外梯，罐顶放散管。 */
  function drawGasholderModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var r = Math.max(4, Math.round(m.r * z));
    var h = Math.max(12, Math.round(m.h * z));
    var cols = roleCols(pal, m.role || "rust", m.bg);
    var dark = m.bg ? pal.bgSteelDark : pal.metalDark;
    var light = m.bg ? pal.bgSteelLight : pal.metalLight;
    // 基础环
    ctx.fillStyle = m.bg ? pal.bgSteelDark : pal.stoneDark;
    ctx.fillRect(p.x - Math.round(r * 1.1), baseY - 2, Math.round(r * 2.2), 2);
    fillDiamond(ctx, p.x, baseY - 2, Math.round(r * 1.16), Math.max(1, Math.round(r * 0.58)),
                m.bg ? pal.bgSteel : pal.stone);
    // 导架：两根后立柱先画（被罐体挡住一半）
    var frameH = h + Math.max(3, Math.round(5 * z));
    [-0.86, 0.86].forEach(function (side) {
      var fx = p.x + Math.round(r * side);
      ctx.fillStyle = dark;
      ctx.fillRect(fx, baseY - frameH, Math.max(1, Math.round(1.4 * z)), frameH);
    });
    // 三节套筒：底节最粗，往上逐节变细，露出上一节的上沿
    var sections = [
      { rr: 1.0, top: 0.50 }, { rr: 0.90, top: 0.70 }, { rr: 0.80, top: 0.86 },
    ];
    sections.forEach(function (sec) {
      var sr = Math.round(r * sec.rr), sh = Math.round(h * sec.top);
      var ry = Math.max(1, Math.round(sr * 0.5));
      fillOval(ctx, p.x, baseY, sr, ry, cols.right);
      ctx.fillStyle = cols.left;
      ctx.fillRect(p.x - sr, baseY - sh, sr * 2, sh);
      ctx.fillStyle = cols.top;
      ctx.fillRect(p.x - Math.round(sr * 0.70), baseY - sh, Math.max(1, Math.round(sr * 0.40)), sh);
      ctx.fillStyle = cols.right;
      ctx.fillRect(p.x + Math.round(sr * 0.32), baseY - sh, Math.max(1, Math.round(sr * 0.68)), sh);
      // 上沿环口：套筒的标志
      fillOval(ctx, p.x, baseY - sh, sr, ry, cols.top);
      fillOval(ctx, p.x, baseY - sh, Math.max(1, sr - Math.max(1, Math.round(1.6 * z))),
               Math.max(1, ry - 1), cols.right);
      linePath(ctx, [{ x: p.x - sr, y: baseY - sh }, { x: p.x, y: baseY - sh + ry },
                    { x: p.x + sr, y: baseY - sh }], 1, light);
    });
    // 罐顶：穹顶 + 放散管 + 人孔
    var topY = baseY - Math.round(h * 0.86);
    var topR = Math.round(r * 0.8);
    for (var cap = 0; cap < 3; cap += 1) {
      var cr = Math.round(topR * (1 - cap * 0.24));
      fillOval(ctx, p.x, topY - Math.round(cap * 3 * z), cr, Math.max(1, Math.round(cr * 0.5)),
               cap === 0 ? cols.top : light);
    }
    linePath(ctx, [{ x: p.x, y: topY - Math.round(9 * z) },
                  { x: p.x, y: topY - Math.round(13 * z) }], Math.max(1, Math.round(1.5 * z)), dark);
    linePath(ctx, [{ x: p.x - 1, y: topY - Math.round(13 * z) }, { x: p.x + 1, y: topY - Math.round(13 * z) }],
             1, light);
    // 前柱压在罐体上：前缘的两根导架柱（比后柱亮一档即可，别亮成白线）
    [-0.86, 0.86].forEach(function (side) {
      var fx = p.x + Math.round(r * side);
      ctx.fillStyle = m.bg ? pal.bgSteel : pal.steel;
      ctx.fillRect(fx, baseY - frameH, Math.max(1, Math.round(1.4 * z)), Math.round(frameH * 0.9));
    });
    // 顶部导架环 + 外梯
    linePath(ctx, [{ x: p.x - Math.round(r * 0.86), y: baseY - frameH },
                  { x: p.x + Math.round(r * 0.86), y: baseY - frameH }],
             Math.max(1, Math.round(1.4 * z)), light);
    var ladderX = p.x + Math.round(r * 0.34);
    linePath(ctx, [{ x: ladderX, y: baseY + 2 }, { x: ladderX, y: topY + 2 }], 1, dark);
    linePath(ctx, [{ x: ladderX + Math.max(2, Math.round(2.4 * z)), y: baseY + 2 },
                  { x: ladderX + Math.max(2, Math.round(2.4 * z)), y: topY + 2 }], 1, dark);
    for (var rung = 4; rung < h - 4; rung += Math.max(3, Math.round(4 * z))) {
      linePath(ctx, [{ x: ladderX, y: baseY - rung * z },
                    { x: ladderX + Math.max(2, Math.round(2.4 * z)), y: baseY - rung * z }], 1, light);
    }
    // 中间检修平台
    var deckY = baseY - Math.round(h * 0.50);
    linePath(ctx, [{ x: p.x - Math.round(r * 0.95), y: deckY },
                  { x: p.x, y: deckY + Math.max(1, Math.round(r * 0.48)) },
                  { x: p.x + Math.round(r * 0.95), y: deckY }], Math.max(1, Math.round(1.4 * z)), light);
    if (S.night) {
      ctx.fillStyle = pal.bad;
      ctx.fillRect(p.x - 1, topY - Math.round(15 * z), 2, 2);
      ctx.globalAlpha = 0.3;
      fillOval(ctx, p.x, topY - Math.round(15 * z), vlen(5), vlen(3), pal.bad);
      ctx.globalAlpha = 1;
    }
    // 出口管：气柜侧下方的短管接到管廊
    linePath(ctx, [{ x: p.x - Math.round(r * 0.6), y: baseY - Math.round(h * 0.14) },
                  { x: p.x - r - Math.max(2, Math.round(4 * z)), y: baseY - Math.round(h * 0.14) }],
             Math.max(1, Math.round(2 * z)), cols.right);
  }

  /** 循环水池：下沉水面 + 池壁 + 栏杆 + 溢流堰；夜里水面反灯。 */
  function drawBasinModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(8, Math.round(m.hw * z));
    var hh = Math.max(5, Math.round(m.hh * z));
    var wall = Math.max(2, Math.round((m.h || 3) * z));
    var concrete = roleCols(pal, "concrete", m.bg);
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    // 池壁：外圈台阶 + 内壁两片暗面
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, wall, concrete.top, concrete.left, concrete.right);
    var iw = Math.max(4, hw - Math.max(2, Math.round(5 * z)));
    var ih = Math.max(3, hh - Math.max(1, Math.round(2.5 * z)));
    fillDiamond(ctx, p.x, baseY - wall, iw, ih, m.bg ? pal.bgSteelDark : pal.glassDark);
    fillDiamond(ctx, p.x, baseY - wall - Math.max(1, Math.round(2 * z)),
                Math.max(2, iw - vlen(3)), Math.max(2, ih - vlen(2)), pal.glass);
    // 涟漪：两道横向淡线 + 一道高光
    ctx.globalAlpha = 0.55;
    linePath(ctx, [{ x: p.x - iw * 0.6, y: baseY - wall - vlen(4) },
                  { x: p.x + iw * 0.2, y: baseY - wall - vlen(5) }], 1, pal.glassLight);
    linePath(ctx, [{ x: p.x - iw * 0.1, y: baseY - wall - vlen(7) },
                  { x: p.x + iw * 0.62, y: baseY - wall - vlen(7) }], 1, pal.glassLight);
    ctx.globalAlpha = 1;
    // 溢流堰：一角的小方口 + 侧向排水管
    var weirX = p.x + Math.round(iw * 0.58);
    ctx.fillStyle = concrete.top;
    ctx.fillRect(weirX - vlen(4), baseY - wall - vlen(2), vlen(8), Math.max(1, Math.round(1.5 * z)));
    linePath(ctx, [{ x: p.x + hw - 1, y: baseY - wall - vlen(2) },
                  { x: p.x + hw + vlen(4), y: baseY - wall - vlen(2) },
                  { x: p.x + hw + vlen(4), y: baseY + 1 }], Math.max(1, vlen(2)), steelDark);
    // 栏杆：沿左右两缘（前缘留出检修口）
    for (var ri = 0; ri <= 5; ri += 1) {
      var t = ri / 5;
      ctx.fillStyle = steelDark;
      ctx.fillRect(Math.round(p.x - hw + hw * t), Math.round(baseY - wall + ih * t - vlen(4)), 1, vlen(4));
      ctx.fillRect(Math.round(p.x + hw - hw * t), Math.round(baseY - wall + ih * t - vlen(4)), 1, vlen(4));
    }
    ctx.globalAlpha = 0.85;
    linePath(ctx, [{ x: p.x - hw, y: baseY - wall - vlen(4) },
                  { x: p.x, y: baseY - wall + ih - vlen(4) }], 1, steelLight);
    linePath(ctx, [{ x: p.x, y: baseY - wall + ih - vlen(4) },
                  { x: p.x + hw, y: baseY - wall - vlen(4) }], 1, steelLight);
    ctx.globalAlpha = 1;
    if (S.night) {
      ctx.globalAlpha = 0.35;
      fillOval(ctx, p.x + Math.round(iw * 0.2), baseY - wall - vlen(3), vlen(10), vlen(3), pal.window);
      ctx.globalAlpha = 1;
    }
  }

  /** 装车台：混凝土台 + 四柱顶棚 + 两根鹤管 + 计量牌，棚下停罐车。 */
  function drawLoadingRackModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var hw = Math.max(8, Math.round(m.hw * z));
    var hh = Math.max(4, Math.round(m.hh * z));
    var h = Math.max(10, Math.round(m.h * z));
    var cols = roleCols(pal, "steel", m.bg);
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var postW = Math.max(1, Math.round(2 * z));
    // 装车台面
    drawIsoBoxAt(ctx, p.x, baseY, hw, hh, Math.max(1, Math.round(2 * z)),
                 pal.stoneLight, pal.stone, pal.stoneDark);
    // 四根立柱（前后各两根，前柱画在顶棚之后）
    var legs = [[-0.84, 0.72, false], [0.84, 0.72, false], [-0.84, -0.78, true], [0.84, -0.78, true]];
    legs.forEach(function (lg) {
      if (lg[2]) return;
      var lx = p.x + Math.round(hw * lg[0]), ly = baseY + Math.round(hh * lg[1]);
      ctx.fillStyle = steelDark;
      ctx.fillRect(lx, ly - h, postW, h);
      ctx.fillStyle = steelLight;
      ctx.fillRect(lx, ly - h, postW, 1);
    });
    // 顶棚：菱形板 + 檐口 + 警示斜纹
    drawIsoBoxAt(ctx, p.x, baseY - h, hw, hh, Math.max(1, Math.round(2 * z)),
                 cols.top, cols.left, cols.right);
    linePath(ctx, [{ x: p.x - hw, y: baseY - h }, { x: p.x, y: baseY - h + hh },
                  { x: p.x + hw, y: baseY - h }], Math.max(1, Math.round(z)), steelLight);
    ctx.fillStyle = pal.warn;
    for (var stripe = 0; stripe < 3; stripe += 1) {
      var sx = p.x - Math.round(hw * 0.5) + stripe * Math.max(4, Math.round(hw * 0.32));
      ctx.fillRect(sx, baseY - h + Math.round(hh * 0.82), Math.max(2, Math.round(3 * z)), 1);
      ctx.fillStyle = pal.ink;
      ctx.fillRect(sx + Math.max(2, Math.round(2 * z)), baseY - h + Math.round(hh * 0.82), 1, 1);
      ctx.fillStyle = pal.warn;
    }
    // 前柱（压在顶棚上）
    legs.forEach(function (lg) {
      if (!lg[2]) return;
      var lx = p.x + Math.round(hw * lg[0]), ly = baseY + Math.round(hh * lg[1]);
      ctx.fillStyle = steelDark;
      ctx.fillRect(lx, ly - h, postW, h);
      ctx.fillStyle = steelLight;
      ctx.fillRect(lx, ly - h, postW, 1);
    });
    // 鹤管：两根从棚底垂到罐口高度，带阀门轮与软管弯头
    [-0.34, 0.30].forEach(function (off) {
      var ax = p.x + Math.round(hw * off);
      var armTop = baseY - h + vlen(2);
      var armBottom = baseY - Math.round(h * 0.42);
      linePath(ctx, [{ x: ax, y: armTop }, { x: ax, y: armBottom }], Math.max(1, Math.round(2 * z)), steelDark);
      linePath(ctx, [{ x: ax - 1, y: armTop }, { x: ax - 1, y: armBottom }], 1, steelLight);
      linePath(ctx, [{ x: ax, y: armBottom }, { x: ax + Math.round(hw * 0.14), y: armBottom }],
               Math.max(1, Math.round(2 * z)), steelDark);
      fillOval(ctx, ax, armBottom - vlen(4), vlen(2), Math.max(1, vlen(2)), pal.rust);
      ctx.fillStyle = pal.warn;
      ctx.fillRect(ax - 1, armBottom + 1, 2, 1);
    });
    // 计量牌：台侧的立牌
    var signX = p.x + Math.round(hw * 0.92), signY = baseY + Math.round(hh * 0.4);
    ctx.fillStyle = steelDark;
    ctx.fillRect(signX, signY - vlen(7), 1, vlen(7));
    ctx.fillStyle = pal.mark;
    ctx.fillRect(signX - vlen(3), signY - vlen(9), vlen(6), vlen(4));
    ctx.fillStyle = pal.paintDark;
    ctx.fillRect(signX - vlen(2), signY - vlen(8), vlen(4), 1);
    if (S.night) {
      ctx.globalAlpha = 0.34;
      fillOval(ctx, p.x, baseY - Math.round(h * 0.6), vlen(16), vlen(7), pal.window);
      ctx.globalAlpha = 1;
      ctx.fillStyle = pal.window;
      ctx.fillRect(p.x - Math.round(hw * 0.3), baseY - h + 1, 2, 1);
    }
  }

  /** 火炬塔：细高塔身 + 塔顶燃烧头 + 三只引火管，昼夜都有火苗与烟柱。 */
  function drawFlareModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(16, Math.round(m.h * z));
    var steel = m.bg ? pal.bgSteel : pal.steel;
    var steelDark = m.bg ? pal.bgSteelDark : pal.steelDark;
    var steelLight = m.bg ? pal.bgSteelLight : pal.steelLight;
    var metalDark = m.bg ? pal.bgSteelDark : pal.metalDark;
    // 基础墩 + 三个地脚
    drawIsoBoxAt(ctx, p.x, baseY, vlen(m.hw || 7), vlen(m.hh || 3.5), Math.max(1, Math.round(2 * z)),
                 pal.stoneLight, pal.stone, pal.stoneDark);
    // 塔身：三段收细（下粗上细），每段一条法兰
    var segs = [
      { w: 2.2, top: 0.42 }, { w: 1.9, top: 0.74 }, { w: 1.6, top: 1.0 },
    ];
    var prev = 0;
    segs.forEach(function (sg) {
      var w = Math.max(1, Math.round(sg.w * z));
      var from = baseY - Math.round(h * prev), to = baseY - Math.round(h * sg.top);
      ctx.fillStyle = steelDark;
      ctx.fillRect(p.x - w, to, w * 2, from - to);
      ctx.fillStyle = steelLight;
      ctx.fillRect(p.x - w, to, Math.max(1, w), from - to);
      ctx.fillStyle = steelDark;
      ctx.fillRect(p.x - w - 1, to, (w + 1) * 2, Math.max(1, Math.round(z)));
      prev = sg.top;
    });
    // 检修平台 + 爬梯
    var deck = baseY - Math.round(h * 0.62);
    linePath(ctx, [{ x: p.x - vlen(5), y: deck }, { x: p.x + vlen(5), y: deck }],
             Math.max(1, Math.round(1.4 * z)), steelLight);
    linePath(ctx, [{ x: p.x + vlen(3), y: baseY }, { x: p.x + vlen(3), y: baseY - Math.round(h * 0.95) }],
             1, metalDark);
    for (var rung = 3; rung < h * 0.9; rung += Math.max(3, Math.round(4 * z))) {
      linePath(ctx, [{ x: p.x + vlen(3), y: baseY - rung * z }, { x: p.x + vlen(5), y: baseY - rung * z }],
               1, steelLight);
    }
    // 燃烧头：略宽的短节 + 三只引火管
    var tipY = baseY - h;
    var tipW = Math.max(2, Math.round(2.6 * z));
    ctx.fillStyle = steelDark;
    ctx.fillRect(p.x - tipW, tipY - Math.max(2, Math.round(3 * z)), tipW * 2, Math.max(2, Math.round(3 * z)));
    ctx.fillStyle = steelLight;
    ctx.fillRect(p.x - tipW, tipY - Math.max(2, Math.round(3 * z)), tipW, 1);
    [-1, 0, 1].forEach(function (dir) {
      ctx.fillStyle = metalDark;
      ctx.fillRect(p.x + Math.round(dir * tipW * 1.2), tipY - Math.max(2, Math.round(3 * z)) - 1, 1, 2);
    });
    // 火苗：昼夜都烧，夜里更亮更大（外层火色 → 内层亮芯，逐帧轻抖）
    var flick = 1 + Math.sin((S.ticks || 0) * 0.11 + m.x * 0.05) * 0.16;
    var flameH = Math.max(4, Math.round((S.night ? 10 : 7) * z * flick));
    var flameW = Math.max(2, Math.round((S.night ? 4.2 : 3.2) * z * flick));
    var flameY = tipY - Math.max(2, Math.round(3 * z));
    ctx.globalAlpha = S.night ? 0.55 : 0.4;
    fillOval(ctx, p.x, flameY - Math.round(flameH * 0.4), Math.round(flameW * 1.5),
             Math.round(flameH * 0.8), pal.warn);
    ctx.globalAlpha = S.night ? 0.75 : 0.5;
    fillOval(ctx, p.x, flameY - Math.round(flameH * 0.35), flameW, Math.round(flameH * 0.6), pal.rustLight);
    ctx.globalAlpha = 1;
    fillOval(ctx, p.x, flameY - Math.round(flameH * 0.3), Math.max(1, Math.round(flameW * 0.5)),
             Math.max(1, Math.round(flameH * 0.34)), S.night ? pal.window : pal.mark);
    // 烟柱：从火苗往上飘，越远越淡（不用渐变，纯色叠 alpha）
    for (var s = 1; s <= 3; s += 1) {
      ctx.globalAlpha = 0.22 / s;
      fillOval(ctx, p.x + Math.round(s * 1.6 * z), flameY - Math.round(flameH + s * 7 * z),
               Math.max(1, Math.round((2 + s * 1.6) * z)), Math.max(1, Math.round((2 + s * 1.2) * z)),
               m.bg ? pal.bgSteel : pal.smoke);
    }
    ctx.globalAlpha = 1;
    // 塔脚围栏
    ctx.fillStyle = pal.warn;
    ctx.fillRect(p.x - vlen(6), baseY - vlen(3), vlen(12), 1);
  }

  function drawLampModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(8, Math.round((m.h || 24) * z));
    var arm = Math.max(3, Math.round(4 * z));
    fillOval(ctx, p.x, baseY + 1, Math.max(2, Math.round(3 * z)), 1, pal.shadow);
    ctx.fillStyle = pal.stoneDark;
    ctx.fillRect(p.x - 1, baseY - 1, 3, 2);
    // 灯杆 + 挑臂 + 灯头
    ctx.fillStyle = pal.metalDark;
    ctx.fillRect(p.x, baseY - h, 1, h);
    ctx.fillRect(p.x - arm, baseY - h, arm + 1, 1);
    ctx.fillStyle = S.night ? pal.window : pal.glassGlow;
    ctx.fillRect(p.x - arm, baseY - h + 1, 2, 1);
    ctx.fillStyle = pal.steelLight;
    ctx.fillRect(p.x - arm - 1, baseY - h + 1, 1, 1);
    if (S.night) {
      ctx.globalAlpha = 0.20;
      fillOval(ctx, p.x - arm, baseY - h + 1, Math.max(3, Math.round(4 * z)),
               Math.max(2, Math.round(3 * z)), pal.window);
      ctx.globalAlpha = 0.05;
      fillPoly(ctx, [
        { x: p.x - arm - 1, y: baseY - h + 2 },
        { x: p.x - arm + 2, y: baseY - h + 2 },
        { x: p.x + Math.round(5 * z), y: baseY + 1 },
        { x: p.x - Math.round(6 * z), y: baseY + 1 },
      ], pal.window);
      ctx.globalAlpha = 1;
    }
  }

  function drawReelModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var s = Math.max(3, Math.round(6 * z)) * (m.s || 1);
    var r = Math.max(2, Math.round(s * 0.55));
    fillOval(ctx, p.x, baseY + 1, Math.round(s * 1.1), Math.max(1, Math.round(s * 0.4)), pal.shadow);
    // 侧放线盘：外圈木法兰 + 内圈线缆
    fillOval(ctx, p.x, baseY - r, r, Math.round(r * 0.72), pal.crateDark);
    fillOval(ctx, p.x, baseY - r, Math.max(1, r - 2), Math.max(1, Math.round(r * 0.60)), pal.crate);
    for (var wr = 1; wr <= 3; wr += 1) {
      linePath(ctx, [{ x: p.x - Math.round(r * 0.72), y: baseY - r + wr },
                    { x: p.x + Math.round(r * 0.72), y: baseY - r + wr }], 1, pal.metalDark);
    }
    fillOval(ctx, p.x, baseY - r, Math.max(1, Math.round(r * 0.28)),
             Math.max(1, Math.round(r * 0.20)), pal.crateDark);
    ctx.fillStyle = pal.paint;
    ctx.fillRect(p.x - 1, baseY - r - 1, 2, 1);
  }

  function drawFlagModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(6, Math.round((m.h || 24) * z));
    fillOval(ctx, p.x, baseY + 1, Math.max(2, Math.round(2 * z)), 1, pal.shadow);
    ctx.fillStyle = pal.stoneDark;
    ctx.fillRect(p.x - 1, baseY - 1, 3, 2);
    ctx.fillStyle = pal.metalLight;
    ctx.fillRect(p.x, baseY - h, 1, h);
    var wave = Math.round(Math.sin(S.ticks * 0.35 + m.x) * Math.max(1, 2 * z));
    var fw = Math.max(4, Math.round(9 * z));
    var fh = Math.max(2, Math.round(3 * z));
    ctx.fillStyle = pal.paint;
    ctx.fillRect(p.x + 1, baseY - h + 1 + wave, fw, fh);
    ctx.fillStyle = pal.mark;
    ctx.fillRect(p.x + 1, baseY - h + 1 + wave, fw, 1);
  }

  function drawFigureModel(ctx, pal, m) {
    var z = S.cam.zoom;
    var p = viewPoint(m.x, m.y);
    var baseY = p.y - Math.round((m.z0 || 0) * z);
    var h = Math.max(2, Math.round(4 * z));
    ctx.fillStyle = pal.ink;
    ctx.fillRect(p.x, baseY - h, 1, h);
    ctx.fillRect(p.x, baseY - h - 1, 1, 1);
    if (S.night) {
      ctx.fillStyle = pal.warn;
      ctx.fillRect(p.x, baseY - h + 1, 1, 1);
    }
  }

  function drawEffects(ctx, pal) {
    var site = S.world && S.world.site;
    if (!site) return;
    var z = S.cam.zoom;
    var W = S.bufW, H = S.bufH;
    // 烟囱 / 筒仓的烟：浓度与颜色由这座厂区「今天在不在开工」决定。
    // 以前无论有没有活都在冒一样的白烟——烟于是变成了纯装饰。现在它是读数：
    // 在开工 → 浓、密、往上冲；收工但今天有产出 → 细一点；连续返工 → 转黑、压得低。
    site.smoke.forEach(function (s) {
      var p = viewPoint(s.x, s.y);
      var top = p.y - s.h * z;
      var st = s.projectId != null ? statFor(s.projectId) : null;
      var puffs = 5;
      var thick = 1;
      var tone = s.steam ? pal.steelLight : pal.smoke;
      if (s.steam) {
        thick = 0.9;
      } else if (st) {
        if (st.busy > 0) { puffs = 7; thick = 1.25; }
        else if (st.rework >= 3) { puffs = 6; thick = 1.0; tone = pal.ink; }
        else if (st.done > 0) { thick = 0.8; }
        else { thick = 0.62; }
      }
      for (var i = 0; i < puffs; i += 1) {
        var t = ((S.ticks * 0.055 + i * (1 / puffs)) % 1);
        var px = p.x + Math.sin((S.ticks * 0.03 + i) * 2) * 3 - t * 9;
        var py = top - t * Math.max(10, 26 * z);
        var r = Math.max(2, (2 + t * 6) * z);
        if (s.steam) {
          px = p.x + Math.sin(i * 1.7 + t * 4) * (3 + t * 5) * z - t * 10 * z;
          py = top - t * 37 * z;
          r = Math.max(2, (4 + t * 10) * z);
        }
        ctx.globalAlpha = (1 - t) * (s.steam ? 0.27 : 0.32) * thick;
        fillOval(ctx, px, py, r, r * 0.72, tone);
      }
      ctx.globalAlpha = 1;
    });
    // 处理机组的火星
    site.sparks.forEach(function (s) {
      var p = viewPoint(s.x, s.y);
      var top = p.y - s.h * z;
      for (var i = 0; i < 6; i += 1) {
        var t = ((S.ticks * 0.11 + i * 0.15) % 1);
        var px = p.x + Math.sin(i * 2.1 + S.ticks * 0.05) * (4 + t * 10);
        var py = top + t * t * 16 * z - t * 5;
        ctx.fillStyle = i % 2 ? pal.sun : pal.rustLight;
        ctx.fillRect(Math.round(px), Math.round(py), 1, 1);
      }
    });
    // 空气里的粉尘
    for (var i = 0; i < 60; i += 1) {
      var seed = i * 97 + 13;
      var dx = ((seed * 37) % Math.max(1, W) + S.ticks * 0.12 * (1 + i % 3)) % Math.max(1, W);
      var dy = Math.round(H * 0.22) + ((seed * 53) % Math.max(1, Math.round(H * 0.7)));
      ctx.globalAlpha = 0.10 + (i % 5) * 0.035;
      ctx.fillStyle = pal.dust;
      ctx.fillRect(Math.round(dx), Math.round(dy + Math.sin(S.ticks * 0.02 + i) * 2), 1, 1);
    }
    ctx.globalAlpha = 1;
  }

  function drawLighting(ctx, pal) {
    var W = S.bufW, H = S.bufH;
    var g = ctx.createRadialGradient(W / 2, H / 2, Math.min(W, H) * 0.34,
                                     W / 2, H / 2, Math.max(W, H) * 0.72);
    g.addColorStop(0, "rgba(0,0,0,0)");
    g.addColorStop(1, pal.shadow);
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);
  }

  // ---------- 现实天气 ----------
  /** 天气层：整体压暗 / 雾 / 雨 / 雪 / 雷闪。粒子只画在 Canvas 内，
      文字标签仍是 DOM 覆盖层；reduced-motion 下只保留静态色调。 */
  function drawWeather(ctx, pal) {
    var kind = weatherKind();
    if (kind === "clear" || kind === "unknown") return;
    var W = S.bufW, H = S.bufH;
    if (kind !== "fog") {
      ctx.globalAlpha = kind === "thunder" ? 0.20 : (kind === "snow" ? 0.07 : 0.13);
      ctx.fillStyle = S.night ? "rgba(8, 16, 24, 1)" : "rgba(56, 64, 74, 1)";
      ctx.fillRect(0, 0, W, H);
      ctx.globalAlpha = 1;
    }
    if (kind === "fog") {
      var fog = ctx.createLinearGradient(0, H * 0.22, 0, H);
      fog.addColorStop(0, "rgba(200, 205, 205, 0)");
      fog.addColorStop(0.55, S.night ? "rgba(90, 105, 120, 0.20)"
        : "rgba(220, 215, 205, 0.22)");
      fog.addColorStop(1, S.night ? "rgba(70, 85, 100, 0.34)"
        : "rgba(235, 228, 215, 0.34)");
      ctx.fillStyle = fog;
      ctx.fillRect(0, H * 0.22, W, H * 0.78);
    }
    if (kind === "rain" || kind === "drizzle" || kind === "thunder") {
      drawRain(ctx, pal, kind);
    }
    if (kind === "snow") drawSnow(ctx, pal);
    if (kind === "thunder") drawLightning(ctx, pal);
  }

  function drawRain(ctx, pal, kind) {
    if (S.reduced) return;
    var W = S.bufW, H = S.bufH;
    var drizzle = kind === "drizzle";
    var count = drizzle ? 70 : 132;
    ctx.fillStyle = S.night ? pal.glassLight : pal.glassGlow;
    for (var i = 0; i < count; i += 1) {
      var seed = i * 97 + 13;
      var x = (seed * 7 + S.ticks * (drizzle ? 1.1 : 2.4)) % W;
      var y = (seed * 31 + S.ticks * (drizzle ? 3 : 7) + (i % 5) * 17) % H;
      ctx.globalAlpha = 0.14 + (i % 4) * 0.055;
      ctx.fillRect(Math.round(x), Math.round(y), 1, drizzle ? 2 : 4);
      if (!drizzle) {
        ctx.fillRect(Math.round(x) + 1, Math.round(y) + 4, 1, 1);
      }
    }
    ctx.globalAlpha = 1;
  }

  function drawSnow(ctx, pal) {
    if (S.reduced) return;
    var W = S.bufW, H = S.bufH;
    ctx.fillStyle = S.night ? pal.glassLight : "#f4f7fb";
    for (var i = 0; i < 84; i += 1) {
      var drift = Math.sin(S.ticks * 0.045 + i * 0.7) * 9;
      var x = (i * 113 + drift + 23) % W;
      var y = (i * 47 + S.ticks * 1.7) % H;
      ctx.globalAlpha = 0.30 + (i % 3) * 0.16;
      var size = (i % 2) + 1;
      ctx.fillRect(Math.round(x), Math.round(y), size, size);
    }
    ctx.globalAlpha = 1;
  }

  function drawLightning(ctx, pal) {
    var phase = S.ticks % 120;
    if (phase >= 2) return;
    var W = S.bufW, H = S.bufH;
    var skyH = sceneHorizon();
    ctx.globalAlpha = phase === 0 ? 0.22 : 0.10;
    ctx.fillStyle = "#eaf4ff";
    ctx.fillRect(0, 0, W, H);
    ctx.globalAlpha = 0.65;
    ctx.strokeStyle = pal.glassLight;
    ctx.lineWidth = 1;
    var bx = Math.round(W * (0.52 + (S.ticks % 7) * 0.045));
    var by = 0;
    ctx.beginPath();
    ctx.moveTo(bx, by);
    for (var i = 1; i <= 6; i += 1) {
      bx += ((i * 37) % 19) - 9;
      by = Math.round(skyH * (i / 6) + ((i * 13) % 7));
      ctx.lineTo(bx, by);
    }
    ctx.stroke();
    ctx.globalAlpha = 1;
  }

  // ---------- FX 导出（主文件转发目标 + 测试探针入口） ----------
  FX.render = render; FX.viewPoint = viewPoint; FX.vlen = vlen;
  FX.fillDiamond = fillDiamond; FX.fillOval = fillOval; FX.linePath = linePath;
  FX.drawIsoBoxAt = drawIsoBoxAt;   // 待发月台包裹与成品垛货箱共用一套立体建模
  FX.seeded = seeded; FX.isToday = isToday; FX.roleCols = roleCols;
  FX.drawModel = drawModel; FX.drawModels = drawModels;
  FX.drawTruckModel = drawTruckModel; FX.drawForkliftModel = drawForkliftModel;
  FX.drawWorkerPose = drawWorkerPose; FX.drawHoverRing = drawHoverRing;
  FX.collectDynamicItems = collectDynamicItems; FX.sceneDrawItems = sceneDrawItems; FX.drawLayer = drawLayer;
  FX.sceneHorizon = sceneHorizon; FX.sawRoofFaces = sawRoofFaces;
  FX.drawCoolingTowerModel = drawCoolingTowerModel; FX.drawPipeRackModel = drawPipeRackModel;
  FX.drawTransferBridgeModel = drawTransferBridgeModel; FX.drawRackSupports = drawRackSupports;
  FX.drawCoalShedModel = drawCoalShedModel; FX.drawSiloBatteryModel = drawSiloBatteryModel;
  FX.drawGasholderModel = drawGasholderModel; FX.drawBasinModel = drawBasinModel;
  FX.drawLoadingRackModel = drawLoadingRackModel; FX.drawFlareModel = drawFlareModel;
  FX.daylightFactor = daylightFactor;   // 昼光系数（panel 探针行为断言用）
})();