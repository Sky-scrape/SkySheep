// app-skills.js —— 技能与 MCP（技能：导入 / 删除 / 本机候选；MCP：导入 / 手动添加 / 删除）
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules；
// 本文件只有函数声明，零加载期执行，按钮接线仍留在 app.js 原位（含
// bindLocalSkillToolbar() 顶层裸调用——app.js 加载期执行，故 index.html
// 先载本文件再载 app.js）；request / showModal 等助手是运行时调用。

// ---------- 技能：导入 / 删除 ----------

// 桌面窗口里能弹原生选择框（pywebview 桥）；浏览器模式下退回手动粘贴路径
function nativePickerAvailable() {
  return !!(window.pywebview && window.pywebview.api && window.pywebview.api.pick);
}

async function pickPath(kind) {
  if (!nativePickerAvailable()) return { paths: [], error: "no-picker" };
  try {
    return await window.pywebview.api.pick(kind);
  } catch (e) {
    return { paths: [], error: e.message };
  }
}

function skillStatus(text, ok = true) {
  const el = document.getElementById("skill-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 长名单截短：导入几十个技能时不能把 78 个名字全点名一遍，
// 只列前几个 + 「等 N 个」，全量名单在技能列表里本来就能看到
function nameList(names, max = 6) {
  if (names.length <= max) return names.join("、");
  return `${names.slice(0, max).join("、")} 等 ${names.length} 个`;
}

function mcpStatus(text, ok = true) {
  const el = document.getElementById("mcp-status");
  el.textContent = text;
  el.className = "card-status " + (ok ? "ok" : "bad");
  el.hidden = !text;
  autoHideStatus(el, text, ok);
}

// 内置常用 MCP 预设：每张卡点「＋ 添加」即写入配置并即时连接、工具立刻可用。
// 数据来自 boot 快照的 mcp_presets（后端 presets.py 单一事实源）。
function renderMcpPresets(snap) {
  const box = document.getElementById("settings-mcp-presets");
  if (!box) return;
  const presets = snap.mcp_presets || [];
  const installed = new Set(snap.mcp_installed || []);
  box.innerHTML = "";
  if (!presets.length) return;
  const head = document.createElement("div");
  head.className = "mcp-preset-head";
  head.textContent = "常用预设 · 点「＋ 添加」一键接入";
  box.appendChild(head);
  presets.forEach((p) => {
    const needMap = { uv: "需 uv（随 SkySheep 自带）", node: "需 Node.js", local: "需先安装本机程序" };
    // available 是后端启动时对本机运行时（npx/uvx/独立程序）的探测结果：
    // 缺运行时的卡直接置灰并说明缺什么，而不是点了添加才收到「启动命令不存在」
    const missing = p.available === false;
    const need = missing
      ? { uv: "未检测到 uv", node: "未检测到 Node.js", local: "未检测到本机程序" }[p.need]
      : (needMap[p.need] || "需 Node.js");
    const has = installed.has(p.name);
    const card = document.createElement("div");
    card.className = "mcp-preset-card" + (has ? " added" : "") + (missing ? " unavailable" : "");
    card.innerHTML = `
      <div class="mpc-top">
        <span class="mpc-label">${escapeHtml(p.label)}</span>
        ${p.readonly ? '<span class="chip safe-mark">只读</span>' : ""}
      </div>
      <div class="mpc-desc" title="${escapeHtml(p.desc)}">${escapeHtml(p.desc)}</div>
      <div class="mpc-foot">
        <span class="mpc-need" title="${escapeHtml(p.desc)}">${escapeHtml(need)}</span>
        <button type="button" class="btn-ghost mpc-add"${has || missing ? " disabled" : ""}>${has ? "✓ 已添加" : (missing ? "缺运行时" : "＋ 添加")}</button>
      </div>`;
    const btn = card.querySelector(".mpc-add");
    if (!has && !missing) {
      btn.onclick = async () => {
        btn.disabled = true;
        btn.textContent = "添加中…";
        let r;
        try {
          r = await request("mcp.add_preset", { name: p.name });
        } catch (e) {
          btn.disabled = false;
          btn.textContent = "＋ 添加";
          mcpStatus("添加失败：" + e.message, false);
          return;
        }
        await renderSettings();
        boot();
        const bad = (r.mcp_warnings || []).length > 0;
        mcpStatus(r.hint || (bad ? "已添加，但有服务没连上，详见列表" : "已添加"), !bad);
      };
    }
    box.appendChild(card);
  });
}

// 导入技能：优先弹系统选择框；浏览器里手动粘贴路径；也支持直接贴网址下载安装
function importSkillModal(prefill = "") {
  const box = document.createElement("div");
  const canPick = nativePickerAvailable();
  box.innerHTML = `
    <p class="dim small">三种装法任选其一：点「选择…」挑一个技能文件夹或 .zip 技能包；
    直接粘贴本机路径；或粘贴 <b>GitHub / Gitee 仓库链接</b>（也认 .zip 直链），程序会下载后自动安装。</p>
    <div class="form-grid">
      <label class="wide">技能文件夹 / .zip 路径
        <span class="model-line">
          <input data-f="source" placeholder="${canPick ? "点右边按钮选择，或直接粘贴路径" : "把技能文件夹或 .zip 的完整路径粘贴到这里"}" value="${escapeHtml(prefill)}" autocomplete="off">
          <button class="btn-ghost browse" type="button">选择…</button>
        </span>
      </label>
      <label class="wide">或从网址安装（GitHub / Gitee 仓库链接、.zip 直链）
        <input data-f="url" placeholder="https://github.com/用户名/仓库" autocomplete="off">
      </label>
      <label>装到哪里
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <div class="form-status"></div>
    </div>`;
  const input = box.querySelector('input[data-f="source"]');
  const urlInput = box.querySelector('input[data-f="url"]');
  const status = box.querySelector(".form-status");
  const browse = box.querySelector(".browse");
  if (!canPick) browse.title = "当前是浏览器模式，请手动粘贴路径";
  browse.onclick = async () => {
    const r = await pickPath("skill_dir");
    if (r.error) {
      // 原生框打不开时再试 .zip（有些环境选文件夹的实现不一样）
      const z = await pickPath("skill_zip");
      if (z.paths && z.paths.length) {
        input.value = z.paths[0];
        status.textContent = "已选择：" + z.paths[0];
        status.className = "form-status ok";
        return;
      }
      status.textContent = "打不开系统选择框，请手动粘贴路径（" + r.error + "）";
      status.className = "form-status bad";
      input.focus();
      return;
    }
    if (!r.paths || !r.paths.length) return; // 用户取消
    input.value = r.paths[0];
    status.textContent = "已选择：" + r.paths[0];
    status.className = "form-status ok";
  };
  showModal("导入技能", box, async () => {
    const source = input.value.trim();
    const url = urlInput.value.trim();
    if (source && url) throw new Error("两种方式选一种：本地路径 或 网址，别都填");
    if (!source && !url) throw new Error("请选择/粘贴技能路径，或粘贴要安装的网址");
    if (url) {
      status.textContent = "正在下载技能包，请稍候…";
      status.className = "form-status";
    }
    const r = await request("skills.install", {
      source: url || source,
      scope: box.querySelector('select[data-f="scope"]').value,
    });
    await renderSettings();
    boot();
    const where = r.scope === "project" ? "本项目" : "全局";
    skillStatus(`✓ 已导入 ${r.count} 个技能到${where}：${nameList(r.installed)}（已启用，可直接使用）`);
  }, "导入");
}

// 本机现存技能：只读探测 Claude Code / agents / Codex / 本项目 .claude 里已有的技能，
// 结果平铺在技能列表上方的面板里（不再弹窗），勾选后复用既有导入逻辑
// （skills.install 复制安装），探测本身不动任何文件
async function loadLocalSkills() {
  const panel = document.getElementById("skill-local-panel");
  if (!panel) return;
  const wasHidden = panel.hidden;
  panel.hidden = false;
  // 面板在列表上方，点按钮后滚到面板顶部，保证工具条与候选首行可见
  if (wasHidden) panel.scrollIntoView({ block: "nearest" });
  const list = document.getElementById("skill-local-list");
  const note = document.getElementById("skill-local-note");
  list.innerHTML = '<li class="empty-hint">正在探测本机技能目录（Claude Code / agents / Codex / 本项目 .claude）…</li>';
  note.textContent = "";
  let r;
  try {
    r = await request("skills.scan_local");
  } catch (e) {
    list.innerHTML = `<li class="empty-hint">✗ 探测失败：${escapeHtml(e.message)}</li>`;
    skillStatus("✗ 读取本机技能失败: " + e.message, false);
    return;
  }
  const cands = r.candidates || [];
  if (!cands.length) {
    list.innerHTML = `<li class="empty-hint">本机常见技能目录里没有找到技能。
      探测位置：~/.claude/skills、~/.agents/skills、~/.codex/skills、本项目 .claude/skills。
      技能在别处的话，用「＋ 导入技能」选文件夹，或粘贴路径 / GitHub·Gitee 链接安装。</li>`;
    note.textContent = "";
    return;
  }
  const doneCnt = cands.filter((c) => c.installed).length;
  note.textContent = `共 ${cands.length} 个${doneCnt ? `，其中 ${doneCnt} 个已装过（灰显不可选）` : "，均可导入"}`;
  list.innerHTML = cands.map((c) => `
    <li><label class="${c.installed ? "done" : ""}" title="${escapeHtml(c.path)}">
      <input type="checkbox" data-path="${escapeHtml(c.path)}" ${c.installed ? "disabled" : "checked"}>
      <span class="scan-name">${escapeHtml(c.name)}</span>
      <span class="scan-desc">${escapeHtml(c.description || "")}</span>
      <span class="scan-origin">${escapeHtml(c.origin)}</span>
    </label></li>`).join("");
}

// 面板里的全选 / 全不选 / 收起：前两个只作用于可选（未装过）的勾选框，
// 收起则整个面板藏起来（结果不销毁，再点「本机现存」重新探测）
function bindLocalSkillToolbar() {
  const panel = document.getElementById("skill-local-panel");
  if (!panel) return;
  panel.querySelectorAll(".scan-toolbar button").forEach((b) => {
    b.onclick = () => {
      if (b.dataset.act === "close") { panel.hidden = true; return; }
      panel.querySelectorAll(".scan-list input[type=checkbox]:not(:disabled)").forEach(
        (cb) => { cb.checked = b.dataset.act === "all"; });
    };
  });
}

// 导入面板里勾选的候选：逐个走 skills.install，单个失败不中断其余
async function importLocalSkills() {
  const panel = document.getElementById("skill-local-panel");
  const scope = document.getElementById("skill-local-scope").value;
  const picked = [...panel.querySelectorAll(".scan-list input[type=checkbox]:checked")]
    .map((x) => x.dataset.path);
  if (!picked.length) {
    skillStatus("✗ 先勾选要导入的技能", false);
    return;
  }
  const btn = document.getElementById("btn-skill-local-import");
  btn.disabled = true;
  const ok = [];
  const bad = [];
  for (const p of picked) {
    try {
      const res = await request("skills.install", { source: p, scope });
      ok.push(...res.installed);
    } catch (e) {
      bad.push(`${p.split(/[\\/]/).filter(Boolean).pop()}（${e.message}）`);
    }
  }
  btn.disabled = false;
  await renderSettings();
  boot();
  const where = scope === "project" ? "本项目" : "全局";
  if (ok.length) {
    skillStatus(`✓ 已导入 ${ok.length} 个技能到${where}：${nameList(ok)}（已启用，可直接使用）`);
    loadLocalSkills();  // 重新探测：刚装过的转为灰显，剩余候选一眼可见
  } else {
    skillStatus("✗ " + (bad[0] || "导入失败"), false);
  }
}

function deleteSkillModal(s) {
  const box = document.createElement("div");
  box.innerHTML = `<p>确定删除技能 <b>${escapeHtml(s.name)}</b> 吗？</p>
    <p class="dim small">会把技能文件夹从磁盘上删除（${s.source === "project" ? "本项目" : "全局"}技能目录），不可恢复。
    只是想临时停用的话，点它右边的「已启用」按钮即可。</p>`;
  showModal("删除技能", box, async () => {
    await request("skills.delete", { name: s.name });
    await renderSettings();
    boot();
    skillStatus(`✓ 已删除技能「${s.name}」`);
  }, "删除");
}

function batchDeleteSkillsModal(names) {
  const box = document.createElement("div");
  box.innerHTML = `<p>确定删除选中的 <b>${names.length}</b> 个技能吗？</p>
    <p class="dim small">会把这些技能文件夹从磁盘上删除，不可恢复：
    ${names.map((n) => `<span class="mono-path">${escapeHtml(n)}</span>`).join("、")}</p>`;
  showModal("批量删除技能", box, async () => {
    const bad = [];
    for (const n of names) {
      try {
        await request("skills.delete", { name: n });
      } catch (e) {
        bad.push(`${n}（${e.message}）`);
      }
    }
    await renderSettings();
    boot();
    skillStatus(bad.length ? `✗ 部分删除失败：${bad.join("、")}` : `✓ 已删除 ${names.length} 个技能`);
  }, "全部删除");
}

// /save-skill：把一次成功会话的做法一键存成技能草稿。
// 后端不调模型、从会话消息机械生成草稿，这里只负责让用户改完再保存；
// 保存走 skills.save_draft（与技能安装同一套落点校验），成功后技能立即生效。
// （放在本机候选区之后：上面的面板走「就地平铺不弹窗」的既有约定，这里是真弹窗）
async function saveSkillDraftModal() {
  if (!currentSessionId) {
    addNotice("当前还没有会话可保存：先发一条消息、跑完一轮任务再试。");
    return;
  }
  let d;
  try {
    d = await request("skills.save_from_session", { id: currentSessionId });
  } catch (e) {
    addNotice("生成技能草稿失败: " + e.message);
    return;
  }
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">已从当前会话机械提炼出草稿（不调模型）：目标取首条消息、
    步骤取实际用过的工具。${d.turn_count ? `共 ${d.turn_count} 轮有工具调用。` : "本会话没有工具调用，步骤要你自己补。"}
    下面各项都可以改，保存后立即生效，可在「MCP / Skills」页继续管理。</p>
    <div class="form-grid">
      <label>技能名（同时是技能目录名，不能含 / \\ : 等路径字符）
        <input data-f="name" value="${escapeHtml(d.name)}" autocomplete="off">
      </label>
      <label class="wide">描述（一句话，会显示在技能清单里）
        <input data-f="description" value="${escapeHtml(d.description)}" autocomplete="off">
      </label>
      <label class="wide">正文（给模型的操作指令；小节：目标 / 步骤 / 注意事项 / 适用边界）
        <textarea data-f="body" rows="14">${escapeHtml(d.body)}</textarea>
      </label>
      <label>保存到哪里
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <div class="form-status"></div>
    </div>`;
  const nameInput = box.querySelector('[data-f="name"]');
  showModal("把会话存为技能草稿", box, async () => {
    const name = nameInput.value.trim();
    if (!name) throw new Error("技能名不能为空");
    const description = box.querySelector('[data-f="description"]').value.trim();
    const body = box.querySelector('[data-f="body"]').value.trim();
    // frontmatter 在前端拼装：name / description 分别来自上面两个字段，
    // 保证保存名与 frontmatter 一致（后端也会再校验一遍）
    const content = `---\nname: ${name}\ndescription: ${description}\n---\n\n${body}\n`;
    const r = await request("skills.save_draft", {
      name,
      content,
      scope: box.querySelector('select[data-f="scope"]').value,
    });
    await renderSettings();
    boot();
    const where = r.scope === "project" ? "仅本项目" : "全局";
    addNotice(`✓ 已保存技能「${r.name}」（${where}），去「MCP / Skills」页查看。`);
  }, "保存草稿");
  nameInput.focus();
  nameInput.select();
}

// ---------- MCP：导入 / 手动添加 / 删除 ----------

// 导入 MCP：粘贴配置片段（或选一个 .json 文件）
function importMcpModal(prefill = "") {
  const box = document.createElement("div");
  const canPick = nativePickerAvailable();
  box.innerHTML = `
    <p class="dim small">粘贴一段 MCP 配置即可接入。支持 Claude Desktop 的
    <b>{"mcpServers": {...}}</b> 格式，也支持单个服务（记得加 <b>"name"</b> 字段）。</p>
    <div class="form-grid">
      <label class="wide">MCP 配置（JSON）
        <textarea data-f="snippet" rows="8" placeholder='{
  "mcpServers": {
    "fetch": { "command": "uvx", "args": ["mcp-server-fetch"] },
    "remote": { "url": "http://localhost:8000/mcp", "readonly": true }
  }
}'></textarea>
      </label>
      <label class="wide">或从文件导入
        <span class="model-line">
          <input data-f="path" placeholder="${canPick ? "点右边按钮选一个 .json 文件" : "粘贴 .json 配置文件的完整路径"}" value="${escapeHtml(prefill)}" autocomplete="off">
          <button class="btn-ghost browse" type="button">选择…</button>
        </span>
      </label>
      <label>存到哪个配置
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <label class="wide inline-check">
        <input data-f="overwrite" type="checkbox"> 覆盖同名服务（不勾选则跳过已存在的）
      </label>
      <div class="form-status"></div>
    </div>`;
  const pathInput = box.querySelector('input[data-f="path"]');
  const status = box.querySelector(".form-status");
  box.querySelector(".browse").onclick = async () => {
    const r = await pickPath("json");
    if (r.error) {
      status.textContent = "打不开系统选择框，请手动粘贴路径（" + r.error + "）";
      status.className = "form-status bad";
      return;
    }
    if (!r.paths || !r.paths.length) return;
    pathInput.value = r.paths[0];
    status.textContent = "已选择：" + r.paths[0];
    status.className = "form-status ok";
  };
  showModal("导入 MCP 服务", box, async () => {
    const snippet = box.querySelector('textarea[data-f="snippet"]').value.trim();
    const path = pathInput.value.trim();
    if (!snippet && !path) throw new Error("请粘贴 MCP 配置，或选择一个 .json 文件");
    const baseParams = {
      snippet,
      path,
      scope: box.querySelector('select[data-f="scope"]').value,
      overwrite: box.querySelector('input[data-f="overwrite"]').checked,
    };
    let r = await request("mcp.import", baseParams);
    // M10：含 stdio 定义时后端先回命令清单，确认后才写盘——连接即执行本机命令，
    // 必须让人看清楚要跑什么再继续
    if (r.needs_confirm) {
      // env 透明度（对抗审查联动项；字段契约：env 为「键 → 字符串值」的对象，
      // 由后端 pending_stdio_commands 下发）：stdio 服务的环境变量能改变目标程序
      // 的运行时行为（NODE_OPTIONS/PYTHONPATH 等），确认时必须逐键摆出来看。
      // confirm() 弹的是纯文本：值原样显示才忠实，不做 escapeHtml（纯文本无
      // 注入面，转义反而会把 & " 显示成实体）；换行压成空格防止借弹窗排版
      // 伪造文案，超长值只截断「展示」——确认导入的仍是完整值。没有 env 或
      // env 为空对象时，输出与原来完全一致。
      const lines = (r.pending || []).map((d) => {
        const full = [d.command, ...(d.args || [])].join(" ");
        let line = `  ${d.name}: ${full}`;
        const env = d.env && typeof d.env === "object" && !Array.isArray(d.env) ? d.env : null;
        const envLines = env ? Object.keys(env).map((k) => {
          const v = String(env[k] ?? "").replace(/\s+/g, " ").trim();
          return `      ${k}=${v.length > 200 ? v.slice(0, 200) + "…（已截断）" : v}`;
        }) : [];
        if (envLines.length) line += "\n      环境变量：\n" + envLines.join("\n");
        return line;
      }).join("\n");
      if (!confirm(
        "以下 MCP 服务会在连接时执行本机命令：\n" + lines +
        "\n\n请确认它们来自可信来源（继续 = 允许在本机运行上述程序）。"
      )) return;
      r = await request("mcp.import", { ...baseParams, confirmed: true });
    }
    await renderSettings();
    boot();
    const where = r.scope === "project" ? "本项目" : "全局";
    if (r.added.length) {
      const bad = (r.mcp_warnings || []).length;
      mcpStatus(
        `✓ 已导入到${where}：${r.added.join("、")}${bad ? "；有服务没连上，详情见列表里的红色提示" : "，已连接可直接使用"}`,
        !bad
      );
    } else {
      mcpStatus(`没有新增：${r.skipped.join("、")} 已存在。勾选「覆盖同名服务」再试即可替换。`, false);
    }
  }, "导入");
}

// 手动添加：分字段填写一个服务（常用服务请直接用上面的「常用预设」一键添加）
// 请求头文本 → 对象。格式约定为每行 `名称: 值`（冒号或中文冒号都认，
// 值里的冒号原样保留——Bearer token 里出现冒号不该被截断）。
function parseHeaderLines(text) {
  const out = {};
  for (const raw of (text || "").split(/\n+/)) {
    const line = raw.trim();
    if (!line) continue;
    const i = line.search(/[:：]/);
    if (i <= 0) continue;
    const name = line.slice(0, i).trim();
    const value = line.slice(i + 1).trim();
    if (name && value) out[name] = value;
  }
  return out;
}

function addMcpModal() {
  const box = document.createElement("div");
  box.innerHTML = `
    <p class="dim small">逐个填写一个 MCP 服务：本地命令填「启动命令 + 参数」，远程服务填「服务地址」，二选一。
    常用服务（网页抓取、Git 仓库等）已做成一键预设，在设置页上方的预设卡里添加即可。</p>
    <div class="form-grid">
      <label>服务名（英文标识）
        <input data-f="name" placeholder="fetch" autocomplete="off">
      </label>
      <label class="wide">启动命令 command（本地服务填这个）
        <input data-f="command" placeholder="uvx" autocomplete="off">
      </label>
      <label class="wide">命令参数 args（每行一个）
        <textarea data-f="args" rows="3" placeholder="mcp-server-fetch"></textarea>
      </label>
      <label class="wide">服务地址 url（远程服务填这个）
        <input data-f="url" placeholder="http://localhost:8000/mcp" autocomplete="off">
      </label>
      <label class="wide">请求头 headers（仅远程服务；每行一个 <code>名称: 值</code>）
        <textarea data-f="headers" rows="3" placeholder="Authorization: Bearer xxxxx"></textarea>
      </label>
      <label>单次调用超时（秒，留空默认 120）
        <input data-f="timeout" type="number" min="1" step="1" placeholder="120" autocomplete="off">
      </label>
      <label>存到哪个配置
        <select data-f="scope">
          <option value="global">全局（所有项目都能用）</option>
          <option value="project">仅本项目</option>
        </select>
      </label>
      <label class="wide inline-check">
        <input data-f="readonly" type="checkbox"> 这个服务的工具自动放行（只读类服务可勾选，跳过每次确认；服务自己声明会改数据的工具仍会逐次确认）
      </label>
      <p class="dim small" style="margin:4px 0 0">远程服务的 token（如 Bearer）过期时会报「鉴权失败」：回到这里更新 headers 重新保存即可。详见 docs/mcp-远程服务鉴权.md。</p>
      <div class="form-status"></div>
    </div>`;
  showModal("手动添加 MCP 服务", box, async () => {
    const val = (f) => box.querySelector(`[data-f="${f}"]`).value.trim();
    const args = val("args").split(/\n+/).map((s) => s.trim()).filter(Boolean);
    const headers = parseHeaderLines(val("headers"));
    const timeoutNum = Number(val("timeout"));
    const r = await request("mcp.save_server", {
      name: val("name"),
      command: val("command"),
      args,
      url: val("url"),
      headers,
      timeout: Number.isFinite(timeoutNum) && timeoutNum > 0 ? timeoutNum : 0,
      scope: box.querySelector('select[data-f="scope"]').value,
      readonly: box.querySelector('input[data-f="readonly"]').checked,
    });
    await renderSettings();
    boot();
    const st = (r.mcp || []).find((m) => m.name === val("name"));
    const bad = st && !st.connected;
    mcpStatus(
      bad
        ? `已保存「${val("name")}」，但没连上：${st.error || "未知错误"}`
        : `✓ 已添加「${val("name")}」${st ? `，已连接 ${st.tools.length} 个工具` : ""}`,
      !bad
    );
  }, "添加");
}

function deleteMcpModal(name) {
  const box = document.createElement("div");
  box.innerHTML = `<p>确定删除 MCP 服务 <b>${escapeHtml(name)}</b> 吗？</p>
    <p class="dim small">会从本机 mcp.json 里移除它并断开连接；它的工具会立即从 Agent 可用工具里消失。</p>`;
  showModal("删除 MCP 服务", box, async () => {
    const r = await request("mcp.delete", { name });
    await renderSettings();
    boot();
    mcpStatus(`✓ 已删除「${name}」并断开连接`);
  }, "删除");
}
