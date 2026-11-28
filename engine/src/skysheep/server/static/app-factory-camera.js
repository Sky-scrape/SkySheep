/* ============================================================
 * 工厂相机与手势控制层：视口投影、取景模式、边界钳位、拖拽平移、
 * 光标锚定缩放、热区命中测试与悬停信息牌。
 *
 * 加载顺序：app-factory.js -> 本文件 -> app-factory-scene.js ->
 *           app-factory-drawer.js -> app-factory-draw.js。
 * 共享状态 S / dom 与纯辅助函数从 FX 读取；加载在后方的抽屉 / 绘制入口
 * 统一经 FX.xxx 运行时查表，避免与加载顺序产生隐式耦合。
 * ============================================================ */
(function () {
  "use strict";

  var FX = window.FX;
  var S = FX.S, dom = FX.dom;
  var clamp = FX.clamp, nowMs = FX.nowMs;
  var activeState = FX.activeState, factoryById = FX.factoryById;
  var findLine = FX.findLine, findRun = FX.findRun;
  var statFor = FX.statFor, lineStateText = FX.lineStateText;
  var BUFFER_MIN_W = FX.BUFFER_MIN_W || 320;
  var BUFFER_MIN_H = FX.BUFFER_MIN_H || 180;
  var CAM_EASE = FX.CAM_EASE || 9;
  var WAVE_MS = FX.WAVE_MS || 900;

  var DRAG_THRESHOLD = 5;
  var WHEEL_STEP = 1.15;
  var CAM_MAX_ZOOM = 2.8;

  var pointerPress = null;
  var suppressLabelClickUntil = 0;

  // ---------- 坐标变换（全工厂唯一事实来源） ----------
  function viewPoint(x, y) {
    var z = S.cam.zoom;
    return {
      x: Math.round((x - S.cam.x) * z + S.bufW / 2),
      y: Math.round((y - S.cam.y) * z + S.bufH / 2),
    };
  }

  function bufToWorld(bx, by) {
    var z = S.cam.zoom || 1;
    return {
      x: (bx - S.bufW / 2) / z + S.cam.x,
      y: (by - S.bufH / 2) / z + S.cam.y,
    };
  }

  function canvasRect() {
    if (dom.canvas && dom.canvas.getBoundingClientRect) {
      var r = dom.canvas.getBoundingClientRect();
      return {
        left: Number(r.left) || 0,
        top: Number(r.top) || 0,
        width: Math.max(1, r.width || S.cssW || S.bufW),
        height: Math.max(1, r.height || S.cssH || S.bufH),
      };
    }
    return { left: 0, top: 0, width: Math.max(1, S.cssW || S.bufW), height: Math.max(1, S.cssH || S.bufH) };
  }

  function canvasPoint(ev) {
    var rect = canvasRect();
    var bx = (ev.clientX - rect.left) * (S.bufW / rect.width);
    var by = (ev.clientY - rect.top) * (S.bufH / rect.height);
    return { x: bx, y: by, cx: ev.clientX - rect.left, cy: ev.clientY - rect.top };
  }

  // ---------- 相机边界与模式切换 ----------
  function syncCameraButtons() {
    if (dom.workBtn) dom.workBtn.setAttribute("aria-pressed", S.camMode === "work" ? "true" : "false");
    if (dom.centerBtn) dom.centerBtn.setAttribute("aria-pressed", S.camMode === "panorama" ? "true" : "false");
  }

  function worldBounds() {
    return (S.world && S.world.bounds) || { minX: -240, maxX: 240, minY: -160, maxY: 160 };
  }

  /** 计算给定缩放下的相机可移动包围盒：
   *  - 放大近看时至少留 ~35% 视口落在世界内，防止拖飞；
   *  - 南侧绝不把整座台地拖进上半屏而在底下露大片空沙地；
   *  - 缩放比全览小时保留适中余量以维持居中感并允许轻推。 */
  function cameraBounds(zoom) {
    var b = worldBounds();
    var z = Math.max(0.05, Number(zoom) || S.cam.zoom || 1);
    var hardMinX = b.minX + 20, hardMaxX = b.maxX - 20;
    var hardMinY = b.minY + 10, hardMaxY = b.maxY - 10;
    var cx = (b.minX + b.maxX) / 2;
    var cy = (b.minY + b.maxY) / 2;
    if (hardMinX > hardMaxX) hardMinX = hardMaxX = cx;
    if (hardMinY > hardMaxY) hardMinY = hardMaxY = cy;

    var halfVW = (S.bufW / 2) / z;
    var halfVH = (S.bufH / 2) / z;
    var worldW = Math.max(1, b.maxX - b.minX);
    var worldH = Math.max(1, b.maxY - b.minY);

    var minX, maxX;
    if (worldW > halfVW * 1.35) {
      minX = clamp(b.minX + halfVW * 0.55, hardMinX, cx);
      maxX = clamp(b.maxX - halfVW * 0.55, cx, hardMaxX);
    } else {
      var slackX = Math.max(36, worldW * 0.18);
      minX = Math.max(hardMinX, cx - slackX);
      maxX = Math.min(hardMaxX, cx + slackX);
    }
    if (minX > maxX) minX = maxX = clamp(cx, hardMinX, hardMaxX);

    var minY, maxY;
    var skyMarginY = (S.bufH * 0.11) / z;
    if (worldH > halfVH * 1.35) {
      minY = clamp(b.minY + halfVH * 0.45, hardMinY, cy);
      maxY = clamp(b.maxY - halfVH * 0.62, cy, hardMaxY);
    } else {
      var slackN = Math.max(24, Math.max(worldH * 0.16, skyMarginY));
      var slackS = Math.max(20, worldH * 0.10);
      minY = Math.max(hardMinY, cy - slackN);
      maxY = Math.min(hardMaxY, cy + slackS);
    }
    if (minY > maxY) minY = maxY = clamp(cy, hardMinY, hardMaxY);

    return { minX: minX, maxX: maxX, minY: minY, maxY: maxY };
  }

  function clampCameraTarget(tx, ty, zoom) {
    var cb = cameraBounds(zoom);
    return {
      x: clamp(tx, cb.minX, cb.maxX),
      y: clamp(ty, cb.minY, cb.maxY),
    };
  }

  function applyCamera(x, y, zoom, animate) {
    if (animate) {
      S.cam.tx = x;
      S.cam.ty = y;
      S.cam.tz = zoom;
      FX.updateLabels();
      return;
    }
    S.cam.x = S.cam.tx = x;
    S.cam.y = S.cam.ty = y;
    S.cam.zoom = S.cam.tz = zoom;
    S.camDirty = false;
    FX.render();
    if (!S.mounted) FX.updateLabels();
  }

  function stepCamera(dt) {
    var k = 1 - Math.exp(-dt * CAM_EASE);
    var moved = false;
    var nx = S.cam.x + (S.cam.tx - S.cam.x) * k;
    var ny = S.cam.y + (S.cam.ty - S.cam.y) * k;
    var nz = S.cam.zoom + (S.cam.tz - S.cam.zoom) * k;
    if (Math.abs(nx - S.cam.x) > 0.05 || Math.abs(ny - S.cam.y) > 0.05) moved = true;
    if (Math.abs(nz - S.cam.zoom) > 0.001) moved = true;
    S.cam.x = nx; S.cam.y = ny; S.cam.zoom = nz;
    if (Math.abs(S.cam.tx - S.cam.x) < 0.1) S.cam.x = S.cam.tx;
    if (Math.abs(S.cam.ty - S.cam.y) < 0.1) S.cam.y = S.cam.ty;
    if (Math.abs(S.cam.tz - S.cam.zoom) < 0.002) S.cam.zoom = S.cam.tz;
    return moved;
  }

  function requestCameraSync() {
    if (S.mounted && !S.reduced && S.raf) {
      S.camDirty = true;
      FX.updateLabels();
    } else {
      FX.render();
      if (!S.mounted) FX.updateLabels();
    }
  }

  function fitCamera(animate) {
    S.focusPid = null;   // 全景 = 退出聚焦档（Esc / 重开视图都回到这里）
    S.camMode = "panorama";
    syncCameraButtons();
    var w = S.bufW, h = S.bufH;
    // 全景 = 整个场景（台地 + 厂区坪 + 外圈设施）都进画幅，取景按 bounds。
    var b = (S.world && S.world.bounds) || { minX: -80, maxX: 80, minY: -50, maxY: 50 };
    var pad = 32;
    var z = Math.min((w - pad * 2) / Math.max(1, b.maxX - b.minX),
                     (h - pad * 2) / Math.max(1, b.maxY - b.minY));
    z = Math.max(0.01, Math.min(z, 1.45));
    S.fitZoom = z;
    var cx = (b.minX + b.maxX) / 2;
    // 天空占上方约 11% 封顶，余量上下均分且底部保 16px 安全边；机台名等
    // 「比全景更近才出现」的阈值仍按 fitZoom（窗口尺寸变化不改变全景语义）。
    var landOffset = Math.max(0, Math.min(h * 0.11,
      (h - (b.maxY - b.minY) * z) / 2 - 16));
    var cy = (b.minY + b.maxY) / 2 - landOffset / z;
    applyCamera(cx, cy, z, animate);
  }

  /** 作业区取景：紧框「调度厅 + 真实厂区坪」，让机台与包裹占比更大更清晰。
      外围远景自然退作画框边缘氛围；想一眼看全外圈设施可随时点「全景」。 */
  function fitWorkCamera(animate) {
    var fs = (S.world && S.world.factories) || [];
    if (!fs.length) {
      if (S.world) fitCamera(animate);
      else {
        S.focusPid = null;
        S.camMode = "work";
        syncCameraButtons();
      }
      return;
    }
    // 先算一遍全景 fitZoom 当底线参考，保证「比全景更近才显示机台名」语义稳定
    var b = S.world.bounds || { minX: -80, maxX: 80, minY: -50, maxY: 50 };
    var pZoom = Math.min((S.bufW - 64) / Math.max(1, b.maxX - b.minX),
                         (S.bufH - 64) / Math.max(1, b.maxY - b.minY));
    S.fitZoom = Math.max(0.01, Math.min(pZoom, 1.45));

    var minX = -56, maxX = 56, minY = -44, maxY = 44;
    fs.forEach(function (f) {
      minX = Math.min(minX, f.x - f.halfW - 22);
      maxX = Math.max(maxX, f.x + f.halfW + 22);
      minY = Math.min(minY, f.y - f.halfH - 42);
      maxY = Math.max(maxY, f.y + f.halfH + 22);
    });
    var pad = 24;
    var z = Math.min((S.bufW - pad * 2) / Math.max(1, maxX - minX),
                     (S.bufH - pad * 2) / Math.max(1, maxY - minY));
    z = clamp(z, S.fitZoom, 1.75);
    S.workZoom = z;
    S.focusPid = null;
    S.camMode = "work";
    syncCameraButtons();
    var cx = (minX + maxX) / 2;
    var cy = (minY + maxY) / 2 - 8 / z;
    applyCamera(cx, cy, z, animate);
  }

  /** 聚焦档：把镜头推进到某个厂区。 */
  function applyFocusCamera(animate) {
    var f = S.world && S.world.factories.find(function (x) { return x.id === S.focusPid; });
    if (!f) { S.focusPid = null; fitCamera(animate); return; }
    S.camMode = "focus";
    syncCameraButtons();
    var w = f.halfW * 2 + 90;
    var h = f.halfH * 2 + 150;
    var pad = 24;
    var z = Math.min((S.bufW - pad * 2) / Math.max(1, w),
                     (S.bufH - pad * 2) / Math.max(1, h));
    z = clamp(z, (S.fitZoom || 0.82) * 1.15, 2.6);
    var cx = f.x;
    var cy = f.y - 10 / z;   // 抬高一点，给机台上方的名牌留位置
    applyCamera(cx, cy, z, animate);
  }

  function focusProject(pid) {
    if (!S.world) return;
    var f = S.world.factories.find(function (x) { return x.id === pid; });
    if (!f) return;
    if (S.viewMode === "board") FX.setViewMode("scene");
    S.selectedKey = f.lines.length ? f.lines[0].key : null;
    S.focusPid = pid;
    applyFocusCamera(true);
    FX.renderPanel();   // 选中态要跟着变
  }

  // ---------- 场景尺寸 ----------
  function resizeScene(initial) {
    if (!dom.stage || !dom.canvas) return;
    // DOM 标签使用逻辑 CSS 像素；rect 已含全局界面缩放，混用会让名牌整体漂移。
    S.cssW = Math.max(1, dom.stage.clientWidth);
    S.cssH = Math.max(1, dom.stage.clientHeight);
    var bw = Math.max(BUFFER_MIN_W, Math.round(S.cssW / 2));
    var bh = Math.max(BUFFER_MIN_H, Math.round(S.cssH / 2));
    if (dom.canvas.width !== bw || dom.canvas.height !== bh) {
      dom.canvas.width = bw;
      dom.canvas.height = bh;
    }
    S.bufW = bw;
    S.bufH = bh;
    var ctx = dom.canvas.getContext("2d");
    if (ctx) ctx.imageSmoothingEnabled = false;
    // 聚焦档 / 作业区档跟着窗口尺寸重算取景，不能被一次 resize 拉回全景
    if (!S.world) {
      syncCameraButtons();
      return;
    }
    if (S.focusPid) applyFocusCamera(false);
    else if (S.camMode === "work" && S.world.factories && S.world.factories.length) fitWorkCamera(false);
    else if (S.camMode === "custom") {
      var c = clampCameraTarget(S.cam.x, S.cam.y, S.cam.zoom);
      applyCamera(c.x, c.y, S.cam.zoom, false);
    } else fitCamera(false);
  }

  // ---------- 悬停提示与热区判定 ----------
  /** 悬停的轻量中文提示：只说画面上真实存在的状态，不虚构关联。
      机台 / 车 / 叉车 / 羊 / 返工区 / 托盘 / 调度厅同时会画一圈高亮（见绘制层），提示文字写回 canvas.title。 */
  function hoverTitle(hit) {
    if (!hit) return "";
    if (hit.type === "pkg") {
      var run = findRun(hit.id);
      return run ? ("包裹 #" + run.id + "  " + (run.name || "")) : "包裹";
    }
    if (hit.type === "line") {
      var rec = findLine(hit.key);
      if (!rec) return "";
      var modelText = rec.line.provider_name
        ? "  · " + (rec.line.provider_label || rec.line.provider_name) +
          (rec.line.model ? " / " + rec.line.model : "")
        : "";
      return "机台  " + (rec.line.name || rec.key) + "  " +
        lineStateText(rec.line) + modelText;
    }
    if (hit.type === "hall") {
      var c = (activeState() && activeState().counters) || {};
      return "调度厅  在跑 " + (Number(c.running) || 0) + " · 排队 " + (Number(c.queued) || 0) + "（点击看全局调度）";
    }
    if (hit.type === "rework") {
      var rstat = statFor(hit.pid);
      return rstat.rework > 0
        ? ("返工区  今日异常 " + rstat.rework + " 次（点击排查处置）")
        : "返工区  今日零返工（点击看记录）";
    }
    if (hit.type === "pallet") {
      var pstat = statFor(hit.pid);
      return "成品垛  今日做成 " + (pstat.done || 0) + " 件（点击看产出）";
    }
    if (hit.type === "yard") {
      var yf = factoryById(hit.pid);
      return yf ? ("厂区  " + yf.name + "（点击聚焦查看；滚轮可放大、拖动可平移）") : "";
    }
    if (hit.type === "upcoming") {
      var boardView = S.noticeboardView;
      var n = (boardView && boardView.count) || 0;
      return n ? ("待发告示牌  " + n + " 项（点击查看）")
               : "待发告示牌  暂无待发（点击查看）";
    }
    if (hit.type === "queued") {
      var recQ = findLine(hit.key);
      return recQ ? ("排队  " + (recQ.line.name || recQ.key)) : "";
    }
    if (hit.type === "truck") {
      var trip = null;
      S.trips.forEach(function (t) { if (String(t.pid) === String(hit.pid)) trip = t; });
      var phase = trip ? FX.tripPose(trip, nowMs()).phase : "";
      return "出货卡车  " + ({ approach: "正在进场", wait: "等候装货", dock: "停靠装货位",
        load: "装货中", leave: "离场" }[phase] || "在场内");
    }
    if (hit.type === "forklift") return "叉车  托盘与车斗之间搬货";
    if (hit.type === "sheep") {
      var f = factoryById(hit.pid);
      if (!f) return "";
      var pose = FX.workerPose(f, nowMs(), S.reduced);
      var what = { patrol: "巡检中", pause: "巡检停留", gate: "在门口值守", idle: "待命" }[pose.state] || "在场";
      return "工头小羊  " + what + (S.reduced ? "" : "（点一下挥手）");
    }
    return "";
  }

  function showHoverTip(hit, ev) {
    if (!dom.tip || !dom.stage) return;
    var text = hoverTitle(hit);
    if (!text) {
      hideHoverTip();
      return;
    }
    var rect = dom.stage.getBoundingClientRect();
    var width = dom.stage.clientWidth || rect.width;
    var height = dom.stage.clientHeight || rect.height;
    var px = (ev.clientX - rect.left) * width / (rect.width || width);
    var py = (ev.clientY - rect.top) * height / (rect.height || height);
    var x = px + 14;
    var y = py + 18;
    dom.tip.textContent = text;
    dom.tip.style.display = "";
    var w = dom.tip.offsetWidth || 120;
    var h = dom.tip.offsetHeight || 24;
    if (x + w > width - 8) x = Math.max(4, px - w - 12);
    if (y + h > height - 8) y = Math.max(4, py - h - 10);
    dom.tip.style.left = Math.round(x) + "px";
    dom.tip.style.top = Math.round(y) + "px";
  }

  function hideHoverTip() {
    if (dom.tip && dom.tip.style.display !== "none") dom.tip.style.display = "none";
  }

  function hitTest(x, y) {
    // 包裹（小目标）与告示牌优先：在跑的、排队的、牌面与动态对象（车 / 叉车 / 羊 / 返工 / 托盘 / 调度厅）
    // 都是小目标，一起判——它们必须压过脚下的机台与厂区坪热区
    for (var i = S.hits.length - 1; i >= 0; i -= 1) {
      var h = S.hits[i];
      if (h.rw != null) {
        if (h.type === "upcoming" && Math.abs(x - h.x) <= h.rw
            && Math.abs(y - h.y) <= h.rh) return h;
        continue;
      }
      if ((h.type === "pkg" || h.type === "upcoming" || h.type === "queued"
           || h.type === "sheep" || h.type === "truck" || h.type === "forklift"
           || h.type === "rework" || h.type === "pallet" || h.type === "hall")
          && Math.hypot(x - h.x, y - h.y) <= h.r) return h;
    }
    if (!S.world) return null;
    var best = null;
    S.world.lines.forEach(function (rec) {
      if (!rec.drawn) return;
      var p = viewPoint(rec.x, rec.y);
      var r = Math.max(8, 14 * S.cam.zoom);
      if (Math.abs(x - p.x) <= r && y >= p.y - r * 1.6 && y <= p.y + r) {
        best = { type: "line", key: rec.key };
      }
    });
    if (best) return best;
    // 厂区菱形坪兜底：点在任意厂区空地/建筑上可以直接聚焦进去看细节
    var wp = bufToWorld(x, y);
    for (var fi = S.world.factories.length - 1; fi >= 0; fi -= 1) {
      var f = S.world.factories[fi];
      if (Math.abs(wp.x - f.x) / f.halfW + Math.abs(wp.y - f.y) / f.halfH <= 1.0) {
        return { type: "yard", pid: f.id };
      }
    }
    return null;
  }

  function activateLabel(labelEl) {
    if (!labelEl || !labelEl.dataset) return false;
    if (labelEl.dataset.focusPid != null && labelEl.dataset.focusPid !== "") {
      focusProject(Number(labelEl.dataset.focusPid));
      return true;
    }
    if (labelEl.dataset.openHall) {
      FX.openDispatchDrawer();
      return true;
    }
    if (labelEl.dataset.openLine) {
      FX.openLineDrawer(labelEl.dataset.openLine, { focus: true });
      return true;
    }
    return false;
  }

  // ---------- 指针与滚轮手势 ----------
  function onPointerDown(ev) {
    if (!dom.canvas || (ev.button != null && ev.button !== 0)) return;
    var labelEl = ev.target && ev.target.closest
      ? ev.target.closest("[data-focus-pid], [data-open-hall], [data-open-line]")
      : null;
    pointerPress = {
      x: ev.clientX,
      y: ev.clientY,
      lastX: ev.clientX,
      lastY: ev.clientY,
      startCamX: S.cam.x,
      startCamY: S.cam.y,
      moved: 0,
      dragging: false,
      id: ev.pointerId,
      labelEl: labelEl,
    };
    // 按下瞬间冻结在当前视觉相机，避免和切景缓动相互拉扯
    S.cam.tx = S.cam.x;
    S.cam.ty = S.cam.y;
    S.cam.tz = S.cam.zoom;
    if (!labelEl && dom.canvas.setPointerCapture && ev.pointerId != null) {
      try { dom.canvas.setPointerCapture(ev.pointerId); } catch (e) { /* 合成事件无活跃指针 */ }
    }
  }

  function onPointerMove(ev) {
    if (!dom.canvas) return;
    if (pointerPress) {
      if (ev.pointerId != null && pointerPress.id != null && ev.pointerId !== pointerPress.id) return;
      var dx = ev.clientX - pointerPress.x;
      var dy = ev.clientY - pointerPress.y;
      pointerPress.moved = Math.max(pointerPress.moved, Math.abs(dx) + Math.abs(dy));
      pointerPress.lastX = ev.clientX;
      pointerPress.lastY = ev.clientY;
      if (pointerPress.moved > DRAG_THRESHOLD && S.world) {
        if (!pointerPress.dragging) {
          pointerPress.dragging = true;
          S.hover = null;
          hideHoverTip();
          if (dom.canvas.title) dom.canvas.title = "";
          if (dom.canvas.setPointerCapture && pointerPress.id != null) {
            try { dom.canvas.setPointerCapture(pointerPress.id); } catch (e) { /* 忽略 */ }
          }
        }
        var rect = canvasRect();
        var scaleX = rect.width / S.bufW;
        var scaleY = rect.height / S.bufH;
        var z = S.cam.zoom || 1;
        var targetX = pointerPress.startCamX - dx / (scaleX * z);
        var targetY = pointerPress.startCamY - dy / (scaleY * z);
        var clamped = clampCameraTarget(targetX, targetY, z);
        // 若撞到边界后立刻回拖，重锚原点，保证反向首像素即刻跟手
        if (clamped.x !== targetX) pointerPress.startCamX = clamped.x + dx / (scaleX * z);
        if (clamped.y !== targetY) pointerPress.startCamY = clamped.y + dy / (scaleY * z);
        S.cam.x = S.cam.tx = clamped.x;
        S.cam.y = S.cam.ty = clamped.y;
        S.focusPid = null;
        S.camMode = "custom";
        syncCameraButtons();
        requestCameraSync();
        if (dom.canvas.style) dom.canvas.style.cursor = "grabbing";
      }
      return;
    }
    // 非拖拽态才更新悬停提示与高亮
    S.hoverPointer = { clientX: ev.clientX, clientY: ev.clientY };
    var pt = canvasPoint(ev);
    var hit = hitTest(pt.x, pt.y);
    var prev = S.hover;
    S.hover = hit || null;
    dom.canvas.title = hoverTitle(hit);
    if (dom.canvas.style) {
      dom.canvas.style.cursor = hit ? "pointer" : "grab";
    }
    showHoverTip(hit, ev);
    if (S.reduced && (S.hover || prev) &&
        (String(S.hover && S.hover.type) !== String(prev && prev.type) ||
         String(S.hover && S.hover.key) !== String(prev && prev.key) ||
         String(S.hover && S.hover.pid) !== String(prev && prev.pid))) {
      FX.render();
    }
  }

  function onPointerUp(ev) {
    if (!dom.canvas || !pointerPress) return;
    if (ev.pointerId != null && pointerPress.id != null && ev.pointerId !== pointerPress.id) return;
    var moved = Math.max(pointerPress.moved,
      Math.abs(ev.clientX - pointerPress.x) + Math.abs(ev.clientY - pointerPress.y));
    var labelEl = pointerPress.labelEl;
    if (dom.canvas.releasePointerCapture && pointerPress.id != null) {
      try {
        if (!dom.canvas.hasPointerCapture || dom.canvas.hasPointerCapture(pointerPress.id)) {
          dom.canvas.releasePointerCapture(pointerPress.id);
        }
      } catch (e) { /* 忽略 */ }
    }
    pointerPress = null;
    if (dom.canvas.style) dom.canvas.style.cursor = "grab";
    if (moved > DRAG_THRESHOLD) {
      suppressLabelClickUntil = nowMs() + 250;
      refreshHover();
      return;
    }
    if (labelEl) {
      suppressLabelClickUntil = nowMs() + 250;
      activateLabel(labelEl);
      return;
    }
    var pt = canvasPoint(ev);
    var hit = hitTest(pt.x, pt.y);
    if (hit && hit.type === "pkg") {
      FX.openRunDrawer(hit.id);
      return;
    }
    if (hit && hit.type === "line") {
      FX.openLineDrawer(hit.key, { focus: false });
      return;
    }
    if (hit && hit.type === "hall") {
      FX.openDispatchDrawer();
      return;
    }
    if (hit && hit.type === "rework") {
      FX.openReworkDrawer(hit.pid);
      return;
    }
    if (hit && hit.type === "pallet") {
      FX.openAllRunsDrawer({ project_id: hit.pid, view: "done_today" });
      return;
    }
    if (hit && hit.type === "yard") {
      focusProject(hit.pid);
      return;
    }
    // 点告示牌 = 打开待发信息弹窗（空态也开，只报「暂无待发」）
    if (hit && hit.type === "upcoming") {
      FX.openUpcomingDialog();
      return;
    }
    // 点排队包裹 = 看这条产线为什么卡着（抽屉里有状态与排期），同样不派工
    if (hit && hit.type === "queued" && hit.key) {
      FX.openLineDrawer(hit.key, { focus: true });
      return;
    }
    // 点工头小羊：短挥手一下（纯表现；减少动态效果时不挥手）
    if (hit && hit.type === "sheep" && !S.reduced) {
      var f = factoryById(hit.pid);
      if (f) {
        f.waveUntil = nowMs() + WAVE_MS;
        FX.render();
      }
    }
  }

  function refreshHover() {
    if (!S.hoverPointer || !dom.canvas || pointerPress) return;
    var pt = canvasPoint(S.hoverPointer);
    S.hover = hitTest(pt.x, pt.y) || null;
    dom.canvas.title = hoverTitle(S.hover);
    showHoverTip(S.hover, S.hoverPointer);
  }

  function cancelDrag() {
    if (!pointerPress) return;
    if (dom.canvas && dom.canvas.releasePointerCapture && pointerPress.id != null) {
      try {
        if (!dom.canvas.hasPointerCapture || dom.canvas.hasPointerCapture(pointerPress.id)) {
          dom.canvas.releasePointerCapture(pointerPress.id);
        }
      } catch (e) { /* 忽略 */ }
    }
    pointerPress = null;
    if (dom.canvas && dom.canvas.style) dom.canvas.style.cursor = "grab";
  }

  function onPointerLeave() {
    if (pointerPress && pointerPress.dragging) return;
    var hadHover = !!S.hover;
    S.hover = null;
    S.hoverPointer = null;
    hideHoverTip();
    if (dom.canvas) {
      dom.canvas.title = "";
      if (dom.canvas.style) dom.canvas.style.cursor = "default";
    }
    pointerPress = null;
    if (hadHover && S.reduced) FX.render();
  }

  function onWheel(ev) {
    ev.preventDefault();
    if (!S.world) return;
    var delta = Number(ev.deltaY) || 0;
    if (!delta) return;
    var minZ = Math.max(0.05, Math.min(S.fitZoom || 0.4, 0.38) * 0.85);
    var oldZ = S.cam.zoom || 1;
    var factor = delta < 0 ? WHEEL_STEP : (1 / WHEEL_STEP);
    var nextZ = clamp(oldZ * factor, minZ, CAM_MAX_ZOOM);
    if (Math.abs(nextZ - oldZ) < 1e-4) return;

    // 锚定鼠标在当前真实视觉矩阵下的世界坐标，保证缩放不飘
    var tx = S.cam.x;
    var ty = S.cam.y;
    if (dom.canvas && dom.canvas.getBoundingClientRect && ev.clientX != null) {
      var pt = canvasPoint(ev);
      var wx = (pt.x - S.bufW / 2) / oldZ + tx;
      var wy = (pt.y - S.bufH / 2) / oldZ + ty;
      tx = wx - (pt.x - S.bufW / 2) / nextZ;
      ty = wy - (pt.y - S.bufH / 2) / nextZ;
    }
    var clamped = clampCameraTarget(tx, ty, nextZ);
    S.cam.zoom = S.cam.tz = nextZ;
    S.cam.x = S.cam.tx = clamped.x;
    S.cam.y = S.cam.ty = clamped.y;
    if (pointerPress && pointerPress.dragging) {
      pointerPress.startCamX = clamped.x;
      pointerPress.startCamY = clamped.y;
      pointerPress.x = pointerPress.lastX != null ? pointerPress.lastX : pointerPress.x;
      pointerPress.y = pointerPress.lastY != null ? pointerPress.lastY : pointerPress.y;
    }
    S.focusPid = null;
    S.camMode = "custom";
    syncCameraButtons();
    requestCameraSync();
  }

  function bindCameraEvents() {
    if (dom.labels) {
      // 从可点名牌上按住拖动时同样可平滑平移，只有未移动的轻点才触发名牌操作
      dom.labels.addEventListener("pointerdown", function (ev) {
        if (ev.target && ev.target.closest("[data-focus-pid], [data-open-hall], [data-open-line]")) {
          onPointerDown(ev);
        }
      });
      dom.labels.addEventListener("click", function (ev) {
        if (nowMs() < suppressLabelClickUntil) return;
        var el = ev.target && ev.target.closest("[data-focus-pid], [data-open-hall], [data-open-line]");
        if (el) activateLabel(el);
      });
    }
    if (dom.canvas) {
      dom.canvas.addEventListener("pointerdown", onPointerDown);
      dom.canvas.addEventListener("pointermove", onPointerMove);
      dom.canvas.addEventListener("pointerup", onPointerUp);
      dom.canvas.addEventListener("pointercancel", cancelDrag);
      dom.canvas.addEventListener("lostpointercapture", cancelDrag);
      dom.canvas.addEventListener("pointerleave", onPointerLeave);
      dom.canvas.addEventListener("wheel", onWheel, { passive: false });
      dom.canvas.addEventListener("dblclick", function () {
        if (S.focusPid || S.camMode === "custom") fitWorkCamera(true);
        else if (S.camMode === "work") fitCamera(true);
        else fitWorkCamera(true);
      });
    }
    window.addEventListener("blur", cancelDrag);
  }

  // ---------- FX 导出 ----------
  FX.viewPoint = viewPoint;
  FX.bufToWorld = bufToWorld;
  FX.cameraBounds = cameraBounds;
  FX.clampCameraTarget = clampCameraTarget;
  FX.syncCameraButtons = syncCameraButtons;
  FX.stepCamera = stepCamera;
  FX.fitCamera = fitCamera;
  FX.fitWorkCamera = fitWorkCamera;
  FX.applyFocusCamera = applyFocusCamera;
  FX.focusProject = focusProject;
  FX.resizeScene = resizeScene;
  FX.hoverTitle = hoverTitle;
  FX.hideHoverTip = hideHoverTip;
  FX.hitTest = hitTest;
  FX.refreshHover = refreshHover;
  FX.onPointerDown = onPointerDown;
  FX.onPointerMove = onPointerMove;
  FX.onPointerUp = onPointerUp;
  FX.onPointerLeave = onPointerLeave;
  FX.onWheel = onWheel;
  FX.bindCameraEvents = bindCameraEvents;
})();
