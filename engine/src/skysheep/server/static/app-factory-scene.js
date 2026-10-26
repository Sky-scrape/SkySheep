/* ============================================================
 * 工厂场景构建层：世界布局与厂区 / 厂外建筑的几何建模（自 app-factory.js 拆出，零行为变化）。
 *
 * 加载顺序：app-factory.js -> 本文件 -> app-factory-drawer.js -> app-factory-draw.js。
 * 主文件的状态与工具经 window.FX 顶部绑定（活引用 / 加载时已就位的函数）；
 * 绘制原语 seeded 在绘制层（后加载），调用点经 FX 运行时查表。
 * ============================================================ */
(function () {
  "use strict";

  var FX = window.FX;
  var S = FX.S;
  var TILE_W = FX.TILE_W, TILE_H = FX.TILE_H;
  var clamp = FX.clamp, hashKey = FX.hashKey, machineCap = FX.machineCap;
  var TRIP_PHASES = FX.TRIP_PHASES;   // 出车五段时长（主文件常量，见 app-factory.js）

  // ---------- 动态对象常量（位姿插值用；位姿函数在本文件尾部，均为纯函数） ----------
  var PATROL_SPEED = 7;          // 工头巡检速度：世界单位/秒
  var PATROL_PAUSE_MS = 1700;    // 每个巡检路点的停留时长
  var WAVE_MS = FX.WAVE_MS || 900;   // 挥手时长（常量定义在主文件，主文件写 waveUntil）
  var TRIP_LOAD_MS = (TRIP_PHASES[0] || { ms: 0 }).ms + (TRIP_PHASES[1] || { ms: 0 }).ms
    + (TRIP_PHASES[2] || { ms: 0 }).ms;   // 装货相的起始时刻（前三相之和）
  var TRIP_LOAD_DUR = (TRIP_PHASES[3] || { ms: 0 }).ms;

  // ---------- 厂区布局参数（buildWorld 的几何系数，集中一处便于调版） ----------
  var PLOT_MIN_HALF_W = 92;       // 菱形坪半宽下限：装下后场厂房 + 附房 + 筒仓 + 前场两排机台
                                 // （104 → 92：坪收一圈，同样画幅下机台与包裹相对更大）
  var PLOT_PER_LINE = 9.5;        // 每多一条产线，坪半宽追加的量
  var ENTRANCE_EDGE = 0.96;       // 入口收在坪沿内侧的比例（主轨道保持直线进厂）
  var HUB_DX = 0.28, HUB_DY = 0.06;    // 前场枢纽（包裹分流点）相对厂心的偏移
  var LINE_GAP_MAX = 34;          // 机台横向间距上限
  var LINE_GAP_RATIO = 0.32;      // 机台间距随坪宽的比例
  var LINE_ROW_Y = 0.10;          // 第一排机台的纵向位置（自厂心向下）
  var LINE_ROW_DY = 0.23;         // 第二排的纵向行距（南侧要留出装卸纵列的纵深）

  // ---------- 装卸几何（layoutFactory 的系数：世界坐标 = 屏幕单位 @zoom1） ----------
  // 硬约束：一切装卸设施与三条动线都收在本厂菱形坪内（|dx|/(hw*YARD_K)+|dy|/(hh*YARD_K) ≤ 1，
  // 四角都判、竖直高度不计），绝不伸进相邻槽位。空间不足就缩动态模型 / 减装载表现 /
  // 缩短靠站距离——不外溢、也不缩判定核伪装。
  // 占地先于动线：机台包络算出安全带，安全带上排装货纵列（货车停车位 + 取货托盘 + 叉车）。
  var YARD_Z = 5;                 // 厂坪顶面高度（铺装 h=5）：车 / 叉车 / 工头都坐在它上面
  var YARD_K = 0.92;              // 落点用的菱形内缩比例（比铺装的 0.96 再收一圈留余量）
  var MACHINE_HW = 11.5;          // 机台半宽（与 buildIndustrialSite 的机台模型一致）
  var MACHINE_HH = 4.5;           // 机台半深（23×9 底座：南向要留出装卸纵列）
  var TRUCK_HALF_W = 4.5;         // 车斗半宽（drawTruckModel W=9 的一半）
  var TRUCK_HALF_L = 13;          // 车斗半长（drawTruckModel L=26 的一半）
  var FORK_HALF = 2.5;            // 叉车碰撞核半尺寸
  var TRAY_HALF = 1.5;            // 取货托盘板厚半高（drawPalletModel h=3 的一半）
  var PALLET_HALF = 7.5;          // 成品托盘半宽（drawPalletModel w=15 的一半）
  var FORK_REACH = 2.4;           // 叉臂伸出车体外的地面长度（绘制层用同一值）
  var FORK_LIFT = 3;              // lift=1 时货箱抬起的竖直高度（z，不是地面位移）
  var DROP_GAP = 6.4;             // 叉车放货 / 停靠时车体到车道线的地面距离
  var TRAY_GAP = 10.5;            // 取货托盘中心到车道线的地面距离
  var DOCK_DX = 0.06;             // 装货纵列相对厂心的横向系数（贴着厂心，才收得进菱形）
  var BAY_DX = 0.09;              // 坪内靠站位相对厂心的横向系数（货车短距往返的东端）
  var GATE_DX = 0.12;             // 门岗相对厂心的横向位置（车道南侧，不跨车道）
  var GATE_DY = 10;               // 门岗在车道线以南的距离（柱子让开车斗占地）
  var WORKER_R = 4;               // 工头碰撞半径
  var LANE_CLEAR = 5;             // 车道中心到机台包络南缘的净距（车半宽 4.5 + 余量 0.5）
  var PATROL_MARGIN = 6;          // 巡检带北腿到机台包络南缘的净距
  var PATROL_SPAN = 10;           // 巡检带纵深上限（深了菱形就窄，巡检反而没得走）
  var BLOCK_PAD = 16;             // 装货纵列对巡检带的封锁半宽（车半长 13 + 余量）

  // ---------- 世界 ----------
  // 世界按 3×3 网格排布：调度厅占中心格，项目厂区占外层八格（不画格线）。
  // 超过 8 个项目时继续向外扩 5×5、7×7 的外圈，间距不变。
  var GRID_X = 224;
  var GRID_Y = 120;

  function gridSlots(n) {
    // 固定槽位顺序，不随项目数量重排；坐标投影与平台菱形的两条轴一致。
    var order = [[-1, -1], [1, -1], [-1, 1], [1, 1],
                 [0, -1], [-1, 0], [1, 0], [0, 1]];
    var out = order.slice(0, Math.max(0, n));
    var ring = 2;
    while (out.length < n) {
      var cells = [];
      for (var gx = -ring; gx <= ring; gx += 1) {
        for (var gy = -ring; gy <= ring; gy += 1) {
          if (Math.max(Math.abs(gx), Math.abs(gy)) !== ring) continue;
          cells.push([gx, gy]);
        }
      }
      // 从正北开始顺时针排，保证外圈顺序稳定
      cells.sort(function (a, b) {
        return Math.atan2(a[0], -a[1]) - Math.atan2(b[0], -b[1]);
      });
      for (var ci = 0; ci < cells.length && out.length < n; ci += 1) out.push(cells[ci]);
      ring += 1;
    }
    return out.map(function (g) { return { gx: g[0], gy: g[1] }; });
  }

  function plotSpec(n) {
    if (n <= 2) return { w: 4, h: 3, cols: 2, rows: 1 };
    if (n <= 5) return { w: 5, h: 4, cols: 3, rows: 2 };
    return { w: 6, h: 5, cols: 3, rows: 2 };
  }

  function toTile(x, y) {
    return { x: x / TILE_W + y / TILE_H, y: y / TILE_H - x / TILE_W };
  }
  function iso(x, y) {
    return { x: (x - y) * (TILE_W / 2), y: (x + y) * (TILE_H / 2) };
  }

  // 台地尺寸按 3×3 网格外圈算：直接取所有厂区坪的极限尖角，
  // 加一格余量，让台地恰好包住外圈厂区——中间不空一大片。
  function makePlateau(factories) {
    // 保留完整九宫格，即使部分外圈格暂时没有项目也不缩掉。
    var ring = Math.max(1, Math.ceil((Math.sqrt(factories.length + 1) - 1) / 2));
    var hw = GRID_X * (ring + 0.5);
    var hh = GRID_Y * (ring + 0.5);
    var required = 1;
    factories.forEach(function (f) {
      [[f.x - f.halfW, f.y], [f.x + f.halfW, f.y],
       [f.x, f.y - f.halfH], [f.x, f.y + f.halfH]].forEach(function (p) {
        required = Math.max(required, Math.abs(p[0]) / hw + Math.abs(p[1]) / hh);
      });
    });
    hw = Math.ceil(hw * required * 1.04);
    hh = Math.ceil(hh * required * 1.04);
    return {
      cx: 0, cy: 0,
      hw: hw, hh: hh,
      th: 0,
    };
  }

  function buildWorld(st) {
    var projects = (st && st.projects) || [];
    var hall = { x: 0, y: 0 };
    var factories = [];
    var lines = new Map();
    var keepSelected = S.selectedKey;
    // 3×3 网格：调度厅居中，项目按外层格排布（格线不画出来）。
    // 先占外围四角，再补边格；新增项目不改变已占槽位。
    var slots = gridSlots(projects.length);
    var i = 0;
    projects.forEach(function (p) {
      var slot = slots[i] || { gx: 0, gy: -1 };
      var fx = (slot.gx - slot.gy) * GRID_X / 2;
      var fy = (slot.gx + slot.gy) * GRID_Y / 2;
      var spec = plotSpec((p.lines || []).length);
      var f = {
        project: p, id: p.id, name: p.name, root_ok: p.root_ok !== false,
        x: fx, y: fy, w: spec.w, h: spec.h, spec: spec, lines: [],
        jumpStart: 0,
      };
      var n = spec.w + spec.h;
      // 菱形坪要装下后场厂房 + 附房 + 筒仓 + 前场两排机台，半宽按 104 起算；
      // 建筑与机台都收在「菱形内切矩形」附近，四角不再压出坪沿。
      f.halfW = Math.max(PLOT_MIN_HALF_W, PLOT_PER_LINE * n);
      f.halfH = f.halfW * 0.5;
      // 入口位于调度厅与厂区中心连线的坪沿，主轨道保持一条直线。
      var edgeScale = ENTRANCE_EDGE / (Math.abs(fx) / f.halfW + Math.abs(fy) / f.halfH || 1);
      f.entrance = { x: fx * (1 - edgeScale), y: fy * (1 - edgeScale) };
      // 入口正对厂心（调度厅在正南 / 正北那一列）时，进厂分配带会斜穿中心的装货纵列：
      // 把入口沿坪沿横移三分之一坪宽，让它贴边进厂，中心那条 x 留给装卸。
      if (Math.abs(f.entrance.x - fx) < f.halfW * 0.18) {
        var ex = fx + (fx + f.halfW * HUB_DX >= fx ? 1 : -1) * f.halfW * 0.34;
        var ey = fy + (f.entrance.y >= fy ? 1 : -1) * f.halfH * ENTRANCE_EDGE
          * Math.max(0.15, 1 - Math.abs(ex - fx) / (f.halfW * ENTRANCE_EDGE));
        f.entrance = { x: ex, y: ey };
      }
      f.hub = { x: fx + f.halfW * HUB_DX, y: fy + f.halfH * HUB_DY };
      // 装卸几何（车道 / 装货位 / 巡检环）在机台位姿算完后由 layoutFactory 按真实占地生成。
      f.machines = [];

      f.belt = routeBelt(hall, f.entrance, i);
      // 机台在前场排两排，横向按场院宽度铺开；支线停在机台口，不横穿工作区。
      var gapX = Math.min(LINE_GAP_MAX, f.halfW * LINE_GAP_RATIO);
      var rowY = fy + f.halfH * LINE_ROW_Y;
      // 单厂最多画 machineCap 台机台；其余只在面板 / 抽屉里列出（名牌标 +N）
      var cap = machineCap();
      (p.lines || []).forEach(function (line, li) {
        if (line.kind === "cron" && line.status === "stopped") { /* 仍画机台：挂停工牌 */ }
        var col = li % spec.cols;
        var row = Math.floor(li / spec.cols);
        var mx = fx + (col - (spec.cols - 1) / 2) * gapX;
        var my = rowY + row * f.halfH * LINE_ROW_DY;
        var rec = {
          key: line.key, line: line, kind: line.kind,
          id: line.id, project: p, factory: f,
          x: mx, y: my, index: li, drawn: li < cap,
        };
        rec.path = f.belt.concat([f.hub, { x: mx, y: my - 9 }]);
        rec.transit = transitSeconds(rec.path);
        f.machines.push({ x: mx, y: my });
        if (li < cap) f.lines.push(rec);
        lines.set(rec.key, rec);
      });
      layoutFactory(f);
      factories.push(f);
      i += 1;
    });

    var plateau = makePlateau(factories);
    var site = buildIndustrialSite(hall, factories, plateau);

    var minX = hall.x - 70, maxX = hall.x + 70;
    var minY = hall.y - 50, maxY = hall.y + 50;
    factories.forEach(function (f) {
      minX = Math.min(minX, f.x - f.halfW - 20);
      maxX = Math.max(maxX, f.x + f.halfW + 20);
      minY = Math.min(minY, f.y - f.halfH - 26);
      maxY = Math.max(maxY, f.y + f.halfH + 26);
    });

    site.models.forEach(function (m) {
      if (m.bg && !m.peripheral) return;
      var points = m.path || [{ x: m.x, y: m.y }];
      var hw = m.hw || m.r || m.width || 8;
      var hh = m.hh || (m.r ? m.r * 0.5 : 5);
      // 轴系零件（煤棚 / 筒仓群）的占地落在「沿公路 / 纵深」两条轴上，投影回屏幕
      // 两轴再取包围盒；轴对齐零件照旧（ax=1, ay=0 时两个 span 就是 hw / hh）。
      var ax = m.axis ? m.axis.x : 1, ay = m.axis ? m.axis.y : 0;
      var spanX = Math.abs(hw * ax) + Math.abs(hh * ay);
      var spanY = Math.abs(hw * ay) + Math.abs(hh * ax);
      // 取景包含塔顶，避免缩放和不同窗口尺寸下裁掉主体。
      var height = (m.z0 || 0) + (m.h || Math.max(m.h0 || 0, m.h1 || 0));
      points.forEach(function (pt) {
        minX = Math.min(minX, pt.x - spanX - 8);
        maxX = Math.max(maxX, pt.x + spanX + 8);
        minY = Math.min(minY, pt.y - height - spanY - 16);
        maxY = Math.max(maxY, pt.y + spanY + 12);
      });
    });

    // 固定取景同时包含园区边缘与外围设施，避免居中后裁掉前沿道路。
    minX = Math.min(minX, plateau.cx - plateau.hw - 20);
    maxX = Math.max(maxX, plateau.cx + plateau.hw + 20);
    minY = Math.min(minY, plateau.cy - plateau.hh);
    maxY = Math.max(maxY, plateau.cy + plateau.hh + 20);

    // 取景框（focus）：只框住「调度厅 + 所有厂区坪」，不含外围台地与远景设施。
    // 场景禁缩放之后，默认取景就是唯一画幅：3 个项目时若按 bounds（含台地前缘
    // 与外圈设施）取景，厂区只占中间一小块，机台又回到「看不清」。
    // 这样一来项目越少、镜头越近，机台与成品垛自然就大。
    var focus = null;
    factories.forEach(function (f) {
      if (!focus) {
        focus = { minX: f.x - f.halfW, maxX: f.x + f.halfW,
                  minY: f.y - f.halfH, maxY: f.y + f.halfH };
        return;
      }
      focus.minX = Math.min(focus.minX, f.x - f.halfW);
      focus.maxX = Math.max(focus.maxX, f.x + f.halfW);
      focus.minY = Math.min(focus.minY, f.y - f.halfH);
      focus.maxY = Math.max(focus.maxY, f.y + f.halfH);
    });
    // 调度厅不进取景框：厂区围成一圈时它本来就在中间，一两个项目时把它算进来
    // 会得到一个「厂房在上、调度厅占半屏」的竖长构图。厅只做背景衔接，不抢画幅。
    // 一个项目都没有时没有「厂区坪」可框：退回整体取景，让调度厅与外围设施
    // 完整入画（空场景不该被放大到只剩一栋楼）。
    if (!focus) focus = { minX: minX, maxX: maxX, minY: minY, maxY: maxY };
    var focusPad = 44;
    focus.minX -= focusPad; focus.maxX += focusPad;
    focus.minY -= focusPad; focus.maxY += focusPad;

    // 世界一换，动态对象全部撤场：旧 trip 持有旧厂区的动线几何（pose 会画在
    // 旧位置上），hover 的命中描述也可能指向已不存在的键——重建即清。
    S.trips = [];
    S.hover = null;
    S.hoverPointer = null;
    S.world = {
      hall: hall, factories: factories, lines: lines,
      models: site.models, site: site, plateau: plateau,
      bounds: { minX: minX, maxX: maxX, minY: minY, maxY: maxY },
      focus: focus,
    };
    S.worldSig = projects.map(function (p) {
      return p.id + ":" + (p.lines || []).map(function (l) { return l.key; }).join(",");
    }).join("|");
    // 重建世界时不丢选中态：cron_updated / 运行事件会触发防抖重拉，若每次都把
    // 高亮清掉，面板会一直在用户眼皮底下「闪一下」
    S.selectedKey = lines.has(keepSelected) ? keepSelected : null;
  }

  function buildIndustrialSite(hall, factories, plateau) {
    // 台地由 buildWorld 算好传进来；单测 / 探针会只传前两个参数，
    // 而贴路布置（料场 / 罐区 / 前场支路）依赖台地尺寸，不能依赖调用方传参。
    if (!plateau) plateau = makePlateau(factories);
    var rand = FX.seeded(0x1D05721A);
    var models = [];
    var patches = [];
    var smoke = [];
    var sparks = [];
    var roads = [];
    var extentX = 300, extentY = 150;

    for (var b = 0; b < 64; b += 1) {
      var bx = 0, by = 0;
      for (var attempt = 0; attempt < 8; attempt += 1) {
        bx = (rand() * 2 - 1) * extentX;
        by = (rand() * 2 - 1) * extentY;
        if (!plateau) break;
        if (Math.abs(bx - plateau.cx) / plateau.hw + Math.abs(by - plateau.cy) / plateau.hh <= 0.96) break;
      }
      var br = 8 + rand() * 26;
      patches.push({ x: bx, y: by, rx: br, ry: br * (0.32 + rand() * 0.2), light: rand() < 0.5 });
    }

    function addBox(x, y, hw, hh, h, role, opts) {
      var m = { kind: "box", x: x, y: y, hw: hw, hh: hh, h: h, role: role, z0: 0 };
      if (opts) for (var k in opts) if (Object.prototype.hasOwnProperty.call(opts, k)) m[k] = opts[k];
      models.push(m);
      return m;
    }
    function addCyl(x, y, r, h, role, opts) {
      var m = { kind: "cyl", x: x, y: y, r: r, h: h, role: role, z0: 0 };
      if (opts) for (var k in opts) if (Object.prototype.hasOwnProperty.call(opts, k)) m[k] = opts[k];
      models.push(m);
      return m;
    }

    factories.forEach(function (f) {
      var cx = f.x, cy = f.y, hw = f.halfW, hh = f.halfH;
      var baseZ = 5;
      // 单测 / 探针会直接传自建厂区（没走过 buildWorld）：先补一次初值布局，
      // 免得下面读 f.tray / f.palletSpot / f.gate 拿到 undefined。
      if (!f.tray || !f.palletSpot || !f.gate) layoutFactory(f);
      var variant = Math.abs(hashKey("site" + f.id)) % 8;
      f.buildingVariant = variant;
      addBox(cx, cy, hw * 0.96, hh * 0.96, 5, "concrete",
             { paving: true, rail: true, curb: true, yard: f });

      var compoundStart = models.length;
      var styles = ["重工铸造厂", "物流集散仓", "垂直加工塔", "联排制造坊",
                    "数据机房", "退台研发园", "双跨装配厂", "罐区精炼站"];
      f.compoundStyle = styles[variant];
      var layouts = [
        [-0.20, -0.28, 0.25, 0.18], [-0.10, -0.37, 0.38, 0.16],
        [-0.10, -0.40, 0.14, 0.14], [-0.32, -0.20, 0.16, 0.18],
        [-0.22, -0.25, 0.24, 0.22], [-0.18, -0.32, 0.23, 0.18],
        [-0.27, -0.28, 0.19, 0.24], [-0.37, -0.12, 0.13, 0.16]
      ];
      var layout = layouts[variant];
      var mainBx = cx + hw * layout[0], mainBy = cy + hh * layout[1];
      var mainHW = hw * layout[2], mainHH = hh * layout[3];
      var heights = [38, 24, 56, 34, 27, 32, 22, 29];
      var mainH = heights[variant];
      var topZ = baseZ + mainH;
      addBox(mainBx, mainBy, mainHW, mainHH, mainH, "steel",
             { z0: baseZ, windows: variant === 4 ? 1 : 4, door: true,
               roof: variant === 0 || variant === 3 || variant === 6 ? "saw" : "flat",
               vent: variant === 2, hall: true, pipes: variant === 0 || variant === 7 });

      function wing(x, y, w, d, h, role, opts) {
        var options = { z0: baseZ, roof: "flat", windows: 2, annex: true };
        if (opts) Object.keys(opts).forEach(function (key) { options[key] = opts[key]; });
        return addBox(cx + hw * x, cy + hh * y, hw * w, hh * d, h, role, options);
      }
      function tank(x, y, r, h, role, opts) {
        var options = { z0: baseZ, bands: 3, ladder: true, silo: true };
        if (opts) Object.keys(opts).forEach(function (key) { options[key] = opts[key]; });
        return addCyl(cx + hw * x, cy + hh * y, hw * r, h, role, options);
      }
      function stack(x, y, h) {
        var chimneyX = cx + hw * x, chimneyY = cy + hh * y;
        addCyl(chimneyX, chimneyY, 2.3, h, "steel",
               { z0: baseZ, bands: 2, chimney: true, cap: true });
        smoke.push({ x: chimneyX, y: chimneyY, h: baseZ + h, projectId: f.id });
      }
      if (variant === 0) {
        models.push({ kind: "furnace", x: cx - hw * 0.56, y: cy - hh * 0.10,
                      hw: 10, hh: 6, h: 34, z0: baseZ, role: "rust", projectId: f.id });
        wing(0.25, -0.27, 0.14, 0.13, 16, "rust", { roof: "saw", pipes: true });
        tank(0.49, -0.15, 0.065, 27, "rust");
        stack(-0.27, -0.55, 53);
        stack(-0.07, -0.55, 46);
        wing(-0.20, 0.00, 0.19, 0.055, 7, "steel", { canopy: true, windows: 0 });
      } else if (variant === 1) {
        wing(0.34, -0.06, 0.12, 0.15, 11, "rust", { door: true });
        wing(-0.10, -0.13, 0.30, 0.045, 8, "steel", { canopy: true, windows: 0 });
        for (var container = 0; container < 3; container += 1) {
          wing(-0.48 + container * 0.22, 0.14, 0.085, 0.075, 6,
               container === 1 ? "steel" : "rust", { container: true, windows: 0 });
        }
        wing(-0.48, 0.14, 0.075, 0.065, 5, "steel",
             { z0: baseZ + 6, container: true, windows: 0 });
        models.push({ kind: "truck", x: cx + hw * 0.40, y: cy + hh * 0.25, z0: baseZ });
      } else if (variant === 2) {
        wing(-0.10, -0.40, 0.09, 0.09, 8, "glass", { z0: topZ, windows: 3 });
        topZ += 8;
        wing(-0.36, -0.08, 0.19, 0.12, 14, "steel", { vent: true });
        wing(0.25, -0.18, 0.13, 0.13, 21, "rust", { pipes: true });
        models.push({ kind: "transferBridge",
                      path: [{ x: cx - hw * 0.03, y: cy - hh * 0.29 },
                             { x: cx + hw * 0.20, y: cy - hh * 0.18 }],
                      h0: 20, h1: 20, width: 3 });
      } else if (variant === 3) {
        wing(0.04, -0.40, 0.15, 0.16, 26, "rust", { roof: "saw", door: true });
        wing(0.34, -0.20, 0.13, 0.17, 18, "steel", { roof: "saw", door: true });
        wing(-0.06, -0.10, 0.07, 0.07, 10, "glass");
        wing(-0.50, 0.14, 0.075, 0.06, 7, "rust", { container: true, windows: 0 });
      } else if (variant === 4) {
        wing(0.31, -0.28, 0.16, 0.18, 23, "steel", { windows: 1 });
        for (var ac = 0; ac < 3; ac += 1) {
          addBox(mainBx - mainHW * 0.60 + ac * mainHW * 0.60, mainBy,
                 mainHW * 0.20, mainHH * 0.24, 6, "steel",
                 { z0: topZ, roof: "flat", vent: true });
        }
        wing(0.01, 0.02, 0.12, 0.07, 8, "glass", { windows: 4 });
        for (var cooler = 0; cooler < 3; cooler += 1) {
          wing(-0.51 + cooler * 0.14, 0.15, 0.045, 0.06, 8, "steel", { vent: true, windows: 0 });
        }
        topZ += 6;
      } else if (variant === 5) {
        addBox(mainBx, mainBy, mainHW * 0.72, mainHH * 0.72, 12, "glass",
               { z0: topZ, windows: 5, roof: "flat" });
        topZ += 12;
        wing(0.26, -0.18, 0.15, 0.13, 17, "glass", { windows: 4 });
        wing(-0.42, 0.07, 0.10, 0.08, 11, "steel", { windows: 3 });
        for (var garden = 0; garden < 3; garden += 1) {
          var gardenX = cx + hw * (0.04 + garden * 0.15);
          var gardenY = cy + hh * 0.12;
          addBox(gardenX, gardenY, hw * 0.045, hh * 0.045, 2, "concrete", { z0: baseZ });
          models.push({ kind: "shrub", x: gardenX, y: gardenY, s: 0.8, z0: baseZ + 2 });
        }
      } else if (variant === 6) {
        wing(0.23, -0.27, 0.19, 0.24, 28, "steel", { roof: "saw", door: true });
        wing(-0.01, -0.04, 0.055, 0.075, 9, "glass");
        models.push({ kind: "transferBridge",
                      path: [{ x: cx - hw * 0.28, y: cy - hh * 0.22 },
                             { x: cx + hw * 0.24, y: cy - hh * 0.22 }],
                      h0: 32, h1: 32, width: 3.5 });
        for (var reel = 0; reel < 3; reel += 1) {
          models.push({ kind: "reel", x: cx - hw * 0.52 + reel * hw * 0.14,
                        y: cy + hh * 0.15, s: 0.8, z0: baseZ });
        }
      } else {
        [{ x: 0.04, y: -0.72, h: 48, role: "steel" },
         { x: 0.25, y: -0.55, h: 39, role: "rust" }].forEach(function (column) {
          models.push({ kind: "distillation", x: cx + hw * column.x, y: cy + hh * column.y,
                        r: 3.2, hw: 5.5, hh: 3, h: column.h, z0: baseZ,
                        platforms: 3, role: column.role });
        });
        tank(-0.06, -0.51, 0.085, 43, "rust");
        tank(0.25, -0.37, 0.10, 32, "steel");
        tank(0.48, -0.10, 0.07, 24, "rust");
        tank(0.12, -0.02, 0.065, 18, "steel");
        wing(-0.36, 0.18, 0.12, 0.075, 10, "glass");
        stack(-0.30, -0.43, 48);
        models.push({ kind: "pipeRack",
                      path: [{ x: cx - hw * 0.04, y: cy - hh * 0.34 },
                             { x: cx + hw * 0.43, y: cy - hh * 0.12 }],
                      h0: 9, h1: 9, width: 3 });
      }
      var gateSide = (f.entrance.x >= cx) ? 1 : -1;
      if (variant === 0 || variant === 1 || variant === 6 || variant === 7) {
        wing(gateSide * 0.63, 0.04, 0.045, 0.05, 7, "steel", { door: true, gatehouse: true });
      }
      models.slice(compoundStart).forEach(function (m) {
        m.projectId = f.id;
        m.compoundPart = true;
      });

      // 前场机台：围绕分配带排两排；机台自带底座与状态灯柱
      // 机台尺寸是「生产能不能看清」的总闸：18×9 的机器在全景下只有 7px 宽，
      // 上面的活塞、状态灯、火花全糊成一格。放大到 23×12 后，一台机器的动作
      // 在 1080 宽窗口里能认出是「在干活」而不是一个色块。
      f.lines.forEach(function (rec, li) {
        models.push({ kind: "machine", x: rec.x, y: rec.y, hw: MACHINE_HW, hh: MACHINE_HH,
                      h: 15.5, z0: baseZ, line: rec.line, rec: rec, index: li });
      });
      // 分配带：入口 → 前场枢纽，再分短支线停在每台机台身后（低于机台，不压工作面）
      var hub = f.hub || { x: cx + hw * 0.30, y: cy + hh * 0.12 };
      models.push({ kind: "conveyor", path: routeInYard(f.entrance, hub),
                    h0: 9, h1: 9, width: 2.6, ore: false, projectId: f.id });
      f.lines.forEach(function (rec) {
        models.push({ kind: "conveyor", path: [hub, { x: rec.x, y: rec.y - 7 }],
                      h0: 9, h1: 7, width: 1.6, ore: false, spur: true, projectId: f.id });
      });
      // 前场两角：成品托盘 / 料箱与卷盘；灯杆与工头放在通道边
      // 取货托盘（pick）：站在装货纵列上，只放一只可取箱——叉车取货的锚点
      f.pickModel = { kind: "pallet", x: f.tray.x, y: f.tray.y, z0: baseZ, projectId: f.id,
                      bay: true, pick: true, hw: PALLET_HALF, hh: TRAY_HALF };
      models.push(f.pickModel);
      // 成品垛陈列：移到装货纵列外侧，不与叉车作业线重叠
      f.palletModel = { kind: "pallet", x: f.palletSpot.x, y: f.palletSpot.y, z0: baseZ,
                        projectId: f.id, hw: PALLET_HALF, hh: TRAY_HALF };
      models.push(f.palletModel);
      if (variant === 0 || variant === 3 || variant === 7) {
        addBox(cx - hw * 0.48, cy + hh * 0.26, 5.4, 2.7, 3, "concrete", { z0: baseZ, pad: true });
        models.push({ kind: "crate", x: cx - hw * 0.48, y: cy + hh * 0.24, s: 0.9, z0: baseZ });
        models.push({ kind: "barrel", x: cx - hw * 0.57, y: cy + hh * 0.31, s: 0.9, z0: baseZ });
        if (variant === 7) {
          models.push({ kind: "barrel", x: cx - hw * 0.45, y: cy + hh * 0.39, s: 0.8, z0: baseZ });
        }
      } else if (variant === 1 || variant === 2) {
        models.push({ kind: "crate", x: cx - hw * 0.42, y: cy + hh * 0.30, s: 1.1, z0: baseZ });
        models.push({ kind: "reel", x: cx - hw * 0.24, y: cy + hh * 0.47, s: 0.85, z0: baseZ });
      }
      var sheep = f.sheep || { x: cx - hw * 0.45, y: cy + hh * 0.36 };
      // 工头小羊：开工时沿巡检环走，停工 / 出事时退回门房附近。
      // 它是这座厂区「有没有人」的那一笔——一个 7px 的身影比任何数字都更像在上班。
      // dynamic=true：不进静态画序缓存，每帧按 workerPose 的实际位置与静态画序合并。
      var workerModel = { kind: "worker", x: sheep.x, y: sheep.y, z0: baseZ, s: 0.62,
                          factory: f, seed: Math.abs(hashKey("sheep" + f.id)) % 9,
                          projectId: f.id, dynamic: true };
      models.push(workerModel);
      f.worker = workerModel;
      models.push({ kind: "lamp", x: cx - hw * 0.52, y: cy + hh * 0.16, z0: baseZ, h: 30 });
      models.push({ kind: "lamp", x: cx + hw * 0.52, y: cy - hh * 0.16, z0: baseZ, h: 26 });
      if (variant === 0) {
        models.push({ kind: "pipeRack",
                      path: [{ x: cx + hw * 0.43, y: cy - hh * 0.15 },
                             { x: cx + hw * 0.23, y: cy - hh * 0.27 }],
                      h0: 6, h1: 6, width: 3, projectId: f.id, compoundPart: true });
      }
      models.push({ kind: "conveyor", path: f.belt, h0: 12, h1: 12, width: 4, ore: true,
                    projectId: f.id });
      // 出货门岗：立在车道南侧的路边（两柱沿 x，不跨车道），开工抬起、停工落下、
      // 出事染红——「这批货能出门吗」的即时读数。位置由 layoutFactory 按车道算出。
      f.gate = f.gate || { x: cx + hw * 0.46, y: cy + hh * 0.40 };
      f.gateModel = { kind: "gate", x: f.gate.x, y: f.gate.y, z0: baseZ,
                      projectId: f.id, factory: f };
      models.push(f.gateModel);
      // 返工区：前场靠边的一块空地 + 按今日失败 / 被拦数堆起来的红箱（0 件时也画空地）。
      // 与右前方的成品托盘形成一对——「做成」和「没做成」在同一个场院里。
      f.rework = { x: cx - hw * 0.50, y: cy + hh * 0.34 };
      models.push({ kind: "rework", x: f.rework.x, y: f.rework.y, z0: baseZ,
                    projectId: f.id, factory: f });
      // 二遍布局：拿到厂区全部真实占地（厂房 / 机台 / 传送带 / 返工区）后重算车道线
      // 与装货纵列，再把托盘 / 门岗 / 工头三件挪到定稿位置——占地先于动线。
      layoutFactory(f, models);
      f.pickModel.x = f.tray.x; f.pickModel.y = f.tray.y;
      f.palletModel.x = f.palletSpot.x; f.palletModel.y = f.palletSpot.y;
      f.gateModel.x = f.gate.x; f.gateModel.y = f.gate.y;
      f.worker.x = f.sheep.x; f.worker.y = f.sheep.y;
      // 名牌按主体屋脊定位，不跟随烟囱或独立反应罐抬高。
      f.labelZ = topZ + 10;
    });

    var perimeterStart = models.length;
    var perimeterSmokeStart = smoke.length;
    function anchorFacility(start, smokeStart, name, origin, target) {
      models.slice(start).concat(smoke.slice(smokeStart)).forEach(function (m) {
        m.facility = name;
        m.facilityOrigin = origin;
        m.facilityTarget = target;
      });
    }
    var coolingStart = models.length, coolingSmokeStart = smoke.length;
    // 外围设施共用组坐标；局部零件间距不随扩圈变化。
    models.push({ kind: "cooling", x: 356, y: -233, r: 16, h: 52, z0: 0 });
    models.push({ kind: "cooling", x: 404, y: -207, r: 14, h: 46, z0: 0 });
    smoke.push({ x: 356, y: -233, h: 55, steam: true });
    smoke.push({ x: 404, y: -207, h: 49, steam: true });
    addBox(325, -250, 13, 6.5, 10, "steel", { z0: 0, windows: 2, roof: "flat", vent: true });
    models.push({ kind: "pipeRack", path: [{ x: 338, y: -246 }, { x: 352, y: -236 }], h0: 10, h1: 10, width: 6 });

    anchorFacility(coolingStart, coolingSmokeStart, "coolingPlant",
                   { x: 380, y: -220 }, { x: 155, y: -140 });

    models.push({ kind: "box", x: 480, y: -220, hw: 42, hh: 22, h: 24, role: "steel", windows: 4, roof: "saw", bg: true });
    models.push({ kind: "cyl", x: 510, y: -180, r: 16, h: 34, role: "rust", bands: 4, bg: true });
    // 远景工业带：独立物件不参与建筑组的零件位移。

    models.push({ kind: "cyl", x: 470, y: -100, r: 12, h: 26, role: "steel", bands: 3, bg: true, roadsideTank: 1 });
    models.push({ kind: "box", x: -480, y: -200, hw: 50, hh: 26, h: 22, role: "steel", windows: 3, roof: "flat", bg: true });
    models.push({ kind: "cyl", x: -460, y: -60, r: 24, h: 40, role: "rust", bands: 4, bg: true, roadsideTank: -1 });
    models.push({ kind: "pylon", x: -380, y: -160, bg: true });
    models.push({ kind: "pylon", x: 400, y: -150, bg: true });
    models.push({ kind: "pylon", x: 520, y: -140, bg: true });
    models.push({ kind: "truck", x: -360, y: 190, z0: 0, bg: true });
    models.push({ kind: "truck", x: 360, y: 200, z0: 0, bg: true });
    models.push({ kind: "ore", x: 380, y: 170, s: 2.6, bg: true });
    models.push({ kind: "ore", x: -380, y: 175, s: 2.2, bg: true });
    models.push({ kind: "rock", x: -380, y: -170, s: 2, bg: true });
    models.push({ kind: "rock", x: 400, y: 80, s: 1.6, bg: true });
    models.push({ kind: "shrub", x: -360, y: 180, s: 1.3, bg: true });
    models.push({ kind: "shrub", x: 360, y: 180, s: 1.2, bg: true });
    models.push({ kind: "conveyor", path: [{ x: 454, y: -224 }, { x: 508, y: -180 }], h0: 16, h1: 10, width: 2.6, ore: true, bg: true });
    models.push({ kind: "transferBridge", path: [{ x: 468, y: -196 }, { x: 528, y: -152 }],
                  h0: 16, h1: 12, width: 6, bg: true });
    models.push({ kind: "box", x: 466, y: -198, hw: 7, hh: 4, h: 20, role: "steel", windows: 1, roof: "flat", bg: true });
    models.push({ kind: "box", x: 530, y: -150, hw: 6, hh: 3.5, h: 18, role: "rust", roof: "flat", bg: true });
    var frontY = factories.reduce(function (y, f) { return Math.max(y, f.y + f.halfH); }, 110) + 25;
    models.push({ kind: "truck", x: -380, y: frontY + 10, z0: 0, bg: true });
    // 外围新设施：变电站、水塔和维修库；与项目厂区保持独立。
    var substationStart = models.length;
    addBox(-410, -182, 22, 11, 3, "concrete", { paving: true, bg: true, facility: "substation" });
    for (var tr = 0; tr < 2; tr += 1) {
      addBox(-418 + tr * 15, -182, 5, 2.5, 9, "steel", { bg: true, facility: "substation", vent: true });
      for (var ins = 0; ins < 3; ins += 1) {
        addCyl(-421 + tr * 15 + ins * 3, -182, 0.9, 4, "glass",
               { z0: 9, bg: true, facility: "substation" });
      }
    }
    anchorFacility(substationStart, smoke.length, "substation",
                   { x: -410, y: -182 }, { x: -210, y: -92 });
    var waterStart = models.length;
    addBox(-414, -111, 12, 6, 3, "concrete", { paving: true, bg: true, facility: "waterTower" });
    [[-6, -2], [6, -2], [0, 3]].forEach(function (leg) {
      addCyl(-414 + leg[0], -111 + leg[1], 1.2, 22, "steel", { bg: true, facility: "waterTower" });
    });
    addCyl(-414, -111, 11, 13, "steel", { z0: 22, bg: true, bands: 2, ladder: true, facility: "waterTower" });
    anchorFacility(waterStart, smoke.length, "waterTower",
                   { x: -414, y: -111 }, { x: -214, y: -21 });
    var repairStart = models.length;
    addBox(441, -255, 21, 10.5, 16, "rust",
           { windows: 2, roof: "saw", bg: true, facility: "repairDepot", door: true });
    models.push({ kind: "truck", x: 462, y: -245, z0: 0, bg: true, facility: "repairDepot" });
    anchorFacility(repairStart, smoke.length, "repairDepot",
                   { x: 441, y: -255 }, { x: 231, y: -130 });
    // 厂外建筑群：不生成项目名牌、任务机台或运行状态；整组锚在 facilityAnchor 上，
    // 项目变多（环数 +1）时按锚点整体外移，组内相对位置不变。
    // opts.near=true：摆在前场做近景——不标 bg（bg 会被 roleCols 与雾化调色板压成灰调远景）。
    // opts.plateauAligned=true：位置已按台地前缘公路算好，别再按环数位移（见 perimeterOffset）。
    function utilityGroup(name, x, y, build, opts) {
      var start = models.length;
      build(x, y);
      models.slice(start).forEach(function (m) {
        if (!(opts && opts.near)) m.bg = true;
        m.facility = name;
        if (opts && opts.plateauAligned) m.plateauAligned = true;
        else m.facilityAnchor = { x: x, y: y };
      });
    }
    utilityGroup("serviceQuarter", -170, -140, function (x, y) {
      addBox(x, y, 66, 27, 2, "concrete", { paving: true });
      addBox(x - 26, y - 8, 24, 12, 30, "steel", { z0: 2, roof: "flat", windows: 4, door: true });
      addBox(x - 26, y - 8, 17, 8, 8, "glass", { z0: 32, roof: "flat", windows: 3 });
      addBox(x + 26, y - 5, 20, 10, 19, "rust", { z0: 2, roof: "saw", windows: 3, door: true });
      addBox(x + 4, y + 14, 15, 6, 10, "steel", { z0: 2, roof: "flat", windows: 2, canopy: true });
      addBox(x - 48, y + 9, 9, 4.5, 9, "steel", { z0: 2, roof: "flat", door: true });
      models.push({ kind: "truck", x: x + 39, y: y + 15, z0: 2 });
      models.push({ kind: "lamp", x: x - 8, y: y + 25, z0: 2, h: 22 });
    });
    utilityGroup("energyQuarter", 275, -160, function (x, y) {
      addBox(x, y, 63, 27, 2, "concrete", { paving: true });
      addBox(x - 25, y - 7, 23, 11.5, 25, "rust",
             { z0: 2, roof: "saw", windows: 3, door: true, pipes: true });
      addBox(x - 26, y - 7, 9, 4.5, 6, "steel", { z0: 32, roof: "flat", vent: true });
      addBox(x + 18, y + 9, 17, 8.5, 13, "steel", { z0: 2, roof: "flat", windows: 3, vent: true });
      addCyl(x + 22, y - 15, 9, 31, "steel", { z0: 2, bands: 3, ladder: true });
      addCyl(x + 45, y - 4, 7, 23, "rust", { z0: 2, bands: 2, ladder: true });
      models.push({ kind: "pipeRack", path: [{ x: x + 17, y: y - 9 }, { x: x + 41, y: y + 4 }],
                    h0: 8, h1: 8, width: 3 });
      models.push({ kind: "lamp", x: x - 37, y: y + 19, z0: 2, h: 24 });
    });
    // 西侧物资场：填住外圈后勤带与料场之间那块空沙地，作为库房 / 工棚 / 备件场的补充
    utilityGroup("westDepot", -360, -70, function (x, y) {
      addBox(x - 20, y, 38, 25, 2, "concrete", { paving: true });
      // 主库房（左后）+ 长条工棚（右后），中间留出消防通道
      addBox(x - 25, y - 6, 18, 9, 16, "steel",
             { z0: 2, roof: "flat", windows: 4, door: true });
      addBox(x + 15, y - 21, 13, 5.5, 10, "rust",
             { z0: 2, roof: "saw", windows: 2, door: true });
      // 检修工棚（右侧）与门房（左前）
      addBox(x + 33, y - 6, 11, 6, 10, "steel",
             { z0: 2, roof: "saw", windows: 2, door: true });
      addBox(x - 41, y + 8, 6, 3.5, 7, "glass",
             { z0: 2, roof: "flat", windows: 2, door: true });
      // 备件罐（右前）与管廊
      addCyl(x + 41, y + 14, 5.5, 14, "rust", { z0: 2, bands: 2, ladder: true });
      addCyl(x + 53, y + 8, 4.5, 10, "steel", { z0: 2, bands: 1, ladder: true });
      models.push({ kind: "pipeRack", path: [{ x: x - 25, y: y + 6 }, { x: x - 5, y: y + 6 }],
                    h0: 6, h1: 6, width: 3 });
      // 前场：两辆卡车、两盏灯，拉开整组的横向跨度
      models.push({ kind: "truck", x: x - 21, y: y + 24, z0: 2 });
      models.push({ kind: "truck", x: x + 25, y: y + 24, z0: 2 });
      models.push({ kind: "lamp", x: x - 47, y: y + 24, z0: 2, h: 24 });
      models.push({ kind: "lamp", x: x + 41, y: y + 24, z0: 2, h: 22 });
      // 散料：别都堆在一角
      models.push({ kind: "crate", x: x - 46, y: y - 20, z0: 2, s: 1.1 });
      models.push({ kind: "crate", x: x + 40, y: y + 8, z0: 2, s: 0.9 });
      models.push({ kind: "barrel", x: x + 15, y: y + 22, z0: 2, s: 1 });
      models.push({ kind: "reel", x: x - 35, y: y + 22, z0: 2, s: 0.9 });
      // 小龙门吊站在场院右前缘，吊钩下留一组备件箱
      models.push({ kind: "gantry", x: x + 49, y: y + 30, w: 7, h: 18, hw: 8, hh: 3 });
      models.push({ kind: "crate", x: x + 49, y: y + 34, s: 1.2 });
    }, { near: true });
    // 前场两组近景：贴着前缘公路外侧布置（左西料场、右东罐区）。
    // 位置按当前台地算——公路走向就是台地前缘那两条边，台地变大两组跟着挪，
    // 所以它们标 plateauAligned，不再参与 perimeterOffset 的按环位移（见下）。
    // 场坪一律 2:1 菱形，与园区坪、公路同一倾斜度。
    var roadLen = Math.hypot(plateau.hw + 14, plateau.hh + 8) || 1;
    function roadsideAnchor(side, t, gap) {
      // 前缘左/右臂：侧角 → 前角；返回「路外侧 gap 处」的场地中心（gap=0 即落在公路中线上）
      var ax = side * (plateau.hw + 14), ay = 0;
      var bx = 0, by = plateau.hh + 8;
      var px = ax + (bx - ax) * t, py = ay + (by - ay) * t;
      var nx = side * (plateau.hh + 8) / roadLen, ny = (plateau.hw + 14) / roadLen;
      return { x: Math.round(px + nx * gap), y: Math.round(py + ny * gap),
               nx: nx, ny: ny };
    }
    // 间距不写死：整组建完先量一次「最靠路那面墙沿外法线到公路中线的距离」（负=压上路），
    // 再把整组沿外法线挪过去，只留一条排水沟的宽度。改建筑布局不用回来手改常数。
    var ROAD_CLEARANCE = 14;
    function alignYardToRoad(name, armCorner, n) {
      var near = Infinity;
      models.forEach(function (m) {
        if (m.facility !== name) return;
        var pts = m.path || [{ x: m.x, y: m.y }];
        var hw = m.hw || m.r || 0;
        var hh = m.hh || (m.r ? m.r * 0.5 : 0);
        // 轴系零件（煤棚 / 筒仓群）的纵深本来就沿外法线摆，支撑长度就是 hh；
        // 轴对齐零件要按两条轴在外法线上的投影合成。
        var extent = m.axis ? hh : (hw * Math.abs(n.x) + hh * Math.abs(n.y));
        pts.forEach(function (pt) {
          var d = (pt.x - armCorner.x) * n.x + (pt.y - armCorner.y) * n.y - extent;
          near = Math.min(near, d);
        });
      });
      if (!isFinite(near)) return;
      var shift = Math.round(ROAD_CLEARANCE - near);
      models.forEach(function (m) {
        if (m.facility !== name) return;
        if (m.path) {
          m.path = m.path.map(function (pt) {
            return { x: pt.x + n.x * shift, y: pt.y + n.y * shift };
          });
        } else { m.x += n.x * shift; m.y += n.y * shift; }
      });
    }
    // 两组站在沙地上、贴前缘公路外侧；位置都按当前台地算（公路走向就是台地前缘
    // 那两条边），台地随项目变多长大时跟着挪。
    var stockYard = roadsideAnchor(-1, 0.52, 0);
    var tankYard = roadsideAnchor(1, 0.52, 0);

    // 料场整组顺着公路排：u 轴取公路方向，v 轴取前缘那条边的外法线（v 越大越远离马路）。
    // 老版把「纵深」当成世界 y 轴，而公路本身是斜的，摆出来整排与马路不平行，最高体量
    // 反而被推到最靠路的一侧；现在组内一律用 (u, v) 表达，整排与马路平行。
    var stockNormal = { x: -(plateau.hh + 8) / roadLen, y: (plateau.hw + 14) / roadLen };
    var stockAlong = { x: stockNormal.y, y: -stockNormal.x };

    // 料场按两条马路的方向摆：L 沿近侧公路，R 沿另一条路；两条轴围出平行四边形场院，
    // 三排内容沿 R 方向由后往前排——后排筒仓、中排煤棚与煤堆、前排地磅与排队卡车。
    // 这样围栏的两条翼栅和场内每一排都共用同两条马路方向，不会一边斜一边正。
    utilityGroup("stockyard", stockYard.x, stockYard.y, function (x, y) {
      function at(u, v) {
        return { x: x + stockAlong.x * u + stockNormal.x * v,
                 y: y + stockAlong.y * u + stockNormal.y * v };
      }
      // 围栏后缘中点作为场院坐标原点；L 沿近路，R 沿另一条路并向排前方向
      var backC = at(0, 72);
      var cross = { x: stockAlong.x, y: -stockAlong.y };
      function site(l, d) {
        return { x: backC.x + stockAlong.x * l + cross.x * d,
                 y: backC.y + stockAlong.y * l + cross.y * d };
      }
      var W = 75, D = 95;
      // 后排：筒仓群 + 提升机塔，仓列沿 L 排开
      models.push(Object.assign({ kind: "siloBattery", axis: stockAlong,
                    count: 3, r: 6.4, h: 30, gapX: 24, towerGap: 9, role: "steel",
                    hw: 58, hh: 4 }, site(0, 12)));
      // 中排：煤棚居中，两堆煤分列左右
      models.push(Object.assign({ kind: "coalShed", axis: stockAlong,
                    hw: 34, hh: 11, h: 20, rise: 7, role: "steel" }, site(0, 45)));
      models.push(Object.assign({ kind: "coalPile", hw: 14, hh: 8, h: 10 }, site(-52, 45)));
      models.push(Object.assign({ kind: "coalPile", hw: 11, hh: 6.5, h: 8 }, site(50, 45)));
      // 运煤线：塔顶 → 煤棚脊
      models.push({ kind: "conveyor", path: [site(46, 13), site(-14, 45)],
                    h0: 40, h1: 20, width: 2.6, ore: true });
      // 前排：地磅在中间，排队车朝公路一侧收口，磅房贴在地磅左前方
      models.push(Object.assign({ kind: "weighbridge", hw: 18, hh: 8, h: 2.5 },
                                site(-20, 60)));
      models.push(Object.assign({ kind: "truck", z0: 3 }, site(-20, 60)));
      models.push(Object.assign({ kind: "truck" }, site(-20, 78)));
      models.push(Object.assign({ kind: "truck" }, site(-20, 90)));
      var booth = site(-38, 80);
      addBox(booth.x, booth.y, 5, 4, 7, "concrete", { roof: "flat", windows: 2, door: true });
      models.push(Object.assign({ kind: "crate", s: 0.9 }, site(30, 82)));
      models.push(Object.assign({ kind: "barrel", s: 0.9 }, site(45, 80)));
      models.push(Object.assign({ kind: "reel", s: 0.9 }, site(-60, 70)));
      // 灯杆：前缘两端 + 地磅入口 + 煤堆侧
      models.push(Object.assign({ kind: "lamp", h: 26 }, site(-65, 88)));
      models.push(Object.assign({ kind: "lamp", h: 22 }, site(65, 88)));
      models.push(Object.assign({ kind: "lamp", h: 20 }, site(-45, 92)));
      models.push(Object.assign({ kind: "lamp", h: 22 }, site(-65, 45)));
      // 两盏冷白投光：一盏照筒仓群，一盏照煤棚右翼
      models.push(Object.assign({ kind: "floodlight", hw: 6, hh: 3, h: 36 }, site(-65, 25)));
      models.push(Object.assign({ kind: "floodlight", hw: 6, hh: 3, h: 34 }, site(67, 30)));
      // 围栏：后缘沿 L，左右翼沿 R；前侧完全敞开，地磅入口正对公路
      models.push({ kind: "fence", role: "steel", h: 4, width: 1.5, postGap: 6,
                    path: [site(-W, D), site(-W, 0), site(W, 0), site(W, D)] });
    }, { near: true, plateauAligned: true });
    // 前场东侧罐区：北排是生产（气柜 + 循环水池 + 泵房/管廊），南排是装车（台 + 罐车），
    // 棚子必须整块落在北排的前缘以南——它在 h=15 处张开，压到气柜 / 水池底上就看着粘在一起。
    utilityGroup("tankFarm", tankYard.x, tankYard.y, function (x, y) {
      models.push({ kind: "gasholder", x: x - 46, y: y - 10, r: 15, h: 30, role: "rust" });
      models.push({ kind: "basin", x: x + 30, y: y - 14, hw: 30, hh: 15, h: 3 });
      addBox(x + 62, y - 4, 9, 4.5, 8, "steel", { roof: "flat", windows: 2, vent: true });
      models.push({ kind: "pipeRack", path: [{ x: x + 30, y: y - 4 }, { x: x + 56, y: y - 4 }],
                    h0: 7, h1: 7, width: 2.5 });
      models.push({ kind: "loadingRack", x: x - 8, y: y + 30, hw: 22, hh: 11, h: 15 });
      models.push({ kind: "truck", x: x - 8, y: y + 38 });   // 停在棚下靠外的位置
      models.push({ kind: "flare", x: x + 64, y: y + 12, hw: 7, hh: 3.5, h: 54 });
      models.push({ kind: "barrel", x: x - 58, y: y + 14, s: 1 });
      models.push({ kind: "lamp", x: x - 64, y: y + 36, h: 24 });
      models.push({ kind: "lamp", x: x + 44, y: y + 36, h: 26 });
    }, { near: true, plateauAligned: true });
    // 贴路：两组各挪到「建筑最近那面墙离公路中线 14 单位」。
    [["stockyard", stockYard, -1], ["tankFarm", tankYard, 1]].forEach(function (item) {
      var side = item[2];
      var n = { x: side * (plateau.hh + 8) / roadLen, y: (plateau.hw + 14) / roadLen };
      alignYardToRoad(item[0], { x: side * (plateau.hw + 14), y: 0 }, n);
    });
    var tracks = [];
    var perimeterRing = Math.max(1, Math.ceil((Math.sqrt(factories.length + 1) - 1) / 2));
    function perimeterOffset(m) {
      if (m.plateauAligned) return { x: 0, y: 0 };
      if (m.facilityOrigin) {
        return { x: m.facilityTarget.x * perimeterRing - m.facilityOrigin.x,
                 y: m.facilityTarget.y * perimeterRing - m.facilityOrigin.y };
      }
      if (m.roadsideTank) {
        // 罐体连同高度投影退到前缘道路外侧，不能只让底心离开路面。
        var tankX = m.roadsideTank < 0 ? -300 : 320;
        var tankY = m.roadsideTank < 0 ? 110 : 95;
        return { x: tankX * perimeterRing - m.x, y: tankY * perimeterRing - m.y };
      }
      if (m.facilityAnchor) {
        return { x: m.facilityAnchor.x * (perimeterRing - 1),
                 y: m.facilityAnchor.y * (perimeterRing - 1) };
      }
      var p = m.path ? pointAt(m.path, 0.5) : m;
      var dx, dy;
      if (!m.bg) { dx = -225; dy = 80; }
      else if (p.y < 0) {
        dx = p.x < 0 ? 200 : -210;
        dy = p.x < 0 ? 90 : 125;
        if (p.y > -110) { dx = p.x < 0 ? 200 : -180; dy = p.x < 0 ? 115 : 140; }
      } else { dx = p.x < 0 ? 170 : -170; dy = -60; }
      if (m.kind === "pylon") {
        if (p.x < 0) { dx = 170; dy = 50; }
        else if (p.x < 450) { dx = -170; dy = 75; }
        else { dx = -190; dy = 100; }
      }
      if (m.kind === "rock" && p.y < 0) { dx = 180; dy = 60; }
      if (m.kind === "rock" && p.y > 0) { dx = -150; dy = 0; }
      var x = p.x + dx, y = p.y + dy;
      if (perimeterRing > 1) {
        dx += x * (perimeterRing - 1);
        dy += y * (perimeterRing - 1);
      }
      return { x: dx, y: dy };
    }
    for (var pi = perimeterStart; pi < models.length; pi += 1) {
      var pm = models[pi], offset = perimeterOffset(pm);
      pm.peripheral = true;
      if (pm.path) {
        pm.path = pm.path.map(function (pt) { return { x: pt.x + offset.x, y: pt.y + offset.y }; });
      } else { pm.x += offset.x; pm.y += offset.y; }
    }
    for (var si = perimeterSmokeStart; si < smoke.length; si += 1) {
      var sm = smoke[si], smokeOffset = perimeterOffset(sm);
      sm.x += smokeOffset.x; sm.y += smokeOffset.y;
    }

    // 中央调度厅：石台 + 台阶 + 钢塔 + 检修环 + 玻璃冠 + 天线 + 四角灯柱
    addBox(hall.x, hall.y, 37, 18.5, 6, "concrete", { paving: true, hallBase: true });
    addBox(hall.x, hall.y, 30, 15, 2.5, "concrete", { z0: 6, step: true });
    for (var st = 0; st < 3; st += 1) {
      addBox(hall.x, hall.y + 20 + st * 2.1, 12 - st * 3, 1.1, 1 + st,
             "concrete", { z0: 0, step: true });
    }
    addBox(hall.x, hall.y, 18, 9, 40, "steel",
           { z0: 8.5, windows: 3, door: true, roof: "flat", tower: true });
    addBox(hall.x, hall.y, 20, 10, 9, "glass",
           { z0: 48.5, windows: 3, roof: "flat", crown: true });
    addCyl(hall.x, hall.y, 1.6, 10, "steel", { z0: 57.5 });
    models.push({ kind: "radar", x: hall.x, y: hall.y, z0: 67.5 });
    models.push({ kind: "lamp", x: hall.x - 42, y: hall.y + 13, z0: 6, h: 24 });
    models.push({ kind: "lamp", x: hall.x + 42, y: hall.y + 13, z0: 6, h: 24 });
    models.push({ kind: "lamp", x: hall.x - 36, y: hall.y - 14, z0: 6, h: 20 });
    models.push({ kind: "lamp", x: hall.x + 36, y: hall.y - 14, z0: 6, h: 20 });
    models.push({ kind: "flag", x: hall.x - 14, y: hall.y + 19, z0: 6, h: 26 });
    models.push({ kind: "flag", x: hall.x + 14, y: hall.y + 19, z0: 6, h: 26 });

    return { models: models, patches: patches, smoke: smoke, sparks: sparks,
             tracks: tracks, roads: roads };
  }

  function routeBelt(a, b, seed) {
    var dx = b.x - a.x, dy = b.y - a.y;
    var length = Math.hypot(dx, dy) || 1;
    // 从调度厅外墙出发；直达对应入口，不绕去另一个厂区。
    var start = Math.min(31, length * 0.25);
    return [
      { x: a.x + dx / length * start, y: a.y + dy / length * start },
      { x: b.x, y: b.y },
    ];
  }

  /** 进厂分配带：入口先沿自己的 x 走到枢纽的那一行，再横着过去——斜穿会压到
      厂心的装货纵列（车道 / 托盘 / 叉车都在那一条 x 上）。 */
  function routeInYard(entrance, hub) {
    if (Math.abs(entrance.y - hub.y) < 6) return [entrance, hub];
    return [entrance, { x: entrance.x, y: hub.y }, hub];
  }

  function transitSeconds(pts) {
    var len = 0;
    for (var i = 1; i < pts.length; i += 1) {
      len += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    }
    return clamp(len / 34, 3, 12);
  }

  function pathLength(pts) {
    var len = 0;
    for (var i = 1; i < pts.length; i += 1) {
      len += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
    }
    return len;
  }

  function pointAt(pts, t) {
    if (!pts || !pts.length) return { x: 0, y: 0 };
    if (t <= 0) return pts[0];
    if (t >= 1) return pts[pts.length - 1];
    var total = pathLength(pts);
    var want = total * t;
    var acc = 0;
    for (var i = 1; i < pts.length; i += 1) {
      var seg = Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
      if (acc + seg >= want) {
        var k = seg ? (want - acc) / seg : 0;
        return {
          x: pts[i - 1].x + (pts[i].x - pts[i - 1].x) * k,
          y: pts[i - 1].y + (pts[i].y - pts[i - 1].y) * k,
        };
      }
      acc += seg;
    }
    return pts[pts.length - 1];
  }

  // ---------- 装卸几何（纯函数：真实占地 → 安全装卸位置与路线） ----------
  /** 机台占地包络：按每台机台的真实位姿（含未绘制的产线）取矩形，不用经验系数。 */
  function machineField(f) {
    var pts = (f && f.machines) || [];
    if (!pts.length) {
      pts = [{ x: f.x, y: f.y + f.halfH * LINE_ROW_Y }];   // 没有产线：一条名义机台行
    }
    var minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    pts.forEach(function (p) {
      minX = Math.min(minX, p.x - MACHINE_HW); maxX = Math.max(maxX, p.x + MACHINE_HW);
      minY = Math.min(minY, p.y - MACHINE_HH); maxY = Math.max(maxY, p.y + MACHINE_HH);
    });
    return { minX: minX, maxX: maxX, minY: minY, maxY: maxY };
  }

  /** 折线在给定 y 处穿过的 x（不跨越该 y 时为 null）。 */
  function crossX(pts, y) {
    if (!pts || pts.length < 2) return null;
    for (var i = 1; i < pts.length; i += 1) {
      var a = pts[i - 1], b = pts[i];
      if (b.y === a.y) continue;
      if ((a.y <= y && b.y >= y) || (b.y <= y && a.y >= y)) {
        var k = (y - a.y) / (b.y - a.y);
        return a.x + (b.x - a.x) * k;
      }
    }
    return null;
  }

  /** 菱形坪内判据：把 (x,y) 与自己的半尺寸一起收进本厂菱形（四角都算；竖直高度不计）。 */
  function insideDiamond(f, x, y, hx, hy) {
    var hw = f.halfW * YARD_K, hh = f.halfH * YARD_K;
    var dx = x - f.x, dy = y - f.y;
    var m = 0;
    [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(function (s) {
      m = Math.max(m, Math.abs(dx + s[0] * hx) / hw + Math.abs(dy + s[1] * hy) / hh);
    });
    return m <= 1 + 1e-9;
  }

  /** 沿单轴把落点收进菱形（保留另一轴）：空间不够就缩短而不是外溢。 */
  function clampDiamondX(f, x, y, hx, hy) {
    if (insideDiamond(f, x, y, hx, hy)) return x;
    var hw = f.halfW * YARD_K, hh = f.halfH * YARD_K;
    var room = hw * (1 - (Math.abs(y - f.y) + hy) / hh) - hx;
    return f.x + (x >= f.x ? 1 : -1) * Math.max(0, Math.min(Math.abs(x - f.x), room));
  }
  function clampDiamondY(f, x, y, hx, hy) {
    if (insideDiamond(f, x, y, hx, hy)) return y;
    var hw = f.halfW * YARD_K, hh = f.halfH * YARD_K;
    var room = hh * (1 - (Math.abs(x - f.x) + hx) / hw) - hy;
    return f.y + (y >= f.y ? 1 : -1) * Math.max(0, Math.min(Math.abs(y - f.y), room));
  }

  /** 两轴一致地收进菱形：单轴各夹一次会互相拆台（x 夹完 y 还按旧 x 算）。 */
  function fitDiamond(f, x, y, hx, hy) {
    for (var it = 0; it < 4; it += 1) {
      x = clampDiamondX(f, x, y, hx, hy);
      y = clampDiamondY(f, x, y, hx, hy);
      if (insideDiamond(f, x, y, hx, hy)) break;
    }
    return { x: x, y: y };
  }

  /** 某座厂区的静态占地矩形（真实模型尺寸；铺装是地面、动态件除外）。
      机台 / 厂房 / 罐 / 传送带 / 返工区 / 托盘都算——路线要按它们避让。 */
  function solidRects(models, f) {
    var out = [];
    (models || []).forEach(function (m) {
      if (!m || m.dynamic || m.paving) return;
      // 门岗 / 工头 / 托盘：门岗与托盘由本函数自己摆（放完再回填封锁），不参与他者避让
      if (m.kind === "gate" || m.kind === "worker" || m.kind === "pallet") return;
      if (String(m.projectId) !== String(f.id)) return;
      var pts = m.path || [{ x: m.x, y: m.y }];
      // 传送带先算：它没有 hw/hh，按「带面半宽（与支腿取大）+ 余量」的方芯沿路径铺开。
      // 不用整段包围盒——斜带的外接框会把车道/巡检逼出坪外，方芯贴着带身才够紧。
      if (m.kind === "conveyor") {
        var pad = Math.max((m.width || 3) * 0.5, 3) + 1;
        for (var i = 1; i < pts.length; i += 1) {
          var a = pts[i - 1], b = pts[i];
          var len = Math.hypot(b.x - a.x, b.y - a.y);
          var cnt = Math.max(1, Math.ceil(len / 4));
          for (var k = 0; k <= cnt; k += 1) {
            var tt = k / cnt;
            var px = a.x + (b.x - a.x) * tt, py = a.y + (b.y - a.y) * tt;
            out.push({ kind: m.kind, x0: px - pad, x1: px + pad, y0: py - pad, y1: py + pad });
          }
        }
        return;
      }
      var hw = m.hw, hh = m.hh;
      if (m.kind === "machine") { hw = MACHINE_HW; hh = MACHINE_HH; }
      if (m.kind === "rework") { hw = 13; hh = MACHINE_HH; }
      if (hw == null) {
        if (m.r != null) { hw = m.r; hh = m.r * 0.5; }
        else return;   // 箱 / 桶 / 卷盘 / 灯：太小，不参与路线避让
      }
      pts.forEach(function (p) {
        out.push({ kind: m.kind, x0: p.x - hw, x1: p.x + hw, y0: p.y - hh, y1: p.y + hh });
      });
    });
    return out;
  }

  function rectFree(rects, box) {
    for (var i = 0; i < rects.length; i += 1) {
      var r = rects[i];
      if (box.x1 <= r.x0 || box.x0 >= r.x1 || box.y1 <= r.y0 || box.y0 >= r.y1) continue;
      return false;
    }
    return true;
  }

  /** 车斗走廊：x 从靠站位到停车位（含车长半值），y 为车道线上下各半个车宽。 */
  function laneBox(dockX, bayX, laneY) {
    return { x0: Math.min(dockX, bayX) - TRUCK_HALF_L, x1: Math.max(dockX, bayX) + TRUCK_HALF_L,
             y0: laneY - TRUCK_HALF_W - 0.5, y1: laneY + TRUCK_HALF_W + 0.5 };
  }
  function laneBoxInYard(f, box) {
    return insideDiamond(f, box.x0, box.y0, 0, 0) && insideDiamond(f, box.x1, box.y1, 0, 0);
  }

  /** 车道 + 靠站位扫描：从机台包络以南逐格下移，找第一条「整段车斗走廊既不压静态
      占地、又整条落在本厂菱形坪内」的车道线；靠站位再在走廊内从远到近取最长的
      不压占地的那个位置。两侧都不行就返回 null。 */
  function scanLane(f, dockX, side, field, rects) {
    var top = field.maxY + LANE_CLEAR;
    var bottom = top;
    // 车道线下限：车斗四角必须在菱形内
    var hw = f.halfW * YARD_K, hh = f.halfH * YARD_K;
    var limit = f.y + hh * (1 - (Math.abs(dockX - f.x) + TRUCK_HALF_L) / hw) - TRUCK_HALF_W;
    for (var y = top; y <= limit; y += 1) {
      bottom = y;
      // 靠站位：从最远（菱形允许处）往回收，取第一个不压占地的位置
      var bayMax = clampDiamondX(f, dockX + side * 12, y, TRUCK_HALF_L, TRUCK_HALF_W);
      for (var bx = bayMax; Math.abs(bx - dockX) >= 4; bx -= side * 1) {
        var box = laneBox(dockX, bx, y);
        if (laneBoxInYard(f, box) && rectFree(rects, box)) return { y: y, bayX: bx };
      }
      var only = laneBox(dockX, dockX, y);
      if (laneBoxInYard(f, only) && rectFree(rects, only)) return { y: y, bayX: dockX };
    }
    return null;
  }

  /** 从区间里挖掉若干封锁段，返回剩余子区间。 */
  function subtractSpans(x0, x1, blocks) {
    var spans = [{ a: x0, b: x1 }];
    (blocks || []).forEach(function (bl) {
      var next = [];
      spans.forEach(function (s) {
        if (bl[1] <= s.a || bl[0] >= s.b) { next.push(s); return; }
        if (bl[0] > s.a) next.push({ a: s.a, b: bl[0] });
        if (bl[1] < s.b) next.push({ a: bl[1], b: s.b });
      });
      spans = next.filter(function (s) { return s.b - s.a > 0.5; });
    });
    return spans;
  }

  /** 巡检闭环：机台包络以南的安全带里取最宽的一段空地，避开装货纵列与传送带。
      四点是矩形（北路 / 东短边 / 南路 / 西长边），不做自由导航；不含菱形坪之外的点。 */
  /** 巡检带的两条腿（不依赖 x）：摆成品垛时要给巡检留出这条带。 */
  function patrolBand(f, field) {
    var hwK = f.halfW * YARD_K, hhK = f.halfH * YARD_K;
    var yN = field.maxY + PATROL_MARGIN;
    var yDeep = f.y + hhK * (1 - WORKER_R / hwK) - WORKER_R - 0.5;
    var yS = Math.min(yN + PATROL_SPAN, yDeep);
    if (yS < yN + 6) yS = yN;
    return { yN: yN, yS: Math.max(yS, yN) };
  }

  function patrolRoute(f, field, laneY, dockX, rects) {
    var hw = f.halfW, hh = f.halfH;
    var hwK = hw * YARD_K, hhK = hh * YARD_K;
    var band = patrolBand(f, field);
    var yN = band.yN, yS = band.yS;
    var blocks = [[dockX - BLOCK_PAD, dockX + BLOCK_PAD]];
    (rects || []).forEach(function (r) {
      if (r.y1 <= yN - WORKER_R || r.y0 >= yS + WORKER_R) return;   // 与本带不相交
      blocks.push([r.x0 - WORKER_R, r.x1 + WORKER_R]);
    });
    // 巡检带两端也要落在菱形内：按带内最深的 y 收 x 上限
    var deep = Math.max(Math.abs(yN - f.y), Math.abs(yS - f.y)) + WORKER_R;
    var xMax = Math.max(0, hw * YARD_K * (1 - deep / (hh * YARD_K)) - WORKER_R);
    var xLo = f.x - xMax, xHi = f.x + xMax;
    var spans = subtractSpans(xLo, xHi, blocks);
    if (spans.length) {
      spans.sort(function (p, q) { return (q.b - q.a) - (p.b - p.a); });
      var s = spans[0];
      var pad = Math.min(WORKER_R + 4, (s.b - s.a) * 0.2);
      var xL = s.a + pad, xR = s.b - pad;
      if (xR - xL >= 3) {
        return [{ x: xL, y: yN }, { x: xR, y: yN }, { x: xR, y: yS }, { x: xL, y: yS }];
      }
    }
    // 安全带被占满：退化成原地值守——在带内逐格找一个不压占地、且不出菱形的点
    var stand = null;
    var lim = hw * YARD_K * (1 - (Math.abs(yN - f.y) + WORKER_R) / hhK) - WORKER_R;
    for (var sx = -lim; sx <= lim; sx += 2) {
      if (!insideDiamond(f, f.x + sx, yN, WORKER_R, WORKER_R)) break;
      var pb = { x0: f.x + sx - WORKER_R, x1: f.x + sx + WORKER_R,
                 y0: yN - WORKER_R, y1: yN + WORKER_R };
      if (rectFree(rects, pb)) { stand = f.x + sx; break; }
    }
    var px = stand == null ? Math.max(-lim, Math.min(lim, 0)) + f.x : stand;
    return [{ x: px, y: yN }, { x: px, y: yN }, { x: px, y: yN }, { x: px, y: yN }];
  }

  /** 建立一整场的装卸几何：真实静态占地 → 安全车道线（含菱形收口）→ 装货纵列
      （货车停车位 / 取货托盘 / 叉车停靠位）→ 坪内靠站位与门岗 → 巡检闭环。
      纯函数（结果全部写回 f 便于断言）。车道走单轴直线（y = f.laneY）——车辆模型是
      沿屏幕 x 的侧视，只有沿 x 走才能与模型朝向一致；装货纵列与车道同一条 x。
      硬约束：所有落点连同各自半尺寸都在本厂菱形坪内（FX.insideDiamond）。
      models 可省略（buildWorld 先用只有机台包络的初值，buildIndustrialSite 拿到
      真实模型后再算一次）。 */
  function layoutFactory(f, models) {
    var hw = f.halfW, hh = f.halfH, fx = f.x, fy = f.y;
    var field = machineField(f);
    f.field = field;
    var rects = solidRects(models, f);

    // 车道线 + 坪内靠站位：两侧都试，取先成功的一侧
    var hitE = scanLane(f, fx + hw * DOCK_DX, 1, field, rects);
    var hitW = scanLane(f, fx - hw * DOCK_DX, -1, field, rects);
    var side = 1, pick2 = hitE || hitW;
    if (!hitE && hitW) side = -1;
    var dockX = fx + side * hw * DOCK_DX;
    var laneY = pick2 ? pick2.y : field.maxY + LANE_CLEAR;
    var bayX = pick2 ? pick2.bayX
      : clampDiamondX(f, dockX + side * 8, laneY, TRUCK_HALF_L, TRUCK_HALF_W);
    // 收口：车道线与停车位无论如何都要在菱形内
    laneY = clampDiamondY(f, dockX, laneY, TRUCK_HALF_L, TRUCK_HALF_W);
    bayX = clampDiamondX(f, bayX, laneY, TRUCK_HALF_L, TRUCK_HALF_W);

    f.laneY = laneY;
    f.entranceSide = side;
    f.bayX = bayX;
    f.dock = { x: dockX, y: laneY };
    f.forkLaneX = dockX;
    // 车斗上的卸货锚点：叉尖向北伸到车斗近侧边缘，就是这一点的地面位置。
    // 车斗上码的成箱与叉车放下的货箱共用它，坐标严格一致。
    f.dockBed = { x: dockX, y: laneY + DROP_GAP - FORK_REACH };
    f.dockDrop = { x: dockX, y: laneY + DROP_GAP };   // 放货位（叉尖伸进车斗近侧）
    // 取货托盘（x 必须留在装货纵列上，只定 y）：下界是「让开货车车斗」，
    // 上界是「托盘与叉车取货位都还在菱形内」。两个硬约束一起解，不靠单轴外推。
    var hwK2 = hw * YARD_K, hhK2 = hh * YARD_K, dx0 = Math.abs(dockX - fx);
    var yTrayMin = laneY + TRUCK_HALF_W + TRAY_HALF + 0.5;
    var yTrayMax = fy + Math.min(
      hhK2 * (1 - (dx0 + FORK_HALF) / hwK2) - FORK_REACH - FORK_HALF,   // 取货位在坪内
      hhK2 * (1 - (dx0 + PALLET_HALF) / hwK2) - TRAY_HALF);             // 托盘自身在坪内
    f.tray = { x: dockX,
               y: Math.max(yTrayMin, Math.min(laneY + TRAY_GAP, Math.max(yTrayMin, yTrayMax))) };
    // 取货时车体位置（叉尖压在托盘上）；常驻停靠位就在取货位——不放货时停这儿，
    // 既不凭空出现，也不另占纵深。
    f.forkPick = { x: dockX, y: f.tray.y + FORK_REACH };
    f.forkPark = { x: dockX, y: f.forkPick.y };
    // 坪内靠站位 / 等候点 / 路线端点（都在菱形内；无画外进场，淡入淡出发生在靠站位）
    var inFit = fitDiamond(f, bayX, laneY, TRUCK_HALF_L, TRUCK_HALF_W);
    f.truckIn = { x: inFit.x, y: inFit.y };
    f.apron = f.truckIn;
    f.frontCorner = { x: f.truckIn.x, y: laneY };
    var wFit = fitDiamond(f, dockX + side * Math.max(4, Math.abs(f.truckIn.x - dockX) / 2),
                          laneY, TRUCK_HALF_L, TRUCK_HALF_W);
    f.waitPt = { x: wFit.x, y: wFit.y };
    // 门岗：两根柱子沿 x，站在车道南侧、不跨车道（先摆它，巡检才好避让）
    var gtp = fitDiamond(f, dockX + side * hw * GATE_DX, laneY + GATE_DY, 10, 2.5);
    f.gate = { x: gtp.x, y: gtp.y };

    // 取货托盘 / 门岗先回填封锁，成品垛再摆，最后才排巡检带（避让所有既成事实）
    rects.push({ kind: "pallet", x0: f.tray.x - PALLET_HALF, x1: f.tray.x + PALLET_HALF,
                 y0: f.tray.y - TRAY_HALF, y1: f.tray.y + TRAY_HALF });
    rects.push({ kind: "gate", x0: f.gate.x - 10, x1: f.gate.x + 10,
                 y0: f.gate.y - 2.5, y1: f.gate.y + 2.5 });
    // 成品垛陈列位：候选依次为「靠站位对侧沿车道平齐 → 取货托盘那一行的西侧 →
    // 托盘正南」，要求同时在菱形内、不压任何静态占地、不压车斗走廊。
    var corr = laneBox(dockX, bayX, laneY);
    var band = patrolBand(f, field);
    var bandBox = { x0: -Infinity, x1: Infinity, y0: band.yN - WORKER_R - 1, y1: band.yS + WORKER_R + 1 };
    var cands = [];
    var opp = -side;
    for (var cd = TRUCK_HALF_L + PALLET_HALF + 2; cd <= hw * 0.85; cd += 2) {
      cands.push({ x: dockX + opp * cd, y: laneY });
    }
    for (var cd2 = PALLET_HALF + FORK_HALF + 2; cd2 <= hw * 0.85; cd2 += 2) {
      cands.push({ x: dockX - cd2, y: f.tray.y });
    }
    cands.push({ x: dockX - (PALLET_HALF + FORK_HALF + 2), y: f.tray.y + TRAY_HALF + PALLET_HALF + 2 });
    var spot = null, soft = null, lite = null, lastSpot = null;
    cands.forEach(function (c) {
      var fit = fitDiamond(f, c.x, c.y, PALLET_HALF, TRAY_HALF);
      var sb = { x0: fit.x - PALLET_HALF, x1: fit.x + PALLET_HALF,
                 y0: fit.y - TRAY_HALF, y1: fit.y + TRAY_HALF };
      var hits = function (b) {
        return !(sb.x1 <= b.x0 || sb.x0 >= b.x1 || sb.y1 <= b.y0 || sb.y0 >= b.y1);
      };
      var clean = !hits(corr) && rectFree(rects, sb);
      lastSpot = { x: fit.x, y: fit.y };
      if (clean && !hits(bandBox)) spot = spot || { x: fit.x, y: fit.y };
      else if (clean) soft = soft || { x: fit.x, y: fit.y };       // 干净但压了巡检带
      if (!lite && !hits(corr)) lite = lite || { x: fit.x, y: fit.y };   // 退守：在坪内且压不到车道
    });
    // 成品垛必须始终存在：优先两端都干净的位，其次只保证「在坪内 + 不压车斗走廊」
    f.palletSpot = spot || soft || lite || lastSpot || { x: f.x, y: f.y + f.halfH * 0.5 };
    // 成品垛回填封锁：巡检要绕开它
    rects.push({ kind: "pallet", x0: f.palletSpot.x - PALLET_HALF, x1: f.palletSpot.x + PALLET_HALF,
                 y0: f.palletSpot.y - TRAY_HALF, y1: f.palletSpot.y + TRAY_HALF });
    f.patrol = patrolRoute(f, field, laneY, dockX, rects);
    f.sheep = { x: f.patrol[0].x, y: f.patrol[0].y };

    // 值守点：闭环上最靠装货纵列的那个角（停机 / 出事时站那儿）
    var post = f.patrol[0];
    f.patrol.forEach(function (p) {
      if (Math.abs(p.x - dockX) < Math.abs(post.x - dockX)) post = p;
    });
    f.workerPost = { x: post.x, y: post.y };
    return f;
  }

  // ---------- 动态对象位姿（纯函数：同一输入恒得同一输出，便于测试） ----------
  function smoothstep(k) { return k * k * (3 - 2 * k); }

  /** 出车位姿：沿单轴直线（y = f.laneY）从坪外淡入点进场到装货位，装完沿原线
      倒车出场。位置只由 trip.t0 与各相时长决定，不读任何运行状态。
      heading 全程恒定（= 进场推进方向），倒车段以 reversing 标记——不做 180° 翻面，
      也就没有侧滑。 */
  function tripPose(trip, now) {
    var f = trip.factory;
    var dock = f.dock, wait = f.waitPt || dock, outPt = f.truckIn || f.apron;
    var laneY = f.laneY != null ? f.laneY : dock.y;
    var boxes = clamp(trip.boxes || 1, 1, 3);
    var t = Math.max(0, now - trip.t0);
    var dir = dock.x >= outPt.x ? 1 : -1;   // 进场推进方向（+x 为正）
    var out = { x: outPt.x, y: laneY, z0: YARD_Z, phase: "approach", k: 0,
                heading: dir, reversing: false, alpha: 0, moving: true,
                loaded: 0, boxes: boxes,
                bedDy: f.dockBed ? f.dockBed.y - laneY : 0 };
    function mx(a, b, k) { return a + (b - a) * k; }
    var acc = 0;
    for (var pi = 0; pi < TRIP_PHASES.length; pi += 1) {
      var ph = TRIP_PHASES[pi];
      var k = clamp((t - acc) / ph.ms, 0, 1);
      if (t < acc + ph.ms || pi === TRIP_PHASES.length - 1) {
        out.phase = ph.name;
        out.k = k;
        if (ph.name === "wait") {
          out.x = wait.x; out.moving = false;
        } else if (ph.name === "dock") {
          out.x = mx(wait.x, dock.x, 1 - (1 - k) * (1 - k));   // 缓出：进位减速
        } else if (ph.name === "load") {
          out.x = dock.x; out.moving = false;
          out.loaded = Math.min(boxes, Math.floor(k * boxes) + ((k * boxes) % 1 >= 0.86 ? 1 : 0));
        } else if (ph.name === "leave") {
          out.x = mx(dock.x, outPt.x, smoothstep(k));   // 沿原线倒车出场
          out.reversing = true;
        } else {   // approach：坪外淡入点 → 等候点
          out.x = mx(outPt.x, wait.x, smoothstep(k));
        }
        break;
      }
      acc += ph.ms;
    }
    // 全程首尾淡入淡出：车不该凭空出现或凭空消失
    var total = 0;
    TRIP_PHASES.forEach(function (p) { total += p.ms; });
    var gk = total ? clamp(t / total, 0, 1) : 1;
    out.alpha = Math.min(1, gk / 0.05) * Math.min(1, (1 - gk) / 0.08);
    return out;
  }

  /** 叉尖世界点（载货箱的锚点）：地面 (x, y) 只由车体位置与叉臂伸出量决定，
      z 是竖直抬升——升降绝不改地面 x/y（画的时候投影减 z，不拿升降当平移）。 */
  function forkTip(pose) {
    var up = pose.facing === "north" ? -1 : 1;
    return {
      x: pose.x,
      y: pose.y + up * FORK_REACH,
      z: YARD_Z + clamp(pose.lift || 0, 0, 1) * FORK_LIFT,
    };
  }

  /** 装货循环（纯函数）：叉车沿装货纵列（x = f.forkLaneX）往返，车头恒朝北——
      停靠位 → 正向开上取货位（叉尖压在托盘上）→ 取货（货叉竖直抬起）→ 开回车斗
      南侧卸下 → 倒车退回停靠位。货箱地面点 = forkTip 的地面分量：取货时正落在
      f.tray，卸货时正落在 f.dockBed（车斗上码的成箱用同一锚点）；升降只进 z。 */
  function forkliftCycle(f, ck) {
    var pickY = f.forkPick ? f.forkPick.y : f.tray.y + FORK_REACH;
    var dropY = f.dock.y + DROP_GAP;
    var parkY = f.forkPark ? f.forkPark.y : pickY;
    var pose = { x: f.forkLaneX, y: parkY, z0: YARD_Z, facing: "north", lift: 0,
                 carrying: false, cargo: null, phase: "park" };
    if (ck < 0.16) {                        // 停在停靠位
      pose.y = parkY;
    } else if (ck < 0.34) {                 // 正向开上取货位（车头朝北）
      pose.y = parkY + (pickY - parkY) * smoothstep((ck - 0.16) / 0.18);
      pose.phase = "approach";
    } else if (ck < 0.44) {                 // 取货：货叉托起（竖直抬升，不改地面位置）
      pose.y = pickY; pose.lift = smoothstep((ck - 0.34) / 0.10);
      pose.phase = "pick";
    } else if (ck < 0.80) {                 // 载货正向开回车斗
      pose.y = pickY + (dropY - pickY) * smoothstep((ck - 0.44) / 0.36);
      pose.lift = 1; pose.phase = "carry";
    } else if (ck < 0.88) {                 // 卸进车斗：货叉落下
      pose.y = dropY; pose.lift = 1 - smoothstep((ck - 0.80) / 0.08);
      pose.phase = "drop";
    } else {                                // 空车倒车退回停靠位
      pose.y = dropY + (parkY - dropY) * smoothstep((ck - 0.88) / 0.12);
      pose.phase = "return";
    }
    pose.carrying = pose.phase === "pick" || pose.phase === "carry"
      || (pose.phase === "drop" && pose.lift > 0.6);
    pose.cargo = pose.carrying ? forkTip(pose) : null;
    return pose;
  }

  /** 叉车位姿（装货相才有）：旧签名不变，非装货相返回 null。 */
  function forkliftPose(trip, now) {
    var head = tripPose(trip, now);
    if (head.phase !== "load") return null;
    var f = trip.factory;
    if (!f.dock || !f.tray) return null;
    var boxes = head.boxes;
    var cycle = Math.max(1, TRIP_LOAD_DUR / boxes);
    var lt = Math.max(0, now - trip.t0 - TRIP_LOAD_MS);
    return forkliftCycle(f, (lt % cycle) / cycle);
  }

  /** 叉车常驻位姿：装货相走完整往返，其余时间停在 f.forkPark（不凭空出现/消失）。
      同一 (f, now) 恒得同一结果。 */
  function forkliftDutyPose(f, now) {
    if (!f || !f.dock || !f.tray) return null;
    var trip = null;
    (S.trips || []).forEach(function (t) {
      if (trip || String(t.pid) !== String(f.id)) return;
      if (tripPose(t, now).phase === "load") trip = t;
    });
    if (trip) return forkliftPose(trip, now);
    var park = f.forkPark || { x: f.forkLaneX, y: f.tray.y + FORK_REACH };
    return { x: f.forkLaneX, y: park.y, z0: YARD_Z, facing: "north", lift: 0,
             carrying: false, cargo: null, phase: "park",
             factory: f, projectId: f.id };
  }

  /** 工头位姿：working 沿巡检环走并在每个路点停一小段；trouble/stopped 退到
      门房附近值守；idle 在原位待命；reduced 一律原位静止、不挥手。
      巡检相位由厂区 id 哈希决定，同一 (f, now) 输入恒得同一位置。 */
  function workerPose(f, now, reduced) {
    var anchor = f.sheep || { x: f.x - f.halfW * 0.45, y: f.y + f.halfH * 0.36 };
    var wave = !reduced && !!f.waveUntil && now < f.waveUntil;
    if (reduced) {
      return { x: anchor.x, y: anchor.y, state: "idle", wave: false, facing: 1 };
    }
    var mood = f.mood || "idle";
    if (mood === "trouble" || mood === "stopped") {
      var post = f.workerPost || f.gate || anchor;
      return {
        x: post.x, y: post.y, state: "gate", wave: wave,
        facing: post.x >= anchor.x ? 1 : -1,
      };
    }
    var pts = f.patrol;
    if (mood !== "working" || !pts || pts.length < 2) {
      return { x: anchor.x, y: anchor.y, state: "idle", wave: wave, facing: 1 };
    }
    // 环长折成时间轴：走路（按速度）+ 每个路点停留，循环推进
    var legs = [], cycle = 0, i;
    for (i = 0; i < pts.length; i += 1) {
      var a = pts[i], b = pts[(i + 1) % pts.length];
      var walkMs = Math.hypot(b.x - a.x, b.y - a.y) / PATROL_SPEED * 1000;
      legs.push({ a: a, b: b, walkMs: walkMs });
      cycle += walkMs + PATROL_PAUSE_MS;
    }
    var offset = Math.abs(hashKey("patrol" + f.id)) % cycle;
    var t = ((now + offset) % cycle + cycle) % cycle;
    var facing = 1, acc = 0;
    for (i = 0; i < legs.length; i += 1) {
      var leg = legs[i];
      if (leg.b.x !== leg.a.x) facing = leg.b.x > leg.a.x ? 1 : -1;
      if (t < acc + PATROL_PAUSE_MS) {
        return { x: leg.a.x, y: leg.a.y, state: "pause", wave: wave, facing: facing };
      }
      acc += PATROL_PAUSE_MS;
      if (t < acc + leg.walkMs) {
        var u = smoothstep((t - acc) / leg.walkMs);
        return {
          x: leg.a.x + (leg.b.x - leg.a.x) * u,
          y: leg.a.y + (leg.b.y - leg.a.y) * u,
          state: "patrol", wave: wave, facing: facing,
        };
      }
      acc += leg.walkMs;
    }
    return { x: pts[0].x, y: pts[0].y, state: "pause", wave: wave, facing: facing };
  }

  // ---------- FX 导出（主文件转发目标 + 绘制层绑定 + 测试探针入口） ----------
  FX.buildWorld = buildWorld; FX.buildIndustrialSite = buildIndustrialSite;
  FX.makePlateau = makePlateau; FX.gridSlots = gridSlots;
  FX.pathLength = pathLength; FX.pointAt = pointAt;
  FX.tripPose = tripPose; FX.forkliftPose = forkliftPose; FX.workerPose = workerPose;
  FX.forkliftDutyPose = forkliftDutyPose; FX.forkliftCycle = forkliftCycle;
  FX.forkTip = forkTip; FX.layoutFactory = layoutFactory; FX.machineField = machineField;
  FX.PATROL_SPEED = PATROL_SPEED; FX.PATROL_PAUSE_MS = PATROL_PAUSE_MS;
  FX.GRID_X = GRID_X; FX.GRID_Y = GRID_Y;
  FX.YARD_Z = YARD_Z; FX.YARD_K = YARD_K; FX.LANE_CLEAR = LANE_CLEAR;
  FX.TRUCK_HALF_W = TRUCK_HALF_W; FX.TRUCK_HALF_L = TRUCK_HALF_L;
  FX.FORK_HALF = FORK_HALF; FX.TRAY_HALF = TRAY_HALF; FX.PALLET_HALF = PALLET_HALF;
  FX.WORKER_R = WORKER_R; FX.MACHINE_HW = MACHINE_HW; FX.MACHINE_HH = MACHINE_HH;
  FX.DROP_GAP = DROP_GAP; FX.TRAY_GAP = TRAY_GAP; FX.GATE_DX = GATE_DX; FX.GATE_DY = GATE_DY;
  FX.FORK_REACH = FORK_REACH; FX.FORK_LIFT = FORK_LIFT;
  FX.insideDiamond = insideDiamond;
})();