// app-whitelist.js —— 设置 · 白名单（手动添加 / 撤销 / 清空 / 测试 / 导入导出 / 规则检测）
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有声明与纯数据常量（ruleCheckRun 为函数值 const，声明非调用），
// 零加载期执行；按钮接线（含 4 处加载期函数引用解引用）仍留在 app.js 原位，
// 故 index.html 先载本文件再载 app.js；request / escapeHtml 等助手是运行时调用。

// ---------- 白名单（设置页）：手动添加 / 撤销 / 清空 / 测试 / 导入导出 ----------
// 规则类型的中文标签：设置页列表、确认弹窗的「将添加规则」共用
const RULE_KIND_LABEL = { always: "整个工具", prefix: "前缀", exact: "仅此一条", glob: "通配" };

let rulesStatusTimer = null;

function ruleLabelText(r) {
  const kind = RULE_KIND_LABEL[r.kind] || r.kind;
  return `${r.tool} · ${kind} ${r.pattern || "（全部）"}`;
}

// 操作反馈统一走卡片里的状态行（设置页里对话区是隐藏的，addNotice 看不到）；
// 带「撤销」按钮的提示停留更久
function showRulesStatus(text, action) {
  const el = document.getElementById("rules-status");
  if (!el) return;
  el.innerHTML = "";
  el.appendChild(document.createTextNode(text));
  if (action) {
    const btn = document.createElement("button");
    btn.className = "btn-ghost rule-undo";
    btn.textContent = action.label;
    btn.onclick = action.onClick;
    el.appendChild(btn);
  }
  el.hidden = false;
  if (rulesStatusTimer) clearTimeout(rulesStatusTimer);
  rulesStatusTimer = setTimeout(() => {
    el.hidden = true;
    el.innerHTML = "";
  }, action ? 12000 : 6000);
}

function fmtRuleAge(ts) {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

// exact 规则的来源（轻量推断：三类调用走 exact——含拼接的命令、键盘内容、剪贴板内容，
// 以及 window 的 close 按标题固化）
function ruleOriginNote(r) {
  if (r.kind !== "exact") return "";
  if (r.tool === "keyboard") return "来源：键盘输入内容";
  if (r.tool === "clipboard_write") return "来源：剪贴板写入内容";
  if (r.tool === "window") return "来源：关闭窗口标题";
  if (r.tool === "run_command" && /[;|&<>`$\r\n]/.test(r.pattern || "")) return "来源：含拼接的命令";
  return "来源：单次调用参数";
}

// 手动添加的警示（不阻塞；danger 级需要在表单里勾选确认）
function ruleAddWarning(tool, kind) {
  if (!tool) return null;
  if (kind === "always" && tool === "run_command")
    return {
      danger: true,
      text: "「整个工具」放行 run_command 等于所有命令不再询问（包括删除类命令）。"
        + "建议改用「前缀」；仍要添加请先勾选下方确认。",
    };
  if (kind !== "exact" && (tool === "keyboard" || tool === "clipboard_write"))
    return {
      danger: false,
      text: "键盘 / 剪贴板不按动作整类放行：整类放行等于允许向任意焦点窗口输入任意内容 / "
        + "把任意内容写进剪贴板，建议改用「仅此一条」。",
    };
  if (kind === "prefix" && tool === "window")
    return {
      danger: false,
      text: "窗口动作按动作词放行；其中「关闭窗口」不会整类放行（只按标题匹配）。",
    };
  const safety = ((bootSnap && bootSnap.tools) || []).find((t) => t.name === tool)?.safety;
  if (kind === "always" && (safety === "write" || safety === "dangerous"))
    return { danger: false, text: `「整个工具」将放行 ${tool} 的全部调用（不限路径 / 目标）。` };
  return null;
}

function updateRuleAddFormState() {
  const tool = document.getElementById("rule-tool").value.trim();
  const kind = document.getElementById("rule-kind").value;
  const patternInput = document.getElementById("rule-pattern");
  const hint = document.getElementById("rule-add-hint");
  const warnEl = document.getElementById("rule-add-warn");
  const ackRow = document.getElementById("rule-add-ack-row");
  const ack = document.getElementById("rule-add-ack");
  patternInput.disabled = kind === "always";
  if (kind === "always") {
    patternInput.value = "";
    patternInput.placeholder = "「整个工具」无需填写";
    hint.textContent = "整个工具：该工具的所有调用都会直接放行。";
  } else if (kind === "prefix") {
    patternInput.placeholder = "如 git status / click";
    hint.textContent = "前缀：以这段文本开头的调用都放行（命令类工具不含带 shell 拼接的命令）。";
  } else if (kind === "exact") {
    patternInput.placeholder = "与调用文本完全一致的内容";
    hint.textContent = "仅此一条：与这段文本完全一致的调用才放行。";
  } else {
    patternInput.placeholder = "如 docs/*.md";
    hint.textContent = "通配：按 * ? 通配符匹配（Windows 下大小写不敏感）。";
  }
  const warn = ruleAddWarning(tool, kind);
  if (warn) {
    warnEl.textContent = "⚠ " + warn.text;
    warnEl.classList.toggle("danger", !!warn.danger);
    warnEl.hidden = false;
  } else {
    warnEl.hidden = true;
  }
  const needAck = !!(warn && warn.danger);
  ackRow.classList.toggle("hidden", !needAck);
  if (!needAck) ack.checked = false;
}

function resetRuleAddForm() {
  document.getElementById("rule-tool").value = "";
  document.getElementById("rule-pattern").value = "";
  document.getElementById("rule-add-ack").checked = false;
  document.getElementById("rule-add-warn").hidden = true;
  document.getElementById("rule-add-ack-row").classList.add("hidden");
  updateRuleAddFormState();
}

async function submitRuleAdd() {
  const tool = document.getElementById("rule-tool").value.trim();
  const kind = document.getElementById("rule-kind").value;
  const pattern = document.getElementById("rule-pattern").value.trim();
  if (!tool) {
    showRulesStatus("请先填写工具名", null);
    return;
  }
  const warn = ruleAddWarning(tool, kind);
  if (warn && warn.danger && !document.getElementById("rule-add-ack").checked) {
    showRulesStatus("这条规则影响较大：请先勾选「我了解这条规则的影响」", null);
    return;
  }
  try {
    await request("whitelist.add", { tool, kind, pattern });
    document.getElementById("rule-add-form").classList.add("hidden");
    resetRuleAddForm();
    await renderSettings();
    showRulesStatus(
      `已添加规则：${tool} · ${RULE_KIND_LABEL[kind] || kind}${pattern ? " " + pattern : "（全部）"}`,
      null
    );
  } catch (err) {
    showRulesStatus("添加失败：" + err.message, null);
  }
}

function downloadJson(filename, obj) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

const ruleCheckRun = async () => {
  const tool = document.getElementById("rule-check-tool").value.trim();
  const text = document.getElementById("rule-check-text").value;
  const out = document.getElementById("rule-check-result");
  if (!tool) {
    out.textContent = "请先填写工具名";
    out.className = "rule-check-result warn";
    out.hidden = false;
    return;
  }
  try {
    const r = await request("whitelist.check", { tool, text });
    if (r.allowed) {
      const hit = r.hit || {};
      out.textContent = `✓ 会直接放行 —— 命中规则：${hit.tool} · ${RULE_KIND_LABEL[hit.kind] || hit.kind} ${hit.pattern || "（全部）"}`;
      out.className = "rule-check-result ok";
    } else {
      out.textContent = `✗ 需要确认 —— ${r.reason}`;
      out.className = "rule-check-result warn";
    }
    out.hidden = false;
  } catch (err) {
    out.textContent = "测试失败：" + err.message;
    out.className = "rule-check-result warn";
    out.hidden = false;
  }
};

async function renderSettings() {
  const cfg = await request("config.providers");
  const [{ rules }, snap] = await Promise.all([
    request("whitelist.list"),
    request("boot"),
  ]);
  bootSnap = snap;

  // —— 模型服务：列表视图 / 详情视图 ——
  providerCfg = cfg;
  if (detailOpen && cfg.providers[detailName]) renderProviderDetail(detailName);
  else if (detailOpen) closeProviderDetail(); // 配置项已被删掉 → 退回列表
  else renderProviderList();

  // —— 白名单 ——
  const ul = document.getElementById("rules-list");
  ul.innerHTML = "";
  const safetyByName = {};
  (snap.tools || []).forEach((t) => { safetyByName[t.name] = t.safety; });
  // 工具名候选：现役工具 ∪ 已有规则里出现过的工具（MCP 暂时掉线的名字也应能预置）
  const toolDl = document.getElementById("rule-tool-options");
  if (toolDl) {
    const names = new Set((snap.tools || []).map((t) => t.name));
    (rules || []).forEach((r) => names.add(r.tool));
    toolDl.innerHTML = [...names].sort()
      .map((n) => `<option value="${escapeHtml(n)}"></option>`).join("");
  }
  // 摘要：规则总数 + 整工具放行数量（后者是风险最直观的信号）+ 失效条数
  const summaryEl = document.getElementById("rules-summary");
  const wholeCount = (rules || []).filter((r) => r.kind === "always").length;
  const staleCount = (rules || []).filter((r) => r.stale).length;
  if (rules && rules.length) {
    summaryEl.textContent = `共 ${rules.length} 条规则`
      + (wholeCount ? `；其中 ${wholeCount} 条为「整个工具」放行` : "")
      + (staleCount ? `，${staleCount} 条已失效（悬停徽章查看原因）` : "");
    summaryEl.classList.toggle("risk", wholeCount > 0);
    summaryEl.hidden = false;
  } else {
    summaryEl.hidden = true;
  }
  (rules || []).forEach((r) => {
    const li = document.createElement("li");
    // 规则语义说清楚，否则用户会以为前缀规则能放行任何以它开头的调用：
    // 命令的前缀规则不覆盖 shell 拼接（`a; b` 要重新确认）；键盘 / 剪贴板 / 关窗口
    // 的「总是允许」只固化那一次的内容，不按动作整类放行。
    const kind = RULE_KIND_LABEL[r.kind] || r.kind;
    const extra = r.kind !== "prefix" ? ""
      : r.tool === "run_command" ? "（命令拼接不会命中）"
      : r.tool === "browser" ? "（同动作均放行：打开任意网址）"
      : r.tool === "mouse" ? "（同动作均放行：点任意位置）"
      : "（同动作均放行）";
    // 整工具放行且属写 / 高危工具：金色点提示（工具未知时不区分）
    const risky = r.kind === "always"
      && (safetyByName[r.tool] === "write" || safetyByName[r.tool] === "dangerous");
    const origin = ruleOriginNote(r);
    const age = fmtRuleAge(r.created_at);
    const tip = [kind + extra, origin].filter(Boolean).join("；");
    // 命中统计：帮用户看出哪些规则还在用、哪些可以清理
    const hits = r.hit_count > 0
      ? `<span class="rule-age" title="白名单放行累计 ${r.hit_count} 次，最近一次 ${fmtRuleAge(r.last_hit_at)}">命中 ${r.hit_count} 次</span>`
      : "";
    const enabled = r.enabled !== false;
    // 2.3.0 收紧后不再命中的历史遗留规则（后端 whitelist.list 打 stale 标记，
    // 判定与 gate 匹配侧同源）：徽章悬停显示原因——留着也不会再放行，建议删除
    const stale = r.stale
      ? `<span class="chip chip-warn rule-stale" title="${escapeHtml(r.stale_reason || "该规则已不再命中")}">已失效</span>`
      : "";
    li.innerHTML = `<span class="dot ${risky ? "warn" : "on"}"${risky ? ' title="整工具放行（写 / 高危）"' : ""}></span>
      <span class="rule-text${enabled ? "" : " rule-off"}" title="${escapeHtml(tip)}">${escapeHtml(r.tool)} · ${escapeHtml(kind)}${escapeHtml(extra)} ${escapeHtml(r.pattern || "(全部)")}${origin ? ` <span class="rule-origin">· ${escapeHtml(origin)}</span>` : ""}${enabled ? "" : ' <span class="rule-origin">· 已停用</span>'}</span>
      ${stale}
      ${hits}
      ${age ? `<span class="rule-age">${escapeHtml(age)}</span>` : ""}
      <label class="rule-toggle" title="${enabled ? "停用后保留配置，不再参与放行" : "重新启用这条规则"}">
        <input type="checkbox" data-rule-toggle="${r.id}" ${enabled ? "checked" : ""}>
      </label>
      <button class="rule-del">删除</button>`;
    li.querySelector("[data-rule-toggle]").onchange = async (e) => {
      try {
        await request("whitelist.enable", { id: r.id, enabled: e.target.checked });
        await renderSettings();
        showRulesStatus(e.target.checked ? "已启用规则" : "已停用规则（配置保留）", null);
      } catch (err) {
        showRulesStatus("操作失败：" + err.message, null);
        e.target.checked = !e.target.checked;
      }
    };
    li.querySelector(".rule-del").onclick = async () => {
      try {
        const label = ruleLabelText(r);
        await request("whitelist.remove", { id: r.id });
        await renderSettings();
        showRulesStatus(`已删除：${label}`, {
          label: "撤销",
          onClick: async () => {
            try {
              await request("whitelist.add", {
                tool: r.tool, kind: r.kind, pattern: r.pattern || "",
              });
              await renderSettings();
              showRulesStatus("已恢复规则", null);
            } catch (err) {
              showRulesStatus("恢复失败：" + err.message, null);
            }
          },
        });
      } catch (err) {
        showRulesStatus("删除失败：" + err.message, null);
      }
    };
    ul.appendChild(li);
  });
  if (!rules || !rules.length) {
    ul.innerHTML = '<li class="empty-hint">暂无规则（对话中选「总是允许」后会出现在这里，也可以点上方「＋ 添加规则」手动预设）</li>';
  }

  // —— 技能 ——
  // 列表渲染抽到 renderSkillList（技能页要重复用）；这里只负责总览摘要
  renderSkillSummary(snap.skills || []);
  if (skillManageOpen) renderSkillList(snap.skills || []);
  // 场景模板（打包内官方技能清单）：装完/删完技能后 renderSettings 会重跑，模板的已装徽标跟着刷新
  loadSceneTemplates().catch(() => {});

  // —— 内置工具 ——
  const tul = document.getElementById("settings-tool-list");
  tul.innerHTML = "";
  const safetyMeta = {
    readonly: ["只读", "safe-mark"],
    write: ["写入", "write-mark"],
    dangerous: ["高危", "danger-mark"],
  };
  (snap.tools || []).forEach((t) => {
    const [label, cls] = safetyMeta[t.safety] || [t.safety, ""];
    const li = document.createElement("li");
    li.innerHTML = `
      <span class="item-name" title="${escapeHtml(t.name)}">${escapeHtml(t.name)}</span>
      <span class="chip ${cls}">${escapeHtml(label)}</span>
      <span class="item-desc" title="${escapeHtml(t.description)}">${escapeHtml(t.description)}</span>`;
    tul.appendChild(li);
  });
  if (!(snap.tools || []).length) tul.innerHTML = '<li class="empty-hint">无工具</li>';
  // 折叠标题带上工具数（清单默认收起，展开查看全部）
  const toolFoldCap = document.getElementById("tool-list-fold-cap");
  if (toolFoldCap) toolFoldCap.textContent = `全部内置工具（${(snap.tools || []).length} 个）——点此展开 / 收起`;

  // —— MCP 服务 ——
  const mul = document.getElementById("settings-mcp-list");
  mul.innerHTML = "";
  renderMcpPresets(snap);
  // 按连接状态排序：失败/未连接置顶并标红，其次已连接，再连接中，最后已停用
  const mcpStatusRank = (m) => {
    if (m.enabled === false) return 3;
    if (m.connected) return 1;
    if (m.connecting) return 2;
    return 0;
  };
  [...(snap.mcp || [])].sort((a, b) => mcpStatusRank(a) - mcpStatusRank(b)).forEach((m) => {
    const li = document.createElement("li");
    if (m.enabled === false) li.classList.add("mcp-disabled");
    else if (!m.connected && !m.connecting) li.classList.add("mcp-failed");
    const toolChips = (m.tools || [])
      .map((t) => `<span class="chip">${escapeHtml(t)}</span>`)
      .join("");
    // 停用是「配置保留、只是不连接」：与删除（配置也删）区分开。
    // 连接已改为启动后台进行：连上前后端推送 mcp_updated 刷新这里
    const stateText = m.enabled === false
      ? "已停用"
      : (m.connected
        ? `已连接 · ${m.tools.length} 个工具`
        : (m.connecting ? "连接中…" : escapeHtml(m.error || "未连接")));
    li.innerHTML = `
      <div class="mcp-head">
        <span class="dot ${m.connected ? "on" : "off"}"></span>
        <span class="item-name">${escapeHtml(m.name)}</span>
        ${m.insecure_http ? '<span class="chip chip-warn" title="此服务走 http 明文且配置了鉴权头：凭据可能被中间人截获，建议改用 https 地址">⚠ http 明文携带鉴权头</span>' : ""}
        <span class="${m.connected ? "mcp-ok" : (m.connecting ? "mcp-pending" : "mcp-bad")}">${stateText}</span>
        <button class="btn-ghost mcp-toggle" title="${m.enabled === false ? "重新连接这个服务" : "停用：配置保留，工具从 Agent 移除"}">${m.enabled === false ? "启用" : "停用"}</button>
        ${m.enabled !== false && !m.connected && !m.connecting ? '<button class="btn-ghost mcp-reconnect" title="重连：重建全部 MCP 连接，处理连接失败的服务器">重连</button>' : ""}
        <button class="btn-ghost danger mcp-del" title="删除这个 MCP 服务">删除</button>
      </div>
      ${toolChips ? `<div class="mcp-tools">${toolChips}</div>` : ""}`;
    li.querySelector(".mcp-del").onclick = () => deleteMcpModal(m.name);
    const rcBtn = li.querySelector(".mcp-reconnect");
    if (rcBtn) {
      rcBtn.onclick = async () => {
        rcBtn.disabled = true;
        rcBtn.textContent = "重连中…";
        try {
          const r = await request("mcp.reconnect", {});
          const bad = (r.mcp_warnings || []).length > 0;
          mcpStatus(bad ? "已重连，但仍有服务未连上，详见列表" : "✓ 已重连", !bad);
        } catch (e) {
          mcpStatus("重连失败：" + e.message, false);
        }
        await renderSettings();
        boot();
      };
    }
    li.querySelector(".mcp-toggle").onclick = async () => {
      const enable = m.enabled === false;
      try {
        const r = await request("mcp.set_enabled", { name: m.name, enabled: enable });
        mcpStatus(r.hint || (enable ? "已启用" : "已停用"), enable);
      } catch (e) {
        mcpStatus((enable ? "启用" : "停用") + "失败：" + e.message, false);
      }
      await renderSettings();
      boot();
    };
    mul.appendChild(li);
  });
  if (!(snap.mcp || []).length)
    mul.innerHTML = '<li class="empty-hint">还没有 MCP 服务：点右上角「＋ 导入配置」粘贴一段配置，或「＋ 手动添加」逐个填</li>';

  // —— 关于 ——
  // 版本号只由下方更新面板展示（这里是项目身份与开源链接）；无项目态 working_dir
  // 为空串，要给可读回退而不是渲染出「工作目录：」后面一片空白
  document.getElementById("about-info").innerHTML = `
    <span>SkySheep 开源项目（${snap.frozen ? "安装版" : "源码版"}）·
      <a href="${REPO_PAGE}" target="_blank">项目主页</a> ·
      <a href="${REPO_PAGE}/blob/main/CHANGELOG.md" target="_blank">更新日志</a> ·
      <a href="${REPO_PAGE}/blob/main/LICENSE" target="_blank">MIT License</a></span>
    <span>工作目录：${snap.working_dir
      ? `<b class="copyable-path" title="点击复制">${escapeHtml(snap.working_dir)}</b>`
      : '<span class="dim">未设置（快聊模式，不读写文件）</span>'}</span>
    <span>配置文件：<b class="copyable-path" title="点击复制">${escapeHtml(cfg.config_path)}</b>（设置页保存的就是这个文件）</span>`;
  renderUpdatePanel(snap);
  renderWebsearchCfg().catch(() => {});
  renderImagegenCfg().catch(() => {});
  renderSpeechCfg().catch(() => {});
  renderRoundtableCfg().catch(() => {});
  renderAdversarialCfg().catch(() => {});
  renderSubagentCfg().catch(() => {});

  // —— 数据与隐私 ——
  document.getElementById("about-privacy").innerHTML = `
    <span>会话数据库：<b class="copyable-path" title="点击复制">${escapeHtml(cfg.db_path)}</b>（所有对话记录都存在这里）</span>
    <span>配置与技能：<b class="copyable-path" title="点击复制">${escapeHtml(cfg.home_dir)}</b>（config.toml、skills\\、mcp.json）</span>
    <span>API Key 只写入本机 config.toml，界面里也只显示打码后的片段。</span>`;

  // —— 设置导出 / 导入（换机迁移）——
  const io = document.getElementById("about-io");
  if (io && !io.dataset.bound) {
    io.dataset.bound = "1";
    const expBtn = io.querySelector("#btn-settings-export");
    const impBtn = io.querySelector("#btn-settings-import");
    const ioMsg = io.querySelector(".io-msg");
    expBtn.onclick = async () => {
      ioMsg.textContent = "打包中…";
      try {
        const r = await request("settings.export");
        downloadDataZip(r.filename, r.b64);
        ioMsg.textContent = "✓ 已导出：" + (r.included || []).join("、") +
          "。zip 内含明文 API Key，请妥善保管";
        ioMsg.className = "io-msg ok";
      } catch (e) {
        ioMsg.textContent = "✗ " + e.message;
        ioMsg.className = "io-msg bad";
      }
    };
    impBtn.onclick = () => {
      const inp = document.createElement("input");
      inp.type = "file";
      inp.accept = ".zip";
      inp.onchange = async () => {
        const f = inp.files[0];
        if (!f) return;
        ioMsg.textContent = "导入中…";
        try {
          const b64 = await new Promise((res, rej) => {
            const fr = new FileReader();
            fr.onload = () => res(String(fr.result).split(",")[1] || "");
            fr.onerror = () => rej(new Error("读取文件失败"));
            fr.readAsDataURL(f);
          });
          const r = await request("settings.import", { b64 });
          ioMsg.textContent = "✓ 已恢复：" + (r.restored || []).join("、") +
            (r.skipped && r.skipped.length ? "（跳过：" + r.skipped.join("、") + "）" : "") +
            "。配置已重载。";
          ioMsg.className = "io-msg ok";
          boot();
        } catch (e) {
          ioMsg.textContent = "✗ " + e.message;
          ioMsg.className = "io-msg bad";
        }
      };
      inp.click();
    };
  }
}

async function saveProvider(row, name, setDefault, kind) {
  const msg = row.querySelector(".pr-msg");
  msg.textContent = "保存中…";
  msg.className = "pr-msg";
  const get = (f) => {
    const el = row.querySelector(`input[data-f="${f}"]`);
    return el ? el.value.trim() : "";
  };
  const params = { name, set_default: setDefault };
  const model = get("model");
  const baseUrl = get("base_url");
  const apiKey = get("api_key");
  if (model) params.model = model;
  if (baseUrl) params.base_url = baseUrl;
  if (apiKey) params.api_key = apiKey;
  if (kind) params.kind = kind;
  const reasoningToggle = row.querySelector('input[data-f="supports_reasoning"]');
  if (reasoningToggle) params.supports_reasoning = reasoningToggle.checked;
  const visionToggle = row.querySelector('input[data-f="supports_vision"]');
  if (visionToggle) params.supports_vision = visionToggle.checked;
  // 上下文上限 / 温度：空值也要传（表示"清除该项，回到默认"）
  if (row.querySelector('input[data-f="context_limit"]')) {
    const cl = get("context_limit");
    params.context_limit = cl === "" ? 0 : Number(cl);
  }
  if (row.querySelector('input[data-f="temperature"]')) {
    params.temperature = get("temperature");  // 空串 = 清除
  }
  // 代理：始终传（字符串可为空 = 清除），后端校验格式
  const proxyInput = row.querySelector('input[data-f="proxy"]');
  if (proxyInput) params.proxy = proxyInput.value.trim();
  // 单价：表单里始终带着当前值，整组回传（空 = 0 不计价）；详情页之外的
  // 紧凑行没有这三格，不传即保持原值
  if (row.querySelector('input[data-f="price_in"]')) {
    for (const f of ["price_in", "price_out", "price_cache"]) {
      const v = get(f);
      params[f] = v === "" ? 0 : Number(v);
    }
  }
  try {
    const r = await request("config.save_provider", params);
    msg.textContent = "✓ 已保存" + (r.rebuilt ? "，使用中的模型已热更新" : "");
    msg.className = "pr-msg ok";
    if (!r.provider_error && r.rebuilt) {
      hideBanner();
      curProviderName = name;
      curModelName = r.model;
      setModelChip(`${name}/${r.model}`);
      refreshReasoning();
    }
    if (setDefault) {
      // 默认标记在标题上，必须重渲染才能看到；反馈放到状态行
      await renderSettings();
      providerStatus(`✓ 「${name}」已设为默认，下次启动就用它`);
    } else {
      // 重渲染：Key 掩码/徽章/协议等展示信息跟着更新（输入框里的明文 Key 也随之清空）
      await renderSettings();
      providerStatus(`✓ 已保存「${name}」` + (r.rebuilt ? "，使用中的模型已热更新" : ""));
    }
  } catch (e) {
    msg.textContent = "✗ " + e.message;
    msg.className = "pr-msg bad";
  }
}

// 切换当前对话使用的模型（立即生效，写进 agent.provider）
async function switchProvider(row, name) {
  const msg = row.querySelector(".pr-msg");
  msg.textContent = "切换中…";
  msg.className = "pr-msg";
  try {
    const r = await request("model.switch", { name });
    hideBanner();
    curProviderName = r.provider;
    curModelName = r.model;
    if (r.supports_vision !== undefined) curSupportsVision = r.supports_vision !== false;
    setModelChip(`${r.provider}/${r.model}`);
    await renderSettings();
    providerStatus(`✓ 已切换到「${name}」，当前对话立即生效`);
  } catch (e) {
    msg.textContent = "✗ " + e.message;
    msg.className = "pr-msg bad";
  }
}

// 添加自定义模型服务（表单在弹窗内，校验失败就地报错、不关窗）
function providerStatus(text, ok = true) {
  // 详情视图打开时状态要显示在详情里（列表视图此时是隐藏的）
  const el = detailOpen
    ? document.getElementById("provider-detail-status")
    : document.getElementById("provider-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = false;
  autoHideStatus(el, text, ok);
}

function addProviderModal() {
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">接入内置列表之外的服务：自建中转、公司内网网关、或其他厂商的 OpenAI 兼容接口。
    保存后写入本机 config.toml，可随时在列表里改或删除。</p>
    <div class="form-grid">
      <label>名称（英文标识，用于 config 与日志）
        <input data-f="name" placeholder="my-relay" autocomplete="off">
      </label>
      <label>协议类型
        <select data-f="kind">
          <option value="openai">openai（OpenAI 兼容，多数服务选这个）</option>
          <option value="anthropic">anthropic（Anthropic 原生协议）</option>
        </select>
      </label>
      <label class="wide">接口地址 base_url
        <input data-f="base_url" placeholder="https://your-relay.example.com/v1" autocomplete="off">
      </label>
      <label class="wide model-label">模型名（当前使用；点「检测」后可勾选多个一并启用）
        <span class="model-line">
          <input data-f="model" placeholder="glm-5.3 / deepseek-chat / …" autocomplete="off">
          <button class="btn-ghost detect" type="button" title="用上面填的地址和 Key 查询可用模型">检测</button>
        </span>
      </label>
      <div class="ap-pick hidden"></div>
      <label class="wide">API Key（可留空，用环境变量 &lt;名称大写&gt;_API_KEY 提供）
        <input data-f="api_key" type="password" autocomplete="off" placeholder="留空表示稍后配置">
      </label>
      <div class="form-status"></div>
      <label class="wide inline-check">
        <input data-f="use_now" type="checkbox" checked> 添加后立即切换到它（当前对话马上生效）
      </label>
      <label class="wide inline-check">
        <input data-f="set_default" type="checkbox"> 同时设为默认模型（下次启动也用它）
      </label>
    </div>`;
  const val = (f) => {
    const el = box.querySelector(`[data-f="${f}"]`);
    return el.type === "checkbox" ? el.checked : el.value.trim();
  };
  // 检测可用模型：勾选要一并启用的（可多选），上面的模型名 = 当前使用的那个
  const formStatus = box.querySelector(".form-status");
  const detectBtn = box.querySelector(".detect");
  const pickWrap = box.querySelector(".ap-pick");
  const checkedModels = () =>
    [...pickWrap.querySelectorAll("input:checked")].map((c) => c.value);
  detectBtn.onclick = async () => {
    detectBtn.disabled = true;
    formStatus.textContent = "检测中…正在向该服务查询模型列表";
    formStatus.className = "form-status";
    try {
      const r = await request("config.probe_models", {
        kind: val("kind"),
        base_url: val("base_url"),
        api_key: val("api_key"),
      });
      if (!r.count) {
        formStatus.textContent = "该服务没有返回任何模型（可能是接口地址不对，或该服务不提供模型列表）";
        formStatus.className = "form-status bad";
      } else {
        const cur = val("model");
        pickWrap.classList.remove("hidden");
        pickWrap.innerHTML =
          `<div class="ap-pick-head"><span>检测到 ${r.count} 个可用模型，勾选要一并启用的</span>` +
          `<span><button class="link-btn" type="button" data-a="all">全选</button>` +
          `<button class="link-btn" type="button" data-a="none">清空</button></span></div>` +
          `<div class="ap-list">` +
          r.models.map((m) =>
            `<label class="ap-row"><input type="checkbox" value="${escapeHtml(m)}"${m === cur ? " checked" : ""}>` +
            `<span class="mono">${escapeHtml(m)}</span></label>`).join("") +
          `</div>`;
        pickWrap.querySelector('[data-a="all"]').onclick = () =>
          pickWrap.querySelectorAll("input").forEach((c) => (c.checked = true));
        pickWrap.querySelector('[data-a="none"]').onclick = () =>
          pickWrap.querySelectorAll("input").forEach((c) => (c.checked = false));
        formStatus.textContent = `✓ 检测到 ${r.count} 个可用模型，勾选后随服务一并启用（当前模型始终启用）`;
        formStatus.className = "form-status ok";
      }
    } catch (e) {
      formStatus.textContent = "✗ " + e.message;
      formStatus.className = "form-status bad";
    } finally {
      detectBtn.disabled = false;
    }
  };
  showModal("添加自定义模型服务", box, async () => {
    const primary = val("model");
    // 一并启用 = 主输入框的当前模型 + 勾选项（去重保序，主模型在首位）
    const models = [primary, ...checkedModels().filter((m) => m !== primary)]
      .filter((m, i, a) => m && a.indexOf(m) === i);
    const r = await request("config.add_provider", {
      name: val("name"),
      kind: val("kind"),
      base_url: val("base_url"),
      model: primary,
      api_key: val("api_key"),
      set_default: val("set_default"),
      models: models.length > 1 ? models : undefined,
    });
    let switched = false;
    let switchErr = "";
    providerTab = "custom"; // 添加的是自定义服务：切到自定义页签，让新服务立即可见
    if (val("use_now")) {
      try {
        const s = await request("model.switch", { name: r.added });
        switched = true;
        hideBanner();
        curProviderName = s.provider;
        curModelName = s.model;
        setModelChip(`${s.provider}/${s.model}`);
      } catch (e) {
        switchErr = e.message;
      }
    }
    await renderSettings();
    boot();
    const cnt = `已启用 ${models.length} 个模型`;
    if (switched) providerStatus(`✓ 已添加「${r.added}」并切换使用，当前对话立即生效（${cnt}）`);
    else if (switchErr) providerStatus(`✓ 已添加「${r.added}」，但切换失败：${switchErr}（${cnt}）`, false);
    else if (r.activated) providerStatus(`✓ 已添加「${r.added}」，原先没有可用模型，已自动启用它（${cnt}）`);
    else providerStatus(`✓ 已添加「${r.added}」，${cnt}；需要用时点开它，在里面点「切换使用」`);
  }, "添加");
}

// 从列表里移除模型服务：自定义彻底删除；内置（及停用自定义）只是停用，可在下方恢复
function deleteProviderModal(name, isPreset) {
  const verb = isPreset ? "停用" : "删除";
  const box = document.createElement("div");
  box.innerHTML = isPreset
    ? `<p>确定停用内置服务 <b>${escapeHtml(name)}</b> 吗？</p>
       <p class="dim small">它会从列表里消失（配置保留，本机 config.toml 不再加载它），
       之后可以在列表下方的「已停用的服务」里点「恢复」找回来；恢复时按最新出厂默认重建，
       API Key 会保留。</p>`
    : `<p>确定删除自定义模型服务 <b>${escapeHtml(name)}</b> 吗？</p>
       <p class="dim small">只会从本机 config.toml 里移除这一项；内置服务与历史会话不受影响。</p>`;
  showModal(isPreset ? "停用内置服务" : "删除模型服务", box, async () => {
    const r = await request("config.delete_provider", { name });
    if (detailOpen && detailName === name) resetProviderView(); // 被删的就是当前详情 → 回列表
    await renderSettings();
    boot();
    const suffix = r.was_active
      ? "，请在列表里另选一个模型使用"
      : r.hidden
        ? "，可在下方「已停用的服务」里恢复"
        : "";
    providerStatus(`✓ 已${verb}「${name}」${suffix}`, !r.was_active);
  }, verb);
}
