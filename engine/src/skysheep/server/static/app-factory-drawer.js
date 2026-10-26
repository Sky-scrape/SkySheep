/* ============================================================
 * 工厂抽屉层：运行 / 产线 / 队列 / 需留意 / 设置等详情抽屉与产线操作
 * （自 app-factory.js 拆出，零行为变化）。
 *
 * 加载顺序：app-factory.js -> app-factory-scene.js -> 本文件 -> app-factory-draw.js。
 * 主文件的状态、常量与业务函数经 window.FX 顶部绑定（均已就位）；
 * 绘制层真身（render）经 FX 运行时查表。escapeHtml / request 等是 app.js 全局。
 * ============================================================ */
(function () {
  "use strict";

  var FX = window.FX;
  var S = FX.S, dom = FX.dom;
  var RUN_STATUS = FX.RUN_STATUS, RUN_OUTCOME = FX.RUN_OUTCOME,
      ERROR_KIND = FX.ERROR_KIND, TRIGGER_TEXT = FX.TRIGGER_TEXT;
  var activeState = FX.activeState, applyNavVisibility = FX.applyNavVisibility,
      factoryReload = FX.factoryReload, findLine = FX.findLine, findRun = FX.findRun,
      fitCamera = FX.fitCamera, fmtDur = FX.fmtDur, fmtTokens = FX.fmtTokens,
      fmtTs = FX.fmtTs, freshProgress = FX.freshProgress, hasRealLines = FX.hasRealLines,
      lineStateText = FX.lineStateText, liveStale = FX.liveStale, nowMs = FX.nowMs,
      refreshPanelStates = FX.refreshPanelStates, refreshWeather = FX.refreshWeather,
      renderAll = FX.renderAll, renderAttentionNotice = FX.renderAttentionNotice,
      renderPanel = FX.renderPanel, renderWeatherChip = FX.renderWeatherChip,
      runElapsed = FX.runElapsed, scheduleResync = FX.scheduleResync,
      lineKeyFor = FX.lineKeyFor, stopReplay = FX.stopReplay,
      fmtCountdown = FX.fmtCountdown, refreshUpcoming = FX.refreshUpcoming;

  // ---------- 抽屉 ----------
  /** 被拦工具的快照（后端存 JSON；旧行 / 坏数据回落原文），给抽屉一句人话。 */
  function deniedToolsText(run) {
    var raw = String((run && run.denied_tools) || "");
    if (!raw) return "";
    try {
      var obj = JSON.parse(raw);
      if (obj && typeof obj === "object" && !Array.isArray(obj)) {
        return Object.keys(obj).map(function (name) {
          return name + " ×" + Number(obj[name] || 0);
        }).join(" / ");
      }
    } catch (e) { /* 不是 JSON：按原文显示 */ }
    return raw;
  }

  /** 单次运行的 token 用量（未记录时不占位，避免一排「0 in / 0 out」）。 */
  function tokenRow(run) {
    var tin = Number(run && run.in_tokens) || 0;
    var tout = Number(run && run.out_tokens) || 0;
    if (tin <= 0 && tout <= 0) return "";
    var cached = Number(run && run.cached_tokens) || 0;
    var text = fmtTokens(tin) + " in / " + fmtTokens(tout) + " out";
    if (cached > 0) text += "（缓存 " + fmtTokens(cached) + "）";
    return "<dt>用量</dt><dd>" + escapeHtml(text) + "</dd>";
  }

  /** 产线体检：近 7 天成功率 / 耗时 / 连续失败 + 最近 N 次状态色条。 */
  function healthBlock(line) {
    var h = line && line.health;
    if (!h || !h.total) {
      return '<div><div class="dim small">产线体检</div>' +
        '<div class="fx-hint">近 7 天还没有可统计的运行。</div></div>';
    }
    var rate = Math.round((h.success_rate || 0) * 100);
    var parts = [
      "成功率 <b>" + rate + "%</b>（7 天 " + h.total + " 次）",
      "平均 " + fmtDur(h.avg_s),
      "P95 " + fmtDur(h.p95_s),
      "最慢 " + fmtDur(h.max_s),
    ];
    // 「跑完了但被门控拦过」与「用户判定不算」单独说清：它们不拉低状态色条，
    // 但必须拉低成功率，否则这个百分比会一直是绿的
    if (h.partial) parts.push('<span class="fx-warn">有拦截 ' + h.partial + " 次</span>");
    if (h.marked_failed) parts.push('<span class="fx-bad">判定不算 ' + h.marked_failed + " 次</span>");
    if (h.streak >= 2) parts.push('<span class="fx-bad">连续失败 ' + h.streak + " 次</span>");
    var tail = h.tail || [];
    var tailOutcomes = h.tail_outcomes || [];
    var strip = tail.map(function (s, idx) {
      var oc = tailOutcomes[idx] || "";
      // done 但有拦截的格子不能染成成功绿：它是 partial，不是做成
      var cls = (s === "done" && oc === "partial") ? "partial" : s;
      var tip = (RUN_STATUS[s] || s) + (oc ? " · " + (RUN_OUTCOME[oc] || oc) : "");
      return '<i class="' + escapeHtml(cls) + '" title="' + escapeHtml(tip) + '"></i>';
    }).join("");
    return '<div><div class="dim small">产线体检</div>' +
      '<div class="fx-health">' + parts.join(" · ") + "</div>" +
      '<div class="fx-strip" title="最近 ' + tail.length + ' 次运行（新 → 旧）">' + strip + "</div></div>";
  }

  function drawerShell(title, bodyHtml) {
    return '<div class="fx-drawer-head"><h3>' + title + "</h3>" +
      '<button class="fx-drawer-close" title="关闭（Esc）">✕</button></div>' +
      '<div class="fx-drawer-body">' + bodyHtml + "</div>";
  }

  function openDrawer(html) {
    if (!dom.drawer) return;
    var wasHidden = dom.drawer.classList.contains("hidden");
    dom.drawer.innerHTML = html;
    if (wasHidden) dom.drawer.classList.remove("factory-entered");
    dom.drawer.classList.remove("hidden");
    if (wasHidden) {
      void dom.drawer.offsetWidth;
      dom.drawer.classList.add("factory-entered");
    }
    var close = dom.drawer.querySelector(".fx-drawer-close");
    if (close) {
      close.onclick = closeDrawer;
      // 键盘用户从面板 / 画布打开抽屉后，焦点要跟着进来（Esc 关闭、Tab 从关闭键开始）；
      // 程序化刷新（事件到达 / live 轮询）不抢焦点。
      if (wasHidden) close.focus();
    }
  }

  function closeDrawer() {
    S.drawer = null;
    if (dom.drawer) {
      dom.drawer.classList.add("hidden");
      dom.drawer.innerHTML = "";
    }
  }

  function factoryEscape() {
    if (S.drawer) { closeDrawer(); return true; }
    // 回放中：Esc 先回到此刻（它比「退出聚焦」更该先关：回放是一个临时态）
    if (S.replay && S.replay.running) { stopReplay(); return true; }
    // 聚焦档里 Esc 先回全景；工具栏的「全景」按钮是同一出口（键盘 / 鼠标都行）
    if (S.focusPid) { fitCamera(true); return true; }
    return false;
  }

  function openRunDrawer(id) {
    var run = findRun(id);
    if (!run) return;
    S.drawer = { type: "run", id: id };
    var status = run.status || "";
    var denied = Number(run.denied_count) || 0;
    var deniedTools = deniedToolsText(run);
    var outcome = run.outcome || "";
    var html = '<div class="fx-kv">' +
      "<dt>包裹</dt><dd>#" + escapeHtml(String(run.id)) + "</dd>" +
      "<dt>产线</dt><dd>" + escapeHtml(runTitle(run)) + "</dd>" +
      "<dt>厂区</dt><dd>" + escapeHtml(run.project_name || "—") + "</dd>" +
      "<dt>触发</dt><dd>" + escapeHtml(run.kind === "pipeline_node" && run.trigger === "schedule"
        ? "编排自动推进" : (TRIGGER_TEXT[run.trigger] || run.trigger || "—")) + "</dd>" +
      "<dt>状态</dt><dd><span class=\"fx-status " + escapeHtml(status) + "\">" +
        escapeHtml(RUN_STATUS[status] || status || "—") + "</span>" +
        (outcome
          ? " " + outcomeChip(run) + '<span class="fx-run-meta">' +
            (run.outcome_source === "user" ? "（你判定的）" : "（自动判定）") + "</span>"
          : "") + "</dd>" +
      "<dt>开始</dt><dd>" + escapeHtml(fmtTs(run.started_at)) + "</dd>" +
      "<dt>结束</dt><dd>" + escapeHtml(fmtTs(run.finished_at)) + "</dd>" +
      "<dt>耗时</dt><dd>" + escapeHtml(fmtDur(runElapsed(run))) + "</dd>" +
      (denied > 0
        ? "<dt>被拦</dt><dd class=\"fx-bad\">" + denied + " 次" +
          (deniedTools ? "：" + escapeHtml(deniedTools) : "") +
          "<br><span class=\"fx-run-meta\">无人值守门控拒绝了写入 / 执行类工具，" +
          (run.kind === "cron" ? "产出可能只是模型的绕行说明；点下方「去编辑」核对预授权工具"
            : "产出可能只是模型的绕行说明；点下方「查看编排」核对节点预授权，修改可在对话中交给 Agent 处理") + "</span></dd>"
        : "") +
      tokenRow(run) +
      (status === "running"
        ? "<dt>在跑</dt><dd id=\"fx-run-progress\">" +
          escapeHtml(progressText(run.progress) || "启动中") + "</dd>"
        : "") +
      "</div>";
    if (run.error) {
      var kind = run.error_kind ? (ERROR_KIND[run.error_kind] || run.error_kind) : "";
      html += "<div><div class=\"dim small\">错误" +
        (kind ? " · <span class=\"fx-bad\">" + escapeHtml(kind) + "</span>" : "") +
        "</div><pre class=\"fx-excerpt\">" + escapeHtml(run.error) + "</pre></div>";
    }
    if (run.result_excerpt) {
      html += "<div><div class=\"dim small\">结果摘要</div><pre class=\"fx-excerpt\">" +
        escapeHtml(run.result_excerpt) + "</pre></div>";
    }
    html += '<div class="fx-drawer-ops">';
    if (run.session_id) {
      html += '<button class="btn-primary" data-open-session="' + escapeHtml(String(run.session_id)) + '">打开会话</button>';
    }
    if (run.result_path) {
      html += '<button class="btn-ghost" data-open-result="' + escapeHtml(String(run.result_path)) +
        '" title="打开这个工位的产出全文">打开产出</button>';
    }
    if (status !== "running") {
      // 人工判定与自动判定分开记：跑完了不等于做成了，只有用户知道产出对不对
      html += '<button class="btn-ghost" data-mark="ok">这轮算过</button>';
      html += '<button class="btn-ghost" data-mark="failed">不算</button>';
      if (run.outcome_source === "user") {
        html += '<button class="btn-ghost" data-mark="">撤销判定</button>';
      }
    }
    if (run.status === "running" && run.kind === "cron") {
      html += '<button class="btn-ghost fx-danger" data-cancel-task="' +
        escapeHtml(String(run.task_id)) + '">叫停</button>';
    }
    if (run.kind === "cron" && run.project_id != null) {
      html += '<button class="btn-ghost" data-edit-task="' + escapeHtml(String(run.task_id)) +
        '" data-edit-project="' + escapeHtml(String(run.project_id)) +
        '" title="切到该厂区并打开自动化面板，直接改提示词 / 补预授权工具">去编辑</button>';
    }
    if (run.kind === "pipeline_node" && run.pipeline_id && run.project_id != null) {
      html += '<button class="btn-ghost" data-view-pipeline="1">查看编排</button>';
    }
    html += '<button class="btn-ghost" data-close-drawer="1">关闭</button></div>';
    openDrawer(drawerShell("运行详情", html));
    if (!dom.drawer) return;
    var pipeBtn = dom.drawer.querySelector("[data-view-pipeline]");
    if (pipeBtn) pipeBtn.onclick = function () {
      closeDrawer();
      openPipelineFor(run.project_id, run.pipeline_id, run.task_id);
    };
    var cancelBtn = dom.drawer.querySelector("[data-cancel-task]");
    if (cancelBtn) {
      cancelBtn.onclick = function () {
        cancelRun(Number(cancelBtn.dataset.cancelTask), run.name, cancelBtn);
      };
    }
    var resultBtn = dom.drawer.querySelector("[data-open-result]");
    if (resultBtn) {
      resultBtn.onclick = function () { openFactoryResult(resultBtn.dataset.openResult); };
    }
    var editBtn = dom.drawer.querySelector("[data-edit-task]");
    if (editBtn) {
      editBtn.onclick = function () {
        closeDrawer();
        if (typeof openAutomationFor === "function") {
          openAutomationFor(Number(editBtn.dataset.editProject),
                            Number(editBtn.dataset.editTask));
        }
      };
    }
    dom.drawer.querySelectorAll("[data-mark]").forEach(function (b) {
      b.onclick = function () { markRunOutcome(run, b.dataset.mark, b); };
    });
    var openBtn = dom.drawer.querySelector("[data-open-session]");
    if (openBtn) {
      openBtn.onclick = function () {
        // 会话 id 是 12 位十六进制串（uuid 前缀），不是数字——以前 Number() 一下
        // 变成 NaN，工厂里「打开会话」其实一直是坏的
        var sid = String(openBtn.dataset.openSession || "");
        closeDrawer();
        if (typeof backToChat === "function") backToChat();
        if (typeof openTabForSession === "function") openTabForSession(sid, "");
      };
    }
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;
  }

  function openLineDrawer(key, opts) {
    var rec = findLine(key);
    if (!rec) return;
    opts = opts || {};
    var line = rec.line;
    S.selectedKey = key;
    renderPanel();
    S.drawer = { type: "line", key: key };
    var next = line.next_run_at ? fmtTs(line.next_run_at) : "—";
    var html = '<div class="fx-kv">' +
      "<dt>厂区</dt><dd>" + escapeHtml(rec.project.name || "—") + "</dd>" +
      "<dt>类型</dt><dd>" + (line.kind === "pipeline" ? "装配线" : "单机产线") + "</dd>" +
      "<dt>触发</dt><dd>" + escapeHtml(line.trigger_text || "—") + "</dd>" +
      "<dt>下次</dt><dd>" + escapeHtml(next) + "</dd>" +
      "<dt>状态</dt><dd>" + escapeHtml(lineStateText(line)) + "</dd>" +
      (line.status === "running"
        ? "<dt>在跑</dt><dd id=\"fx-progress\">" +
          escapeHtml(progressText(line.progress) || "启动中") + "</dd>"
        : "") +
      "</div>";
    if (line.kind === "pipeline" && (line.nodes || []).length) {
      html += "<div><div class=\"dim small\">工位</div><div class=\"fx-run-list\">";
      line.nodes.slice(0, 20).forEach(function (n) {
        html += '<div class="fx-run-row"><span class="fx-run-name">' + escapeHtml(n.title || ("工位 " + n.id)) +
          '</span><span class="fx-run-meta">' + escapeHtml(nodeStatusText(n.status)) + "</span></div>";
      });
      html += "</div></div>";
    }
    // 体检数字（成功率 / P95 / 色条）折起来：首屏只说「这轮在做什么」。
    // 要看趋势再展开——它属于排查，不属于观战。
    html += '<details class="fx-more"><summary>更多：产线体检</summary>' +
      healthBlock(line) + "</details>";
    if (line.kind === "pipeline") html += '<button class="btn-ghost" data-view-pipeline="1">查看编排</button>';
    html += '<div class="fx-drawer-ops">';
    if (line.kind === "cron") {
      html += '<button class="btn-primary" data-run-now="1">立即运行</button>';
      html += '<button class="btn-ghost" data-toggle-line="1">' +
        (line.status === "paused" || line.status === "stopped" ? "启用产线" : "暂停产线") + "</button>";
      if (line.status === "running") {
        html += '<button class="btn-ghost fx-danger" data-cancel-line="1">叫停</button>';
      }
      // 看到「上次失败」之后最常见的下一步：改提示词、补预授权工具。
      // 以前要返回对话 → 切项目 → 开自动化面板 → 找任务四步，现在一步。
      html += '<button class="btn-ghost" data-edit-line="1" ' +
        'title="切到这个厂区并打开自动化面板，改提示词或预授权工具">去编辑</button>';
    }
    html += '<button class="btn-ghost" data-close-drawer="1">关闭</button></div>';
    html += '<div><div class="dim small">最近运行</div><div id="fx-line-runs" class="fx-run-list">' +
      '<div class="fx-run-empty">读取中…</div></div></div>';
    openDrawer(drawerShell(escapeHtml(line.name || key), html));
    if (!dom.drawer) return;

    var pipeBtn = dom.drawer.querySelector("[data-view-pipeline]");
    if (pipeBtn) pipeBtn.onclick = function () { closeDrawer(); openPipelineFor(rec.project.id, line.id); };
    var runBtn = dom.drawer.querySelector("[data-run-now]");
    if (runBtn) {
      runBtn.onclick = function () { runLine(line, runBtn); };
    }
    var toggleBtn = dom.drawer.querySelector("[data-toggle-line]");
    if (toggleBtn) {
      toggleBtn.onclick = function () { toggleLine(line, toggleBtn); };
    }
    var cancelBtn = dom.drawer.querySelector("[data-cancel-line]");
    if (cancelBtn) {
      cancelBtn.onclick = function () { cancelRun(line.id, line.name, cancelBtn); };
    }
    var editBtn = dom.drawer.querySelector("[data-edit-line]");
    if (editBtn) {
      editBtn.onclick = function () {
        closeDrawer();
        if (typeof openAutomationFor === "function") {
          openAutomationFor(rec.project.id, line.id);
        }
      };
    }
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;

    loadLineRuns(line);
  }

  function nodeStatusText(s) {
    return {
      blocked: "等待依赖", ready: "待运行", running: "运行中",
      done: "已完成", error: "失败", cancelled: "已取消", skipped: "已跳过",
    }[s] || s || "—";
  }

  function loadLineRuns(line) {
    var box = dom.drawer && dom.drawer.querySelector("#fx-line-runs");
    if (!box) return;
    var params = { limit: 20 };
    if (line.kind === "pipeline") params.pipeline_id = line.id;
    else { params.task_id = line.id; params.kind = "cron"; }
    request("factory.runs", params).then(function (r) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-line-runs");
      if (!host || host !== box) return;
      var rows = (r && r.runs) || [];
      rows.forEach(function (x) { S.runCache.set(x.id, x); });
      if (!rows.length) {
        host.innerHTML = '<div class="fx-run-empty">还没有运行记录</div>';
        return;
      }
      host.innerHTML = rows.map(function (x) {
        return '<button class="fx-run-row" data-run-id="' + escapeHtml(String(x.id)) + '">' +
          '<span class="fx-run-name">#' + escapeHtml(String(x.id)) + " · " + escapeHtml(x.name || "") + "</span>" +
          '<span class="fx-run-meta">' + escapeHtml(RUN_STATUS[x.status] || x.status) + " · " +
          escapeHtml(fmtDurationShort(x)) + "</span></button>";
      }).join("");
      host.querySelectorAll("[data-run-id]").forEach(function (b) {
        b.onclick = function () { openRunDrawer(Number(b.dataset.runId)); };
      });
    }).catch(function (e) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-line-runs");
      if (host && host === box) host.innerHTML = '<div class="fx-run-empty">读取失败：' + escapeHtml(e.message || "") + "</div>";
    });
  }

  function fmtDurationShort(run) {
    if (run.status === "running") return "运行中";
    return fmtDur(runElapsed(run));
  }

  /** 运行行的名字兜底：产线被删后 name 是空串，列表里不能渲染成「厂区 / 」。 */
  function runTitle(run) {
    return run.name || "（已删除的产线）";
  }

  /** 「做成」判定的小标签（空 = 还没有结论，不占位）。 */
  function outcomeChip(run) {
    var oc = run.outcome || "";
    if (!oc) return "";
    var cls = oc === "ok" ? "ok" : (oc === "failed" ? "bad" : "warn");
    var tip = (run.outcome_source === "user" ? "你手动判定" : "自动判定") + "：" + (RUN_OUTCOME[oc] || oc);
    return '<i class="fx-outcome ' + cls + '" title="' + escapeHtml(tip) + '">' +
      escapeHtml(RUN_OUTCOME[oc] || oc) + "</i>";
  }

  var RUN_PAGE = 30;

  /** 全部运行：状态 / 厂区筛选 + 分页 + 行内打开产出。

      后端 factory.runs 一直支持 status / project_id / offset，前端以前只拉
      固定的 60 条平铺——第 61 条以后的历史在工厂里根本翻不到。
   */
  function openAllRunsDrawer(initial) {
    S.drawer = { type: "all" };
    // 每次打开从「全部状态 / 全部厂区」开始：S.runsFilter 会跨打开保留，
    // 而下面的 select 总是重建为默认项——只重置 offset 会让列表按上次筛选
    // 查询、界面却显示「全部」，是最容易让人误判历史的一条。
    initial = initial || {};
    S.runsFilter = { status: initial.status || "", project_id: 0, offset: 0, view: initial.view || "all" };
    var st = activeState();
    var projects = (st && st.projects) || [];
    var projOpts = ['<option value="0">全部厂区</option>'].concat(projects.map(function (p) {
      return '<option value="' + escapeHtml(String(p.id)) + '">' +
        escapeHtml(p.name || ("项目 " + p.id)) + "</option>";
    })).join("");
    var statusOpts = [
      ["", "全部状态"], ["running", "运行中"], ["done", "已完成"], ["empty", "空产出"],
      ["error", "失败"], ["interrupted", "中断"], ["skipped", "跳过"], ["cancelled", "已取消"],
    ].map(function (x) {
      return '<option value="' + x[0] + '">' + x[1] + "</option>";
    }).join("");
    var html = '<div class="fx-runs-filter">' +
      '<select id="fx-runs-status" aria-label="按状态筛选">' + statusOpts + "</select>" +
      '<select id="fx-runs-project" aria-label="按厂区筛选">' + projOpts + "</select>" +
      "</div>" +
      '<div id="fx-all-runs" class="fx-run-list"><div class="fx-run-empty">读取中…</div></div>' +
      '<div class="fx-runs-pager" id="fx-runs-pager"></div>';
    var viewTitle = { all: "全部运行", done_today: "今日做成", tokens_today: "今日 token" };
    var note = S.runsFilter.view === "done_today"
      ? "本地今天零点后完成，按自动或人工判定的做成口径统计（含旧版完成记录）。"
      : (S.runsFilter.view === "tokens_today" ? "本地今天零点后开始的运行；用量为输入 + 输出，未记录用量的运行记为 0。" : "");
    openDrawer(drawerShell(viewTitle[S.runsFilter.view] || "全部运行",
      (note ? '<p class="fx-form-msg">' + note + "</p>" : "") + html));
    if (!dom.drawer) return;
    var statusSel = dom.drawer.querySelector("#fx-runs-status");
    var projSel = dom.drawer.querySelector("#fx-runs-project");
    if (statusSel) {
      statusSel.value = S.runsFilter.status;
      statusSel.onchange = function () {
        S.runsFilter.status = statusSel.value;
        S.runsFilter.offset = 0;
        loadAllRuns();
      };
    }
    if (projSel) {
      projSel.onchange = function () {
        S.runsFilter.project_id = Number(projSel.value) || 0;
        S.runsFilter.offset = 0;
        loadAllRuns();
      };
    }
    loadAllRuns();
  }

  var runsRequestId = 0;

  function loadAllRuns() {
    var f = Object.assign({}, S.runsFilter);
    var requestId = ++runsRequestId;
    var drawer = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
    var params = { limit: RUN_PAGE, offset: f.offset || 0 };
    params.view = f.view || "all";
    if (f.status) params.status = f.status;
    if (f.project_id) params.project_id = f.project_id;
    request("factory.runs", params).then(function (r) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
      if (!host || host !== drawer || requestId !== runsRequestId) return;
      var rows = (r && r.runs) || [];
      rows.forEach(function (x) { S.runCache.set(x.id, x); });
      if (!rows.length) {
        host.innerHTML = '<div class="fx-run-empty">' +
          (f.offset ? "这一页没有更多了。" : "没有符合条件的运行记录。") + "</div>";
      } else {
        host.innerHTML = rows.map(function (x) {
          var path = x.result_path || "";
          return '<div class="fx-run-row" data-run-id="' + escapeHtml(String(x.id)) + '">' +
            '<span class="fx-run-name">#' + escapeHtml(String(x.id)) + " · " +
            escapeHtml(x.project_name || "—") + " / " + escapeHtml(runTitle(x)) + "</span>" +
            outcomeChip(x) +
            '<span class="fx-run-meta">' + escapeHtml(RUN_STATUS[x.status] || x.status) +
            " · " + escapeHtml(fmtDurationShort(x)) +
            (f.view === "tokens_today" ? " · " + (Number(x.in_tokens || 0) + Number(x.out_tokens || 0)).toLocaleString() + " token" : "") + "</span>" +
            (path
              ? '<button class="fx-ack" data-open-result="' + escapeHtml(path) + '"' +
                ' title="打开这个工位的产出全文">产出</button>'
              : "") +
            "</div>";
        }).join("");
        host.querySelectorAll("[data-run-id]").forEach(function (el) {
          var open = function (ev) {
            if (ev && ev.target.closest("[data-open-result]")) return;
            openRunDrawer(Number(el.dataset.runId));
          };
          el.setAttribute("role", "button");
          el.setAttribute("tabindex", "0");
          el.onclick = open;
          el.onkeydown = function (ev) {
            if (ev.key === "Enter" || ev.key === " ") {
              ev.preventDefault();
              open(ev);
            }
          };
        });
        host.querySelectorAll("[data-open-result]").forEach(function (b) {
          b.onclick = function (ev) {
            ev.stopPropagation();
            openFactoryResult(b.dataset.openResult);
          };
        });
      }
      var pager = dom.drawer && dom.drawer.querySelector("#fx-runs-pager");
      if (!pager) return;
      var off = f.offset || 0;
      pager.innerHTML =
        '<button class="btn-ghost" data-page="prev"' + (off > 0 ? "" : " disabled") + ">上一页</button>" +
        '<span class="fx-run-meta">第 ' + (Math.floor(off / RUN_PAGE) + 1) + " 页 · "
        + rows.length + " 条</span>" +
        '<button class="btn-ghost" data-page="next"' + (rows.length >= RUN_PAGE ? "" : " disabled") + ">下一页</button>";
      pager.querySelectorAll("[data-page]").forEach(function (b) {
        b.onclick = function () {
          if (b.disabled) return;
          S.runsFilter.offset = b.dataset.page === "next"
            ? off + RUN_PAGE : Math.max(0, off - RUN_PAGE);
          loadAllRuns();
        };
      });
    }).catch(function (e) {
      var host = dom.drawer && dom.drawer.querySelector("#fx-all-runs");
      if (host && host === drawer && requestId === runsRequestId) host.innerHTML = '<div class="fx-run-empty">读取失败：' + escapeHtml(e.message || "") + "</div>";
    });
  }

  /** 打开编排节点的产出全文（<项目>/.skysheep/pipeline-results/node-<id>.md）。
      路径由后端在 run.result_path 里给出，前端不拼目录。 */
  function openFactoryResult(path) {
    if (!path) return;
    closeDrawer();
    if (typeof backToChat === "function") backToChat();
    if (typeof openRightTab === "function") openRightTab("files");
    if (typeof openFile === "function") {
      Promise.resolve(openFile(path)).catch(function (e) {
        addNotice("打开产出失败：" + (e && e.message ? e.message : "未知错误"));
      });
    } else {
      addNotice("当前版本没有文件预览入口");
    }
  }

  /** 「对话在跑」抽屉：只读快照 + 跳到该会话。
      工厂只覆盖定时任务与编排，用户自己发起的对话轮 / 子代理在这里是盲区。 */
  function openLiveDrawer() {
    var st = activeState();
    var live = (st && st.live) || {};
    var rows = live.runs || [];
    S.drawer = { type: "live" };
    var html = "";
    if (!rows.length) {
      html = '<div class="fx-run-empty">当前没有在跑的对话轮或后台子代理。' +
        "工厂只统计定时任务与编排；你在对话里发起的运行会在这里出现。</div>";
    } else {
      html = '<div class="fx-run-list">' + rows.map(function (r) {
        var kind = r.kind === "turn" ? "对话" : "子代理";
        var title = r.title || (r.kind === "turn" ? "对话轮" : "后台任务");
        return '<div class="fx-run-row">' +
          '<span class="fx-run-name">' + escapeHtml(title) + "</span>" +
          '<span class="fx-run-meta">' + escapeHtml(kind) +
          (liveStale(r) ? ' · <span class="fx-bad">5 分钟无更新</span>' : "") + "</span>" +
          (r.session_id
            ? '<button class="fx-ack" data-live-session="' + escapeHtml(String(r.session_id)) +
              '" title="打开这个会话">去会话</button>'
            : "") +
          "</div>";
      }).join("") + "</div>" +
        '<p class="fx-form-msg">这里是只读观战：对话轮的中止请在会话里操作（工厂的「叫停」只中止定时任务）。</p>';
    }
    openDrawer(drawerShell("对话与子代理在跑", html));
    if (!dom.drawer) return;
    dom.drawer.querySelectorAll("[data-live-session]").forEach(function (b) {
      b.onclick = function () {
        closeDrawer();
        if (typeof backToChat === "function") backToChat();
        if (typeof openTabForSession === "function") openTabForSession(b.dataset.liveSession, "");
      };
    });
  }

  function runLine(line, btn) {
    if (btn) btn.disabled = true;
    request("factory.run_now", { task_id: line.id }).then(function (r) {
      // 后台派工：回执只表示「已起跑」，包裹稍后由 started 事件送进场景
      if (r && r.started) addNotice("已派工：" + (line.name || line.key) + "（包裹马上上带）");
      else addNotice((r && r.notice) || "该工位正在运行中，这次没有重复派工");
    }).catch(function (e) {
      addNotice("派工失败：" + (e.message || "未知错误"));
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  }

  function toggleLine(line, btn) {
    var enabled = line.status === "paused" || line.status === "stopped";
    if (btn) btn.disabled = true;
    request("factory.toggle", { task_id: line.id, enabled: enabled }).then(function (r) {
      line.status = enabled ? "idle" : "paused";
      if (r && r.next_run_at != null) line.next_run_at = r.next_run_at;
      addNotice(enabled ? "产线已启用" : "产线已暂停（不再到点派工；仍可手动运行一次）");
      renderPanel();
      FX.render();  // 绘制层真身（运行时查表）
      if (S.drawer && S.drawer.type === "line") openLineDrawer(line.key, { focus: false });
    }).catch(function (e) {
      addNotice("操作失败：" + (e.message || "未知错误"));
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  }

  /** 待发信息弹窗：未来 30 分钟内到点的定时产线 + 已过点等待调度的排期。

      牌面收成小立牌后放不下明细，这里用居中的只读弹窗列出：任务名 / 厂区 /
      倒计时 / 状态。点其中一条开原产线抽屉（openLineDrawer），**不派工**——
      到点由后端调度，误触不该让还没到点的那一轮提前跑。风格复用通用 showModal
      （与 app-mods 的只读弹窗同路数），关 / Esc / 点背衬都走 hideModal。
      打开时用 refreshUpcoming 现算一次「取最新」，不新增任何定时器。 */
  function openUpcomingDialog() {
    refreshUpcoming();
    var list = S.upcoming || [];
    var box = document.createElement("div");
    if (!list.length) {
      box.innerHTML = '<p class="fx-run-empty">暂无待发</p>' +
        '<p class="fx-form-msg">未来 30 分钟内没有到点的定时产线，也没有等待调度的过期排期。</p>';
    } else {
      box.innerHTML = '<p class="fx-form-msg">未来 30 分钟内到点，以及已过点等待调度的定时产线。' +
        "点一条查看产线（不会派工）。</p>" +
        '<div class="fx-run-list">' + list.map(function (it) {
          var status = it.due ? "等待调度" : "倒计时 " + fmtCountdown(it.eta);
          return '<button type="button" class="fx-run-row" data-upcoming="' +
            escapeHtml(String(it.key || "")) + '">' +
            '<span class="fx-run-name">' + escapeHtml(it.name || it.key || "") + "</span>" +
            '<span class="fx-run-meta">' + escapeHtml(it.project || "—") + " · " +
            escapeHtml(status) + "</span></button>";
        }).join("") + "</div>";
    }
    var modalEl = document.getElementById("modal");
    var cancelBtn = document.getElementById("modal-cancel");
    var hidCancel = !!cancelBtn;
    if (hidCancel) cancelBtn.style.display = "none";   // 只读弹窗：只留「关闭」
    showModal("待发告示牌", box, function () {}, "关闭");
    // 点背衬 = 关闭；隐藏后归还取消键并摘掉监听（与 confirmModal 同一套收尾）
    var onBackdrop = function (ev) { if (ev.target === modalEl) hideModal(); };
    modalEl.addEventListener("click", onBackdrop);
    var obs = new MutationObserver(function () {
      if (!modalEl.classList.contains("hidden")) return;
      obs.disconnect();
      modalEl.removeEventListener("click", onBackdrop);
      if (hidCancel) cancelBtn.style.display = "";
    });
    obs.observe(modalEl, { attributes: true, attributeFilter: ["class"] });
    var rows = box.querySelectorAll("[data-upcoming]");
    rows.forEach(function (b) {
      b.onclick = function () {
        var key = b.dataset.upcoming || "";
        if (!key || !findLine(key)) { addNotice("这条产线已不在当前视图中"); return; }
        hideModal();
        openLineDrawer(key, { focus: true });   // 只开产线详情，绝不派工
      };
    });
    // 只读弹窗藏了取消键，键盘焦点落在第一条行上（空态落「关闭」）；
    // showModal 的延后接管看到焦点已在弹层内就不会再抢。
    var focusTarget = rows[0] || document.getElementById("modal-ok");
    if (focusTarget && focusTarget.focus) focusTarget.focus();
  }

  function openQueueDrawer() {
    var st = activeState() || {};
    var projects = st.projects || [];
    var rows = (st.waiting || []).map(function (r) {
      return { key: r.key, name: r.name, project_id: r.project_id,
        note: "定时产线 · 已等待 " + fmtDur(Math.max(0, nowMs() / 1000 - (r.due_at || 0))) };
    });
    projects.forEach(function (p) {
      (p.lines || []).filter(function (l) { return l.kind === "pipeline"; }).forEach(function (l) {
        (l.nodes || []).filter(function (n) { return n.status === "ready"; }).forEach(function (n) {
          rows.push({ key: l.key, name: l.name + " / " + n.title, project_id: p.id,
            note: "编排工位 · 依赖已满足，等待调度" });
        });
      });
    });
    S.drawer = { type: "queue" };
    var total = Number((st.counters || {}).queued) || 0;
    var html = '<p class="fx-form-msg">定时产线等待并发额度；编排工位已满足依赖，等待调度。点击查看对应产线。</p>';
    html += rows.length ? '<div class="fx-run-list">' + rows.map(function (r) {
      var p = projects.find(function (p) { return p.id === r.project_id; });
      return '<button class="fx-run-row" data-queue-line="' + escapeHtml(r.key) + '"><span class="fx-run-name">' +
        escapeHtml((p ? p.name + " · " : "") + r.name) + '</span><span class="fx-run-meta">' +
        escapeHtml(r.note) + '</span></button>';
    }).join("") + '</div>' : '<div class="fx-run-empty">当前没有等待中的产线。</div>';
    if (total > rows.length) html += '<p class="fx-form-msg">共 ' + total + ' 项，当前列出 ' + rows.length + ' 项（明细有显示上限，已删除厂区也可能无法列出）。</p>';
    openDrawer(drawerShell("排队明细", html));
    if (dom.drawer) dom.drawer.querySelectorAll("[data-queue-line]").forEach(function (b) {
      b.onclick = function () { openLineDrawer(b.dataset.queueLine, { focus: true }); };
    });
  }

  function openAttentionDrawer() {
    var st = activeState();
    var items = (st && st.attention) || [];
    S.drawer = { type: "attention" };
    var html = "";
    // 结构性问题（停工 / 孤儿）要用户去修，标记「知道了」没有意义也不生效，
    // 所以只给运行类问题（失败 / 中断 / 跳过 / 空产出 / 有拦截）一个消化按钮。
    var ackable = { error: 1, interrupted: 1, skipped: 1, empty: 1, partial: 1 };
    var keys = [];
    if (!items.length) {
      html = '<div class="fx-run-empty">没有需要留意的事。</div>';
    } else {
      var kindText = {
        error: "失败", interrupted: "中断", skipped: "跳过",
        empty: "无产出", partial: "有拦截",
        orphan: "孤儿", stopped: "停工",
      };
      html = '<div class="fx-run-list">' + items.map(function (a) {
        var key = String(a.line_key || "");
        var canAck = !!ackable[a.kind] && key;
        if (canAck) keys.push(key);
        return '<div class="fx-run-row fx-attn-row" data-attn-line="' + escapeHtml(key) +
          '" role="button" tabindex="0" title="点开对应产线">' +
          '<span class="fx-run-name">' +
          escapeHtml(a.text || a.kind || "") + '</span><span class="fx-run-meta">' +
          escapeHtml(kindText[a.kind] || a.kind || "") + "</span>" +
          (a.kind === "partial" && findLine(key)
            ? '<button class="fx-ack" data-attn-process="' + escapeHtml(key) + '">去处理</button>' : "") +
          (canAck
            ? '<button class="fx-ack" data-ack="' + escapeHtml(key) + '"' +
              ' title="这条产线的这个问题已处理；同一问题再发生会重新提示">知道了</button>'
            : '<span class="fx-run-meta">修好即消失</span>') +
          "</div>";
      }).join("") + "</div>" +
        '<p class="fx-form-msg">点任意一行打开对应产线；「知道了」只压住这次，之后的新问题会重新提示。</p>';
      // 列表只画前 50 条（服务端上限），但计数是全部：不能让人以为「只剩这些」
      var total = Number(st && st.attention_total) || items.length;
      if (total > items.length) {
        html += '<p class="fx-form-msg">共 ' + total + " 条，这里只列出前 " +
          items.length + " 条；处理掉眼前这些再打开会看到剩下的。</p>";
      }
      if (keys.length > 1) {
        html += '<div class="fx-drawer-ops">' +
          '<button class="btn-ghost" data-ack-all="1">全部知道了</button></div>';
      }
    }
    openDrawer(drawerShell("需留意", html));
    if (!dom.drawer) return;
    dom.drawer.querySelectorAll("[data-attn-line]").forEach(function (row) {
      var open = function (ev) {
        if (ev && ev.target.closest("[data-ack], [data-attn-process]")) return;
        var key = row.dataset.attnLine || "";
        if (!key) return;
        if (!findLine(key)) {
          addNotice("这条产线已不在当前视图中（可能已被删除或所属项目已移除）");
          return;
        }
        openLineDrawer(key, { focus: true });
      };
      row.addEventListener("click", open);
      row.addEventListener("keydown", function (ev) {
        if (ev.target.closest("[data-ack], [data-attn-process]")) return;
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          open(ev);
        }
      });
    });
    dom.drawer.querySelectorAll("[data-attn-process]").forEach(function (b) {
      b.onclick = function (ev) {
        ev.stopPropagation();
        var rec = findLine(b.dataset.attnProcess);
        if (!rec || S.demo) return;
        closeDrawer();
        if (rec.line.kind === "cron") openAutomationFor(rec.project.id, rec.line.id);
        else openPipelineFor(rec.project.id, rec.line.id);
      };
    });
    dom.drawer.querySelectorAll("[data-ack]").forEach(function (b) {
      b.onclick = function (ev) {
        ev.stopPropagation();
        ackAttention([b.dataset.ack]);
      };
    });
    var allBtn = dom.drawer.querySelector("[data-ack-all]");
    if (allBtn) allBtn.onclick = function () { ackAttention(keys); };
  }

  // ---------- 新建产线 ----------
  function openNewLineDrawer(presetProject) {
    var st = S.state || activeState();
    var projects = (st && st.projects) || [];
    if (!projects.length) { addNotice("先在侧栏添加一个项目，再给它的厂区建产线"); return; }
    var tools = [];
    try {
      if (typeof bootSnap !== "undefined" && bootSnap && bootSnap.tools) tools = bootSnap.tools;
    } catch (e) { tools = []; }
    var projectOpts = projects.map(function (p) {
      return '<option value="' + escapeHtml(String(p.id)) + '"' +
        (presetProject && p.id === presetProject ? " selected" : "") + ">" +
        escapeHtml(p.name || ("项目 " + p.id)) + (p.root_ok === false ? "（停工）" : "") + "</option>";
    }).join("");
    var toolRows = tools.length ? tools.map(function (t) {
      var locked = t.safety === "readonly";
      return '<label title="' + escapeHtml(t.description || "") + '"><input type="checkbox" data-tool="' +
        escapeHtml(t.name) + '"' + (locked ? " checked disabled" : "") + ">" + escapeHtml(t.name) + "</label>";
    }).join("") : '<span class="fx-hint">暂时取不到工具清单；保存后可在自动化页再编辑。</span>';
    var weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    var html = '<form class="fx-form" id="fx-new-line">' +
      "<label>厂区（目标项目）</label><select id=\"fx-nl-project\">" + projectOpts + "</select>" +
      "<label>产线名</label><input id=\"fx-nl-name\" type=\"text\" placeholder=\"例如：每日项目状态汇总\">" +
      "<label>任务指令（到点由 Agent 执行的内容）</label>" +
      "<textarea id=\"fx-nl-prompt\" rows=\"3\" placeholder=\"例如：扫描当前目录的 TODO，汇总成一段进度报告\"></textarea>" +
      "<label>频率</label><select id=\"fx-nl-type\">" +
      '<option value="interval">每隔 N 分钟</option>' +
      '<option value="daily">每天固定时间</option>' +
      '<option value="weekly">每周固定时间</option></select>' +
      '<div id="fx-nl-interval-wrap"><label>间隔（分钟）</label>' +
      '<input id="fx-nl-interval" type="number" min="1" max="525600" step="5" value="60"></div>' +
      '<div id="fx-nl-time-wrap" class="hidden"><label>时间（HH:MM）</label>' +
      '<input id="fx-nl-time" type="time" value="09:00">' +
      '<label id="fx-nl-weekday-label" class="hidden">星期几</label>' +
      '<select id="fx-nl-weekday" class="hidden">' +
      weekdays.map(function (d, i) { return '<option value="' + i + '">' + d + "</option>"; }).join("") +
      "</select></div>" +
      "<label>预授权工具（无人值守时自动放行；不勾的会被拒绝）</label>" +
      '<div class="fx-tools">' + toolRows + "</div>" +
      '<p class="fx-hint">只读工具本来就放行；写入 / 执行类必须显式勾选。勾选 run_command 等于允许无人值守执行任意命令，建议只在任务内容完全可信时勾。</p>' +
      '<label class="fx-check"><input id="fx-nl-notify" type="checkbox">完成后推送到已启用的聊天渠道 / Webhook</label>' +
      '<p class="fx-hint">默认不推（每天几十次会淹人）；连续失败的聚合告警不受这个开关影响。</p>' +
      '<div class="fx-form-msg" id="fx-nl-msg"></div>' +
      '<div class="fx-drawer-ops"><button type="submit" class="btn-primary">创建产线</button>' +
      '<button type="button" class="btn-ghost" data-close-drawer="1">取消</button></div>' +
      "</form>";
    openDrawer(drawerShell("新建轻量定时产线", html));
    if (!dom.drawer) return;
    var form = dom.drawer.querySelector("#fx-new-line");
    var typeSel = dom.drawer.querySelector("#fx-nl-type");
    var syncType = function () {
      var v = typeSel.value;
      dom.drawer.querySelector("#fx-nl-interval-wrap").classList.toggle("hidden", v !== "interval");
      dom.drawer.querySelector("#fx-nl-time-wrap").classList.toggle("hidden", v === "interval");
      dom.drawer.querySelector("#fx-nl-weekday-label").classList.toggle("hidden", v !== "weekly");
      dom.drawer.querySelector("#fx-nl-weekday").classList.toggle("hidden", v !== "weekly");
    };
    typeSel.addEventListener("change", syncType);
    syncType();
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;
    form.onsubmit = function (ev) {
      ev.preventDefault();
      var msg = dom.drawer.querySelector("#fx-nl-msg");
      var prompt = dom.drawer.querySelector("#fx-nl-prompt").value.trim();
      if (!prompt) { msg.textContent = "任务指令不能为空"; msg.className = "fx-form-msg error"; return; }
      var name = dom.drawer.querySelector("#fx-nl-name").value.trim();
      var params = {
        project_id: Number(dom.drawer.querySelector("#fx-nl-project").value),
        name: name || (prompt.length > 20 ? prompt.slice(0, 20) : prompt),
        prompt: prompt,
        schedule_type: typeSel.value,
        interval_minutes: Number(dom.drawer.querySelector("#fx-nl-interval").value) || 60,
        time_of_day: dom.drawer.querySelector("#fx-nl-time").value || "09:00",
        weekday: Number(dom.drawer.querySelector("#fx-nl-weekday").value) || 0,
        allowed_tools: Array.prototype.map.call(
          dom.drawer.querySelectorAll("input[data-tool]:checked:not(:disabled)"),
          function (el) { return el.dataset.tool; }
        ),
        notify_channel: !!dom.drawer.querySelector("#fx-nl-notify").checked,
      };
      msg.textContent = "正在创建…";
      msg.className = "fx-form-msg";
      request("factory.line_add", params).then(function (r) {
        addNotice("产线已创建（#" + r.id + "），到点自动运行");
        closeDrawer();
        factoryReload();
      }).catch(function (e) {
        msg.textContent = e.message || "创建失败";
        msg.className = "fx-form-msg error";
      });
    };
  }

  // ---------- 工厂设置 ----------
  function openSettingsDrawer() {
    var st = S.state || activeState();
    var cfg = (st && st.config) || {};
    S.drawer = { type: "settings" };
    // 「对话在跑」原来占顶栏一格；计数条撤掉后入口挪到这里——它属于「看现场，
    // 不是看报表」，所以不进场景，也不占顶栏。
    var liveBox = (st && st.live) || {};
    var liveN = (Number(liveBox.chat) || 0) + (Number(liveBox.subagent) || 0);
    var html = '<div class="fx-drawer-ops fx-live-entry">' +
      '<button type="button" class="btn-ghost" data-open-live="1" ' +
      'title="工厂之外的盲区：哪些对话轮 / 后台子代理正在跑，哪个可能卡住了">' +
      "对话在跑 " + liveN + "</button></div>" +
      '<form class="fx-form" id="fx-settings">' +
      '<label class="fx-check"><input id="fx-cfg-enabled" type="checkbox"' +
      (cfg.enabled !== false ? " checked" : "") + ">显示工厂看板入口（左侧导航）</label>" +
      '<label class="fx-check"><input id="fx-cfg-demo" type="checkbox"' +
      (cfg.demo !== false ? " checked" : "") + ">没有真实产线时播放样板厂</label>" +
      '<label class="fx-check"><input id="fx-cfg-ambient" type="checkbox"' +
      (cfg.ambient !== false ? " checked" : "") + ">昼夜天色（按本机时间；开天气时按当地日出日落）</label>" +
      '<label class="fx-check"><input id="fx-cfg-weather" type="checkbox"' +
      (cfg.weather === true ? " checked" : "") + ">按现实天气渲染（联网，默认关）</label>" +
      "<label>天气城市（城市名，例如：上海 / Shanghai）</label>" +
      '<input id="fx-cfg-weather-location" type="text" maxlength="80" value="' +
      escapeHtml(String(cfg.weather_location || "")) + '" placeholder="留空则只用本机时间判断昼夜">' +
      '<p class="fx-hint">只把城市名交给 Open-Meteo 解析坐标，再按坐标取当前天气；不做 IP 定位、不需要 API Key，结果缓存 30 分钟。关闭时一次网络请求都不发。</p>' +
      "<label>运行历史保留条数（100–100000）</label>" +
      '<input id="fx-cfg-keep" type="number" min="100" max="100000" value="' +
      Number(cfg.history_keep || 2000) + '">' +
      "<label>运行历史保留天数（1–3650）</label>" +
      '<input id="fx-cfg-days" type="number" min="1" max="3650" value="' +
      Number(cfg.history_days || 30) + '">' +
      "<label>同屏最多包裹数（10–300）</label>" +
      '<input id="fx-cfg-max" type="number" min="10" max="300" value="' +
      Number(cfg.max_packages || 60) + '">' +
      "<label>每厂区最多画几台机台（1–12）</label>" +
      '<input id="fx-cfg-machines" type="number" min="1" max="12" value="' +
      Number(cfg.machine_limit || 6) + '">' +
      "<label>场景标签上限（10–80）</label>" +
      '<input id="fx-cfg-labels" type="number" min="10" max="80" value="' +
      Number(cfg.label_limit || 40) + '">' +
      '<p class="fx-hint">机台 / 标签超限的部分不受影响，仍会列在左面板与抽屉里；调大只影响场景里画多少。</p>' +
      "<label>单次运行超时（分钟，0 = 不限）</label>" +
      '<input id="fx-cfg-timeout" type="number" min="0" max="1440" value="' +
      Number(cfg.run_timeout_minutes != null ? cfg.run_timeout_minutes : 60) + '">' +
      '<p class="fx-hint">超时按失败收尾并释放占用位——没有它，一条挂在模型流式 / 网络上的运行会永远显示「运行中」。</p>' +
      "<label>同时运行的产线上限（1–20）</label>" +
      '<input id="fx-cfg-parallel" type="number" min="1" max="20" value="' +
      Number(cfg.max_parallel_runs || 3) + '">' +
      '<p class="fx-hint">到点的产线超过这个数会排队，等下一轮扫描（20 秒）再接；计数条里的「排队」按这个口径算。</p>' +
      "<label>连续失败告警阈值（0 = 不告警）</label>" +
      '<input id="fx-cfg-alert" type="number" min="0" max="50" value="' +
      Number(cfg.alert_after_failures != null ? cfg.alert_after_failures : 3) + '">' +
      '<p class="fx-hint">同一条产线连续失败达到这个次数推一条通知，每天最多一条（防打扰）。</p>' +
      '<div class="fx-form-msg" id="fx-cfg-msg"></div>' +
      '<div class="fx-drawer-ops"><button type="submit" class="btn-primary">保存</button>' +
      '<button type="button" class="btn-ghost" data-close-drawer="1">取消</button></div></form>';
    openDrawer(drawerShell("工厂设置", html));
    if (!dom.drawer) return;
    var form = dom.drawer.querySelector("#fx-settings");
    var closeBtn = dom.drawer.querySelector("[data-close-drawer]");
    if (closeBtn) closeBtn.onclick = closeDrawer;
    // 顶栏那一格「对话在跑」撤掉后，入口挪到设置抽屉顶部（它属于「看现场」，
    // 不属于场景，也不该常驻顶栏）
    var liveBtn = dom.drawer.querySelector("[data-open-live]");
    if (liveBtn) liveBtn.onclick = function () { openLiveDrawer(); };
    form.onsubmit = function (ev) {
      ev.preventDefault();
      var msg = dom.drawer.querySelector("#fx-cfg-msg");
      var weatherOn = dom.drawer.querySelector("#fx-cfg-weather").checked;
      var weatherCity = dom.drawer.querySelector("#fx-cfg-weather-location").value.trim();
      if (weatherOn && !weatherCity) {
        msg.textContent = "开启现实天气需要填写城市（例如：上海）";
        msg.className = "fx-form-msg error";
        return;
      }
      msg.textContent = "正在保存…";
      msg.className = "fx-form-msg";
      saveConfig({
        enabled: dom.drawer.querySelector("#fx-cfg-enabled").checked,
        demo: dom.drawer.querySelector("#fx-cfg-demo").checked,
        ambient: dom.drawer.querySelector("#fx-cfg-ambient").checked,
        weather: weatherOn,
        weather_location: weatherCity,
        history_keep: Number(dom.drawer.querySelector("#fx-cfg-keep").value) || 2000,
        history_days: Number(dom.drawer.querySelector("#fx-cfg-days").value) || 30,
        max_packages: Number(dom.drawer.querySelector("#fx-cfg-max").value) || 60,
        machine_limit: Number(dom.drawer.querySelector("#fx-cfg-machines").value) || 6,
        label_limit: Number(dom.drawer.querySelector("#fx-cfg-labels").value) || 40,
        run_timeout_minutes: Number(dom.drawer.querySelector("#fx-cfg-timeout").value) || 0,
        max_parallel_runs: Number(dom.drawer.querySelector("#fx-cfg-parallel").value) || 3,
        alert_after_failures: Number(dom.drawer.querySelector("#fx-cfg-alert").value) || 0,
      }, null, msg);
    };
  }

  function saveConfig(patch, btn, msgEl) {
    if (btn) btn.disabled = true;
    return request("factory.save", patch).then(function (r) {
      var cfg = (r && r.config) || patch;
      if (S.state) S.state.config = Object.assign({}, S.state.config || {}, cfg);
      if (S.demo) S.demo.config = Object.assign({}, S.demo.config || {}, cfg);
      applyNavVisibility(cfg.enabled !== false);
      addNotice("工厂设置已保存");
      if (msgEl) { msgEl.textContent = "已保存"; msgEl.className = "fx-form-msg"; }
      renderAttentionNotice((S.state && S.state.config) || cfg);
      renderWeatherChip();
      renderAll();
      // 天气开关 / 城市变了：清掉旧快照，强制拉一次（后端仍会按城市缓存）
      if (Object.prototype.hasOwnProperty.call(patch, "weather")
          || Object.prototype.hasOwnProperty.call(patch, "weather_location")) {
        S.weatherSeq += 1; // 让换城市 / 关天气前发出的请求作废
        S.weather = null;
        S.lastWeatherAt = 0;
        refreshWeather(true);
      }
      // 样板厂开关变了：立即重取一次，进入 / 退出演示状态
      if ((S.demo && cfg.demo === false) || (!S.demo && cfg.demo === true && !hasRealLines(S.state))) {
        factoryReload();
      } else if (Object.prototype.hasOwnProperty.call(patch, "machine_limit")) {
        factoryReload();   // 机台上限变了要重建世界（画几台机台在世界构建时定死）
      }
      if (S.drawer && S.drawer.type === "settings" && msgEl) {
        // 留在设置抽屉里，让用户看到结果
      } else if (S.drawer && S.drawer.type === "settings") {
        closeDrawer();
      }
    }).catch(function (e) {
      if (msgEl) { msgEl.textContent = e.message || "保存失败"; msgEl.className = "fx-form-msg error"; }
      else addNotice("保存失败：" + (e.message || "未知错误"));
    }).finally(function () {
      if (btn) btn.disabled = false;
    });
  }

  // ---------- 事件：factory_run（开始 / 工序进度 / 结束） ----------
  /** 进度文案：工具名 + 步数；没有工具时退回「第 N 步 / 启动中」。
      被门控拦过的次数挂在后面——这是「预授权没勾全」唯一看得见的痕迹。 */
  function progressText(pr) {
    var fresh = freshProgress(pr);
    if (!fresh) return "";
    var text;
    if (fresh.tool) text = fresh.tool + (fresh.steps ? " · 第 " + fresh.steps + " 步" : "");
    else if (fresh.steps) text = "第 " + fresh.steps + " 步";
    else text = "启动中";
    var denied = Number(fresh.denied) || 0;
    if (denied > 0) text += " · 被拦 " + denied + " 次";
    return text;
  }

  /** 进度事件：只落到产线 / 运行对象上，不重建场景（包裹位置另有计时驱动）。 */
  function applyRunProgress(payload) {
    if (!payload || payload.id == null) return;
    var key = payload.kind === "pipeline_node"
      ? lineKeyFor("pipeline", payload.pipeline_id) : lineKeyFor("cron", payload.task_id);
    var pr = payload.progress || {};
    var step = {
      tool: String(pr.tool || ""),
      at: Number(pr.at) || 0,
      steps: Number(pr.steps) || 0,
      denied: Number(pr.denied) || 0,
    };
    var rec = S.world && S.world.lines.get(key);
    if (rec) rec.line.progress = step;
    var cached = findRun(payload.id);
    if (cached) cached.progress = step;
    if (!S.mounted) return;
    // 面板最多 2 次/秒重建（多枚包裹同时推进时不要每个事件都重排 DOM）
    var now = nowMs();
    if (!S.lastProgressPanel || now - S.lastProgressPanel > 500) {
      S.lastProgressPanel = now;
      refreshPanelStates();
    }
    // 抽屉正开着这条产线 / 这一轮：就地更新进度行，不重建抽屉（避免抢焦点）
    if (S.drawer && S.drawer.type === "line" && S.drawer.key === key && dom.drawer) {
      var el = dom.drawer.querySelector("#fx-progress");
      if (el) el.textContent = progressText(step) || "—";
    }
    if (S.drawer && S.drawer.type === "run" && Number(S.drawer.id) === Number(payload.id)
        && dom.drawer) {
      var el2 = dom.drawer.querySelector("#fx-run-progress");
      if (el2) el2.textContent = progressText(step) || "—";
    }
  }

  /** 叫停：只停本轮，不动启用状态与排期（后端写 cancelled 终态并重排下次）。 */
  function cancelRun(taskId, name, btn) {
    confirmModal(
      "叫停会立刻中止「" + (name || "这条产线") + "」本轮的运行，已消耗的 token 不会退回；"
        + "产线保持启用，下一轮照原排期继续。",
      { title: "叫停正在跑的运行", okLabel: "叫停", danger: true },
    ).then(function (ok) {
      if (!ok) return;
      if (btn) btn.disabled = true;
      request("factory.cancel", { task_id: taskId }).then(function (r) {
        if (r && r.cancelled) addNotice("已叫停：" + (name || ("任务 " + taskId)));
        else addNotice((r && r.notice) || "没有可叫停的运行");
        if (!(r && r.cancelled) && btn) btn.disabled = false;
      }).catch(function (e) {
        addNotice("叫停失败：" + (e.message || "未知错误"));
        if (btn) btn.disabled = false;
      });
    });
  }

  /** 「这轮算过 / 不算」：人工判定只改 outcome，不删历史、不改状态。

      自动判定（跑完且有产出 / 有没有被门控拦过）回答不了「产出对不对」，
      而成功率要能当决策依据就必须允许人推翻它。 */
  function markRunOutcome(run, outcome, btn) {
    if (btn) btn.disabled = true;
    request("factory.run_mark", { run_id: run.id, outcome: outcome }).then(function (r) {
      var row = (r && r.run) || {};
      run.outcome = row.outcome || "";
      run.outcome_source = row.outcome_source || "";
      var cached = findRun(run.id);
      if (cached && cached !== run) {
        cached.outcome = run.outcome;
        cached.outcome_source = run.outcome_source;
      }
      addNotice(outcome === "ok" ? "已标记：这轮算过"
        : outcome === "failed" ? "已标记：这轮不算" : "已撤销人工判定");
      if (S.drawer && S.drawer.type === "run") openRunDrawer(run.id);
      scheduleResync();   // 产线体检的成功率要跟着重算
    }).catch(function (e) {
      addNotice("标记失败：" + (e.message || "未知错误"));
      if (btn) btn.disabled = false;
    });
  }

  /** 「需留意」已处理：只记时间戳，不删运行历史；更晚的失败会重新浮上来。 */
  function ackAttention(keys) {
    keys = (keys || []).filter(function (k) { return !!k; });
    if (!keys.length) return;
    request("factory.ack", { line_keys: keys }).then(function () {
      addNotice("已标记为知道了（同一问题再发生会重新提示）");
      closeDrawer();
      factoryReload();
    }).catch(function (e) {
      addNotice("标记失败：" + (e.message || "未知错误"));
    });
  }
  // ---------- FX 导出（主文件转发目标 + 测试探针入口） ----------
  FX.applyRunProgress = applyRunProgress; FX.closeDrawer = closeDrawer;
  FX.openAllRunsDrawer = openAllRunsDrawer; FX.openAttentionDrawer = openAttentionDrawer;
  FX.openLineDrawer = openLineDrawer; FX.openLiveDrawer = openLiveDrawer;
  FX.openNewLineDrawer = openNewLineDrawer; FX.openRunDrawer = openRunDrawer;
  FX.openSettingsDrawer = openSettingsDrawer; FX.openQueueDrawer = openQueueDrawer;
  FX.openUpcomingDialog = openUpcomingDialog;
  FX.saveConfig = saveConfig; FX.loadAllRuns = loadAllRuns;
  FX.progressText = progressText;

  window.factoryEscape = factoryEscape;
})();