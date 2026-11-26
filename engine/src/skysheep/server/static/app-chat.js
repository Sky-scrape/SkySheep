// app-chat.js —— 聊天渲染（会话标签/贴底跟随/消息导航条挂点/输入区会话记忆/标签激活）
//              + 流式增量渲染（用户气泡/流式气泡/定稿切块/字符渐显/轮末定格）。
// 从 app.js 拆出（路线图第 3 批）；普通 script 全局函数互调，加载顺序见 index.html。
// 必须先于 app.js 加载：app.js 顶层引用本文件的绑定（chatBox 的 scroll 监听接线、
// updateCoopHint 启动调用经 activeTab 读协作模板名），后载即 TDZ 白屏；
// 本文件顶层只依赖 DOM 与 app-minimap.js（mainMinimap 实例），自包含。
// 共享助手 fmtTokens 仍留在 app.js（任务页签与 app-factory*.js 也用）；
// addNotice / renderHistory / 历史渲染系 / 页签拖动 / 分屏 / 侧栏仍在 app.js，事件期互调。
// ---------- 聊天渲染 ----------
const chatBox = document.getElementById("chat");

// ---------- 会话标签：多会话并行，每个标签一条独立聊天流 ----------
// 事件带 session_id 路由（routeTab），流式状态挂在各标签对象上；
// 后台标签继续接收事件，切回时内容与滚动位置都在。
let chatTabs = [];
let activeTab = null;
let routeTab = null;
let sessionMeta = {}; // sid -> {title}（refreshSessions / snapshot 维护，标签标题用）

function newTabObj(sid, title) {
  const logEl = document.createElement("div");
  logEl.className = "chat-log";
  // role=log（隐含 aria-live=polite）：新消息插入时屏幕阅读器播报，流式增量不打断
  logEl.setAttribute("role", "log");
  logEl.setAttribute("aria-label", "对话消息");
  // 贴底跟随标记：默认跟随；用户上翻（离开底部 40px 以上）即挂起，
  // 滚回底部 40px 内自动恢复。scrollLog 只读这个标记（见其注释）。
  logEl._followBottom = true;
  logEl.addEventListener("scroll", () => {
    logEl._followBottom = logNearBottom(logEl);
    updateJumpBottom();
  }, { passive: true });
  return {
    sid: sid || null, title: title || "", logEl, running: false,
    streamingEl: null, streamingText: "", rtCard: null, rtMemberEls: [],
    teamCard: null, // 团队进行中的团队卡（team_started 建立，事件实时驱动更新）
    lastAssistantText: "", usage: null, needsPerm: false, permData: null,
    needHistory: false, eta: null, etaEl: null,
    thinkMs: 0, thinkT0: 0,
  };
}
function tabFor(sid) { return sid ? chatTabs.find((t) => t.sid === sid) || null : null; }
function curTab() { return routeTab || activeTab; }
function curLog() { const t = curTab(); return t ? t.logEl : chatBox; }
// 分屏列初次无消息时插的「这个会话还没有消息。」空状态：任何真实内容
// （用户气泡 / 回复流 / 用时行）追加后就不该再留着，与辅助窗格 addUser 的
// empty.remove() 同一套语义
function dropEmptyHint(log) {
  if (!log || !log.querySelector) return;
  const empty = log.querySelector(".rp-empty");
  if (empty) empty.remove();
}
// 贴底跟随：用户上翻回看时暂停「新内容拽到底部」，滚回底部自动恢复。
// 判断必须在「追加内容之前」的滚动位置上记账——scrollLog 都在节点追加/长高之后
// 才被调用，那时 scrollHeight 已变大，现量必然误判成「已离开底部」、永远不再跟随。
// scroll 事件只在真实滚动时触发（DOM 追加长高不触发），所以在 scroll 监听里把
// 「此刻是否贴底」记到流元素上，scrollLog 只看标记。
// 阈值 40px：手指/触控板轻微回弹不算离开；超过即停跟随。浮标另按 120px 出，
// 中间这段（40–120px）既不拽底也不弹按钮，避免「刚上滑一点就冒按钮」。
const FOLLOW_BOTTOM_SLOP = 40;
const JUMP_BOTTOM_GAP = 120;
function logNearBottom(el) {
  return el.scrollTop + el.clientHeight >= el.scrollHeight - FOLLOW_BOTTOM_SLOP;
}
function logDistanceFromBottom(el) {
  return Math.max(0, el.scrollHeight - el.scrollTop - el.clientHeight);
}
function curLogEl() {
  const t = curTab();
  return (t && t.logEl) || chatBox.querySelector(".chat-log");
}
// 「跳至最新」浮标只在当前可见聊天流离底部超过 120px 时出现
function updateJumpBottom() {
  const btn = document.getElementById("jump-bottom");
  if (!btn) return;
  const t = curTab();
  const el = curLogEl();
  const far = !!el && logDistanceFromBottom(el) > JUMP_BOTTOM_GAP;
  const show = !!el && t === activeTab && el._followBottom === false && far;
  btn.hidden = !show;
  // html 初始态带着 class="hidden"（display:none !important，压过一切）：
  // 光切 hidden 属性摘不掉这个类，浮标会永远显示不出来，必须同步切类
  btn.classList.toggle("hidden", !show);
}
function scrollLog() {
  const t = curTab();
  // 分屏列是屏上可见的独立对话面：内容进列、不占主栏，照常跟随滚动；
  // 只有后台标签（看不见的页签）追加内容才不抢滚动
  if (t && t !== activeTab && !splitPanes.includes(t)) return;
  // 滚动发生在当前标签的 .chat-log 上（#chat 本体 overflow:hidden，滚它无效）
  const el = (t && t.logEl) || chatBox.querySelector(".chat-log");
  if (!el) return;
  if (el._followBottom === false) return; // 用户上翻回看：不抢位置（滚回底部自动恢复）
  el.scrollTop = el.scrollHeight;
  updateJumpBottom();
}
// 强制回到底部并恢复跟随：用户主动发送消息 / 点「跳至最新」浮标时用
function followLogBottom(el) {
  if (!el) return;
  el._followBottom = true;
  el.scrollTop = el.scrollHeight;
  updateJumpBottom();
}
document.getElementById("jump-bottom").onclick = () => followLogBottom(curLogEl());
function withTab(tab, fn) { routeTab = tab; try { fn(); } finally { routeTab = null; } }

function renderTabs() {
  const bar = document.getElementById("chat-tabs");
  // 常驻：单会话也显示（浏览器式页签，右侧带 ＋ 新建），不再「一个标签就藏起整条栏」
  bar.setAttribute("role", "tablist");
  bar.setAttribute("aria-label", "会话标签");
  bar.innerHTML = "";
  // 页签展示序：用户拖过（tab_order）按偏好序，没记过的按创建序追加在后面；
  // 空标签（无 sid，尚未落库）没有稳定 id、不参与排序，始终垫底
  const blanks = chatTabs.filter((t) => !t.sid);
  const bound = chatTabs.filter((t) => t.sid);
  const ordered = [];
  const seen = new Set();
  tabOrderPrefs.forEach((sid) => {
    const t = bound.find((x) => x.sid === sid);
    if (t) { ordered.push(t); seen.add(t.sid); }
  });
  bound.forEach((t) => { if (!seen.has(t.sid)) ordered.push(t); });
  ordered.push(...blanks);
  ordered.forEach((t, i) => {
    const el = document.createElement("div");
    el.className = "chat-tab" + (t === activeTab ? " active" : "") +
      (t.running ? " running" : "") + (t.needsPerm ? " needs-perm" : "");
    el.setAttribute("role", "tab");
    el.setAttribute("aria-selected", t === activeTab ? "true" : "false");
    el.tabIndex = t === activeTab ? 0 : -1; // roving tabindex：只有当前标签在 Tab 链上
    const title = t.title || (t.sid ? (sessionMeta[t.sid] || {}).title || "会话" : "新会话");
    // 序号只在窄屏（CSS ≤520px）露出来：手机上标题截得只剩两三个字、又没有
    // 悬停提示，同一句话开出来的几个会话标题本来就一样——序号是唯一的区分手段
    el.innerHTML = `<span class="tab-idx" aria-hidden="true">${i + 1}</span>` +
      `<span class="tab-title">${escapeHtml(title)}</span>` +
      (t.running ? '<span class="tab-dot" title="运行中"></span>' : "") +
      (t.needsPerm ? '<span class="tab-perm" title="等待你确认">🔒</span>' : "") +
      `<button class="tab-close" title="关闭标签${t.running ? "（运行中的会话会一并停止）" : ""}">✕</button>`;
    el.title = title + "（双击重命名）";
    el.onclick = (e) => {
      if (e.target.closest(".tab-close")) return;
      activateTab(t);
    };
    // 双击标签：就地改名（与侧栏会话行的行内改名同一套手感）。
    // 正在跑的也可以改：改名只写标题，不碰运行。
    el.ondblclick = (e) => {
      e.preventDefault();
      if (e.target.closest(".tab-close")) return;
      startTabRename(t, el);
    };
    el.oncontextmenu = (e) => {
      e.preventDefault();
      showTabMenu(t, { x: e.clientX, y: e.clientY });
    };
    el.querySelector(".tab-close").onclick = (e) => {
      e.stopPropagation();
      closeTab(t);
    };
    // 中键关闭：浏览器标签的肌肉记忆手势（auxclick 只认主键盘之外的键）
    el.onauxclick = (e) => {
      if (e.button === 1) { e.preventDefault(); closeTab(t); }
    };
    t.el = el; // 右键菜单/改名要找回这张标签的 DOM（renderTabs 每次重建，随渲染刷新）
    if (t.sid) wireTabDrag(el, t);
    bar.appendChild(el);
  });
  // 标签溢出（可横滚）时保证激活标签落在可视区：启动恢复/接管后台会话后
  // 激活的往往是最边上那张，不滚进来用户看不到当前会话是哪个。
  // 顺手把溢出态挂在 bar 上：CSS 据此画右缘渐隐，暗示右边还有
  const overflowing = bar.scrollWidth > bar.clientWidth;
  bar.classList.toggle("has-overflow", overflowing);
  if (activeTab && activeTab.el && overflowing) {
    activeTab.el.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  // Mods 状态托盘（实验性）：stat 片段随会话标签走，跨会话不串
  renderModTray();
  // 标签栏尾部的 ＋ 新建：浏览器式页签的固定收尾。与侧栏「新建会话」/ Ctrl+N
  // 同一个动作（newSessionFromHighlight：落在侧栏高亮的项目/快聊里），
  // 只是把入口放到用户视线所在的标签栏上
  let plus = bar.querySelector(".tab-new");
  if (!plus) {
    plus = document.createElement("button");
    plus.className = "tab-new";
    plus.type = "button";
    plus.title = "新建会话（Ctrl+N）";
    plus.setAttribute("aria-label", "新建会话");
    plus.textContent = "＋";
    plus.onclick = () => newSessionFromHighlight();
    bar.appendChild(plus);
  }
  // 方向键在标签间切换（激活即聚焦）；div 无原生键盘激活，Enter/空格补上。
  // bar 元素常驻，委托只绑一次
  if (!bar._navBound) {
    bar._navBound = true;
    bar.addEventListener("keydown", (e) => {
      const tabs = [...bar.querySelectorAll('[role="tab"]')];
      if (!tabs.length) return;
      const idx = tabs.indexOf(document.activeElement);
      if (e.key === "Enter" || e.key === " ") {
        if (idx >= 0) { e.preventDefault(); tabs[idx].click(); }
        return;
      }
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      if (idx === -1) return;
      e.preventDefault();
      const next = e.key === "ArrowRight" ? (idx + 1) % tabs.length : (idx - 1 + tabs.length) % tabs.length;
      tabs[next].focus();
      tabs[next].click();
    });
    // 溢出时有细滚动条，滚轮也直接横滚——标签栏不比抢一次鼠标位移的精度
    bar.addEventListener("wheel", (e) => {
      if (e.deltaY && bar.scrollWidth > bar.clientWidth) {
        bar.scrollLeft += e.deltaY;
        e.preventDefault();
      }
    }, { passive: false });
    // 双击空白处新建标签：浏览器页签的惯例手势（点在标签/＋上时不触发——
    // 它们自己消费 dblclick/onclick，冒泡上来时 target 不是 bar）
    bar.addEventListener("dblclick", (e) => {
      if (e.target === bar) newSessionFromHighlight();
    });
  }
  // 启动恢复：把当前标签集合与激活态写回 ui.json（下次启动回到同一现场）。
  // 放在 renderTabs 末尾是因为所有标签变动（开/关/接管/懒创建）最终都会走到这；
  // 只收有 sid 的标签，空标签不进恢复列表。偏好写失败静默。
  // 初次 boot 的恢复流程自身也会触发 renderTabs——与写回同一份偏好，无副作用。
  const tabSids = ordered.filter((t) => t.sid).map((t) => t.sid);
  const activeSid = activeTab && activeTab.sid ? activeTab.sid : null;
  const lastKey = (persistSessionTabs._last = persistSessionTabs._last || "");
  const sig = tabSids.join(",") + "|" + (activeSid || "");
  if (sig !== lastKey) {
    persistSessionTabs._last = sig;
    saveUiPrefs({ session_tabs: tabSids, session_active: activeSid || "" });
  }
}

/** 上次写回的签名：标签集合或激活态变了才再写（renderTabs 高频调用，
    不去重的话每次重画都落一次盘）。 */
persistSessionTabs._last = "";

function persistSessionTabs() { /* 由 renderTabs 内联执行（见上），保留名字供测试锢定 */ }

// 消息导航条实现已拆到 app-minimap.js（O5）：miniMsgsFor / createChatMinimap
// 与 MINI_* 常量的唯一事实来源在那里，这里只负责创建主栏实例并挂标签。

// 主栏那一条保留原 id；每个分屏列在 openSplitPane 里各建一条
const mainMinimap = createChatMinimap(chatBox, { id: "chat-minimap" });

function attachTabLog(tab) {
  // 只挂当前激活标签的聊天流；其余保留在内存里（各自滚动位置天然保留）
  if (!tab) return;
  if (tab.logEl.parentElement !== chatBox) {
    chatBox.querySelectorAll(":scope > .chat-log").forEach((el) => {
      if (el !== tab.logEl) el.remove();
    });
    chatBox.appendChild(tab.logEl);
  }
  mainMinimap.bind(tab.logEl);
  updateJumpBottom(); // 浮标跟着当前可见的聊天流走
}

// ---------- 主栏输入区状态按会话记忆 ----------
// mode（规划/执行）、rtOn/teamOn（一次性开关）、draft（草稿）、当前生效模型与
// 视觉能力、图片托盘与 & 引用，全部随会话存取——切标签不串状态（A 标签开的
// 规划模式、贴的图、写的草稿都不会随 B 发出去）。键是会话 sid；空标签共用
// "" 键，懒创建/接管时随标签换绑迁移。
const composerBySid = new Map();
// 引擎全局生效模型的镜像（boot 快照 / 全局切换时更新）：空标签没有会话私有
// 模型，徽章永远按它画。curProviderName 在主栏会话级切换时会被改写成该会话
// 的私有模型——空标签若从 cur 继承，显示的就不是实际发送所用模型（引擎全局）
let globalProviderName = "", globalModelName = "", globalSupportsVision = true;

function composerState(sid) {
  const key = sid || "";
  let st = composerBySid.get(key);
  if (!st) {
    // 新条目 = 默认态：模式与一次性开关不继承当前值（A 标签开的规划模式
    // 不能串到没去过的会话）；模型三件套继承当前全局显示（无覆盖的会话
    // 本来就跟随全局，model_get 落地后校正为会话生效值）
    st = {
      mode: "execute", rtOn: false, teamOn: false, draft: "",
      // 协作模板名（一次性，随本轮发送消费）：与 rtOn/teamOn 同生命周期
      template: "",
      providerName: curProviderName, modelName: curModelName,
      supportsVision: curSupportsVision, images: [], refs: [],
    };
    composerBySid.set(key, st);
  }
  return st;
}

/** 切入前调用：把输入区当前状态写回旧会话的条目。 */
function saveComposerState(sid) {
  const st = composerState(sid);
  st.mode = workMode;
  st.rtOn = rtOn;
  st.teamOn = teamOn;
  st.advOn = advOn;
  st.template = activeTab ? String(activeTab.template || "") : "";
  st.draft = inputEl.value;
  // 模型三件套只随会话条目记：空标签没有会话私有模型（显示跟随引擎全局，
  // 由全局镜像维护），把 cur 写回 "" 条目会把上个会话的私有模型串进去
  if (sid) {
    st.providerName = curProviderName;
    st.modelName = curModelName;
    st.supportsVision = curSupportsVision;
  }
  st.images = pendingImages;
  st.refs = pendingRefs;
}

/** 切入后调用：按新会话的条目恢复输入区（无条目 = 默认态）。
    顺序硬规定：先写 mode 再恢复 rt/team——setTeamOn 的「规划模式开着」
    提示按 workMode 当前值判定，顺序反了会在恢复时弹误报；silent 压掉
    互斥/联动的三处 notice（恢复不是用户操作，不该弹提示）。 */
function restoreComposerState(sid) {
  const st = composerState(sid);
  setWorkMode(st.mode || "execute");
  setRtOn(!!st.rtOn, true);
  setTeamOn(!!st.teamOn, true);
  setAdvOn(!!st.advOn, true);
  // 协作模板最后恢复：三个 setter 会清空它（手动切模式即弃用模板），顺序不能反
  if (activeTab) activeTab.template = String(st.template || "");
  if (sid) {
    curProviderName = st.providerName || "";
    curModelName = st.modelName || "";
    curSupportsVision = st.supportsVision !== false;
  } else {
    // 空标签：模型三件套取引擎全局镜像，不读 "" 条目——条目里可能是上个
    // 会话的私有模型（会话级切换只改 cur，引擎全局并没变），显示会串值
    curProviderName = globalProviderName;
    curModelName = globalModelName;
    curSupportsVision = globalSupportsVision;
  }
  pendingImages = Array.isArray(st.images) ? st.images : [];
  pendingRefs = Array.isArray(st.refs) ? st.refs : [];
  renderImageTray();
  renderRefTray();
  inputEl.value = st.draft || "";
  autoGrowInput(); // 草稿回填后重算输入框高度，否则残留上一会话内容的高度
  if (curProviderName) {
    setModelChip(curModelName ? `${curProviderName}/${curModelName}` : curProviderName);
  }
}

/** 空标签认领会话（懒创建/接管）："" 条目随标签换绑迁移到新 sid。 */
function migrateComposerSid(oldSid, newSid) {
  const prev = composerBySid.get(oldSid || "");
  if (!prev) return;
  const nk = newSid || "";
  if (!composerBySid.has(nk)) composerBySid.set(nk, prev);
  composerBySid.delete(oldSid || "");
}

// 按当前活动会话刷新模型徽章与会话生效模型缓存（切标签/懒创建认领共用）。
// 竞态防护：闭包捕获 tab，落地前 activeTab 已变（或回包 session_id 对不上）
// 就丢弃——快速连切标签时慢回包不会把别的会话的模型画到当前栏。
function refreshActiveSessionBadges(tab) {
  if (!tab || !tab.sid) return;
  const t = tab;
  request("session.model_get", { id: t.sid }).then((r) => {
    if (!r || activeTab !== t || (r.session_id && r.session_id !== t.sid)) return;
    const st = composerState(t.sid);
    st.providerName = r.name || "";
    st.modelName = r.model || "";
    st.supportsVision = r.supports_vision !== false;
    curProviderName = st.providerName;
    curModelName = st.modelName;
    curSupportsVision = st.supportsVision;
    setModelChip(st.modelName ? `${st.providerName}/${st.modelName}` : st.providerName);
  }).catch(() => {});
  refreshReasoning();
}

async function activateTab(tab) {
  if (!tab) return;
  focusMainPane(); // 切回主栏面：所有列变灰（没有列时是空操作）
  // 切标签/点会话：分组视图里「点组头」借走的高亮到此交回活动会话所在组
  // （没借过就不动，也不多画一下；早退路径补一次重画）
  if (groupedClickGk != null) {
    groupedClickGk = null;
    if (tab === activeTab) refreshSessions();
  }
  if (tab === activeTab) {
    attachTabLog(tab);
    // 已是活动标签也可能欠着历史：needHistory 被旁路设上后若只走这条早退，
    // 历史永远不来，用户盯着一面空墙。补拉一次再退（loadTabHistory 会消费标记）
    if (tab.needHistory && tab.sid && !tab.running) await loadTabHistory(tab);
    return;
  }
  // 查找高亮挂在旧标签的 DOM 上：切标签先收掉，避免 mark 残留计数错乱
  if (findHits.length || !document.getElementById("find-bar").classList.contains("hidden")) {
    closeFindBar();
  }
  // 会话记忆：切入前把输入区状态写回旧会话条目（必须在 activeTab 换指前读旧值）
  if (activeTab && activeTab !== tab) saveComposerState(activeTab.sid);
  activeTab = tab;
  attachTabLog(tab);
  renderTabs();
  hidePermission();
  currentSessionId = tab.sid;
  // 接力工作台顶部的会话名跟着活动会话走（右侧面板开着时切标签也要更新）
  // 接力看板跟着活动会话走（右侧面板开着时才重读）
  if (typeof relayDualOnSessionChange === "function") relayDualOnSessionChange();
  // 侧栏会话行选中态跟着活动标签走（与分组视图组头高亮同一来源）：
  // 否则从侧栏点开 A 会话再切到快聊标签，组头高亮动了、行高亮还留在 A
  activeSessionSid = tab.sid || null;
  restoreComposerState(tab.sid);
  // 模型/思考徽章按会话刷新（session.model_get / session.reasoning_get，
  // 带竞态校验；空标签保持全局口径不请求）
  refreshActiveSessionBadges(tab);
  // 后台标签从未渲染过历史（事件创建的）→ 拉一次历史；否则轻量激活。
  // 两种情形互斥：正在跑的标签不能去拉历史（会把流式内容盖掉），只做轻量激活。
  if (tab.needHistory && tab.sid && !tab.running) {
    await loadTabHistory(tab);
  } else if (tab.sid) {
    request("session.activate", { id: tab.sid }).catch(() => {});
  }
  if (tab.usage) setContextUsage(tab.usage.tokens, tab.usage.limit, tab);
  else setContextUsage(0, 0, tab); // 该标签还没发过消息：清掉读数并隐藏环，避免残留上一标签的数值
  // 路由指针跟着活动标签走：下面的权限卡恢复要靠 showPermission 里的
  // routeTab 分支判断，残留的上一个事件路由（常指向别的后台标签）会把
  // 恢复误判成「后台会话的确认请求」，卡片弹不出来。后续事件到达时会照常覆盖。
  routeTab = tab;
  if (tab.needsPerm && tab.permData) showPermission(tab.permData);
  // 会话变了，右侧面板里「跟会话绑定」的页跟着换数据：审查页取的是后端当前
  // 会话的检查点、任务清单取的是当前会话的 todo——不重拉就会拿上一个会话的
  // 内容冒充本会话（工具栏还写着「本会话中 Agent 改过的文件」）。上面的
  // activate / resume 与这两个请求同一条 WS 按序到达，后端看到的已是新会话。
  if (rightTabs.includes("review")) loadRightTab("review");
  if (rightTabs.includes("tasks")) loadRightTab(rightView.tasks);
  // 会话级活动团队状态对齐：其他窗口收的队、重启后的「已中断」标注，
  // 都靠 team.get 落到团队卡上（内存快照，顺带补齐历史里缺的卡）
  if (tab.sid) refreshTeamState(tab);
  refreshAcceptChip(); // 盾牌显示活动会话自己的档位（每会话独立）
  petPrevRunning = !!tab.running;
  petRefresh();
  refreshSessions();
}

function openTabForSession(sid, title, opts = {}) {
  // 接力在右侧面板与聊天并行显示，切换会话时保留面板并更新其会话上下文。
  // 同一会话只在一个面上打开：主栏要开它就把开着的那一列收掉（侧栏点行、
  // 通知跳转、列头 🔒 等路径都汇在这里）。不收的话事件按 tabFor 优先全进
  // 标签，列的运行点/输入锁再也没有事件来复位
  if (sid) closeSplitPanesForSids(new Set([sid]), { restore: false }); // 会话正要开进主栏，无须搬回
  let t = tabFor(sid);
  let adopted = false;
  if (!t) {
    // 先接管一张空占位标签，再考虑新建。启动时没有历史会话、标签全关、刚点过
    //「新建会话」都会留下一张空标签——不接管就会在旁边再叠一张，
    // 界面上看起来就是「点了一下 ＋，却冒出两个会话」。
    // 优先接管当前激活的那张：接管后台那张的话，激活的空标签还会赖在栏上。
    // 与 handleEvent 里「懒创建会话直接认领」是同一套做法。
    const blank = (activeTab && isBlankTab(activeTab)) ? activeTab : chatTabs.find(isBlankTab);
    if (blank) {
      // 接管前先把空标签上的实时输入区状态写回 "" 条目，再随换绑迁移——
      // 不写回的话，刚敲的草稿会被条目里的旧值顶掉
      if (blank === activeTab) saveComposerState("");
      t = blank;
      t.sid = sid;
      t.title = title || "";
      t.titleFixed = false; // 空标签上预命名的名字随标签换绑丢弃：它现在代表另一个会话
      migrateComposerSid("", sid); // 空标签的输入区状态条目随换绑迁移到新会话
      adopted = true;
    } else {
      t = newTabObj(sid, title);
      chatTabs.push(t);
    }
    t.needHistory = !opts.withMessages;
  }
  if (opts.withMessages) {
    renderHistory(t, opts.withMessages, opts.messagesOffset);
  }
  if (!opts.background) {
    // 接管来的标签常常就是当前激活那张，而 activateTab 对「已经是当前标签」
    // 会早退（它的语义是切标签）——这里补上接管必需的记账，否则标签名会一直
    // 停在「新会话」、currentSessionId 也不跟着走。
    if (adopted && t === activeTab) {
      attachTabLog(t);
      currentSessionId = t.sid;
      if (typeof relayDualOnSessionChange === "function") relayDualOnSessionChange();
      restoreComposerState(t.sid); // activateTab 早退了：输入区状态在这里恢复
      refreshActiveSessionBadges(t);
      renderTabs();
    }
    activateTab(t);
  } else renderTabs();
  // 历史同样要在这里补拉（原因同上）
  if (t.needHistory && t === activeTab) loadTabHistory(t);
  return t;
}

/** 空占位标签：没有绑定会话、也没在跑（启动时那张）。可以被新会话接管。
    跑着的无会话标签不算——它正懒创建会话，抢过来会劫持别人的运行。 */
function isBlankTab(t) { return !!t && !t.sid && !t.running; }

/** 拉一张标签的历史并渲染（needHistory 先消费掉，避免重入重复请求）。 */
async function loadTabHistory(tab) {
  if (!tab || !tab.sid || tab.running || !tab.needHistory) return;
  tab.needHistory = false;
  const before = tab.logEl ? tab.logEl.childElementCount : 0;
  try {
    const info = await request("session.resume", { id: tab.sid });
    // 请求期间这一栏已经追加了实时内容（用户赶在历史回来前就发了消息）：
    // 重画历史会把刚发出的消息和流式输出一起清掉——保留现状，历史下次打开再补
    if (tab.running || (tab.logEl && tab.logEl.childElementCount > before)) return;
    renderHistory(tab, info.messages || [], info.messages_offset);
  } catch (e) { addNotice("加载会话内容失败: " + e.message); }
}

// ---------- 流式增量渲染（第二板，拆自 app.js 流式区段；分区详注随板保留） ----------

function addUser(text, images, refs) {
  const d = document.createElement("div");
  d.className = "msg user";
  // 工具截图回填的伪 user 消息（agent 为让模型看截图而补的说明）不是用户发言：
  // 打标后不进右侧导航刻度，也不挂「编辑重发/引用」操作
  if (String(text || "").trimStart().startsWith("[screenshot]")) {
    d.classList.add("screenshot-note");
  }
  // 蓝色气泡画在内层 .user-bubble 上：操作按钮行要常驻占位在气泡下方（外层不再有底色）
  const bubble = document.createElement("div");
  bubble.className = "user-bubble";
  if (text) {
    // 行首的「> 」引用块（选中回答片段引用进来）渲染成样式化引用段，其余照旧纯文本
    const lines = String(text).split("\n");
    let qEnd = 0;
    while (qEnd < lines.length && (lines[qEnd] === ">" || lines[qEnd].startsWith("> "))) qEnd++;
    if (qEnd > 0) {
      const q = document.createElement("div");
      q.className = "user-quote";
      q.textContent = lines.slice(0, qEnd).map((l) => (l === ">" ? "" : l.slice(2))).join("\n");
      bubble.appendChild(q);
      const rest = lines.slice(qEnd).join("\n").replace(/^\n+/, "");
      if (rest) {
        const body = document.createElement("div");
        body.className = "user-text";
        body.textContent = rest;
        bubble.appendChild(body);
      }
    } else {
      bubble.textContent = text;
    }
  }
  (images || []).forEach((im) => {
    const img = document.createElement("img");
    img.className = "user-image";
    img.alt = "图片附件";
    if (im.data) {
      img.src = `data:${safeImageMediaType(im.media_type)};base64,${im.data}`;
    } else {
      // 历史图片是占位（boot/切会话不再整包 base64，见 session.image）：
      // 进入视口前后再按需拉取真身
      const t = curTab();
      img.dataset.imgSid = String(im.session_id || (t ? t.sid : "") || "");
      img.dataset.imgSeq = String(im.seq || "");
      img.dataset.imgIndex = String(im.index || 0);
      img.classList.add("img-pending");
      lazyImgObserve(img);
    }
    // 与工具截图同一套页内放大：点一下原地展开、再点收回，不顶层导航 data URL
    // （历史占位图还没拉回真身时没什么可放大，点了保持无反应）
    img.onclick = () => {
      if (!img.src || img.classList.contains("img-pending")) return;
      img.classList.toggle("zoom");
    };
    bubble.appendChild(img);
  });
  if (refs && refs.length) {
    const line = document.createElement("div");
    line.className = "ref-line";
    line.title = "引用对话的内容会随消息一起发给 Agent";
    line.textContent = "🔗 引用对话：" + refs.map((r) => r.title || "未命名会话").join("、");
    bubble.appendChild(line);
  }
  if (!text && images && images.length) d.classList.add("image-only");
  d.appendChild(bubble);
  const log = curLog();
  dropEmptyHint(log);
  log.appendChild(d);
  scrollLog();
}

function beginAssistant() {
  const t = curTab();
  // 正文开始前先把当前的思考行定格（否则行头的耗时/字数会停在「思考中…」）；
  // finishThinking 之后再把引用清掉，下一次思考另起一行
  finishThinking(t);
  t.streamingEl = document.createElement("div");
  t.streamingEl.className = "msg assistant";
  t.streamingEl.innerHTML = '<div class="md"><p></p></div>';
  const log = curLog();
  dropEmptyHint(log);
  log.appendChild(t.streamingEl);
  t.streamingText = "";
  t._streamBody = null; // 增量渲染状态槽随新气泡重建（见 renderStreamPart）
  t.thinkEl = null;
  t.thinkText = "";
  t.thinkMs = 0;
  t.thinkT0 = 0;
  t.thinkT1 = 0;
}

// ---------- 流式增量渲染 ----------
// 流式期间每 180ms 的重渲只对「未定稿尾块」做：已定稿前缀按段落（空行）切块，
// 且要求块内 ``` 围栏配对，保证每块单独 renderMarkdown 与整文渲染同构
// （本渲染器里列表/表格/引用块都止于空行，见 mdTables/mdLists）；块一定稿
// 就只渲染一次、不再动它，单次渲染成本从「全量文本」降到「尾块」，整轮流式
// 的渲染总开销从 O(n²) 降为线性。光标不在 DOM 里：正文容器带 streaming 类，
// 由 CSS 在最后一个块末尾生成（见 app.css 的 .md.streaming 段）。
// 找最靠后的可定稿切点：[from, cut) 以空行结尾且围栏配对；找不到返回 from。
function streamSealCut(text, from) {
  let end = text.length;
  for (;;) {
    const idx = text.lastIndexOf("\n\n", end - 1);
    if (idx < from) return from;
    const cut = idx + 2;
    const chunk = text.slice(from, cut);
    if ((chunk.match(/^[ \t]*```/gm) || []).length % 2 === 0) return cut;
    // 切点落在未闭合围栏里：回退到围栏开启行之前的空行再试
    const open = chunk.lastIndexOf("```");
    if (open < 0) return from;
    end = from + open;
  }
}
// 流式字符渐显：只给「本次新进入渲染结果」的字符包 span，旧字符保持纯文本。
// 依赖 st.tailRenderedLen（渲染后的字符数）而不是原文长度——markdown 会吃掉
// 标记符号，用原文偏移会错位。单次最多动画 600 字符，避免大段 delta 一次性
// 造几千个 span。错峰只占 3ms/字、封顶 30ms：整段淡入（30ms + .stream-ch 的
// var(--dur-fast)，140ms）必须在下一次重画（间隔见 appendStream，180ms）之前
// 播完——尾块每次重画都是重建 DOM，没播完的 span 会被直接拆掉，看起来就是
// 一截一截地跳出来。
function animateStreamChars(root, from) {
  const total = root.textContent.length;
  if (!Number.isFinite(from) || total <= from) return;
  const startAt = Math.max(from, total - 600);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      if (!n.nodeValue) return NodeFilter.FILTER_REJECT;
      const p = n.parentElement;
      if (p && p.closest("svg, script, style, .katex, .mermaid")) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  let acc = 0;
  while (walker.nextNode()) {
    const n = walker.currentNode;
    nodes.push({ node: n, start: acc });
    acc += n.nodeValue.length;
  }
  for (const item of nodes) {
    const val = item.node.nodeValue || "";
    if (item.start + val.length <= startAt) continue;
    const frag = document.createDocumentFragment();
    let local = 0, wrapped = false;
    for (const ch of val) {
      const g = item.start + local;
      if (g >= startAt && !/\s/.test(ch)) {
        const span = document.createElement("span");
        span.className = "stream-ch";
        span.textContent = ch;
        span.style.animationDelay = Math.min(g - startAt, 10) * 3 + "ms";
        frag.appendChild(span);
        wrapped = true;
      } else {
        frag.appendChild(document.createTextNode(ch));
      }
      local += ch.length;
    }
    if (wrapped) item.node.replaceWith(frag);
  }
}
// 把 [sealed, cut) 定稿进容器（st.tail 的前置位置，直接落在 .md 下，与定稿
// 整文渲染的 DOM 形状一致），再重画尾块。st 是 initStreamBody 的状态槽。
function renderStreamPart(st, text, sealed, cut) {
  if (cut > sealed) {
    st.tail.insertAdjacentHTML("beforebegin", renderMarkdown(text.slice(sealed, cut)));
  }
  st.tail.innerHTML = renderMarkdown(text.slice(cut));
  const rendered = st.tail.textContent.length;
  animateStreamChars(st.tail, Number(st.tailRenderedLen) || 0);
  st.tailRenderedLen = rendered;
  return cut;
}
// 光标不再单独造一个 <p>▍</p> 挂在末尾（它自己占一整行，定稿移除时气泡还会
// 突然矮一行）：给正文容器加 streaming 类，由 CSS 在最后一个块级子元素末尾
// 生成光标（见 app.css 的 .md.streaming 段）。定稿只需摘掉这个类。
function initStreamBody(md, withCursor) {
  md.innerHTML = "";
  const tail = document.createElement("div");
  tail.className = "stream-tail";
  md.appendChild(tail);
  if (withCursor) md.classList.add("streaming");
  return { tail, sealed: 0, tailRenderedLen: 0 };
}

function appendStream(txt) {
  const t = curTab();
  // 思考行默认就是收起的（点开看全文），正文开始不需要再折它
  if (!t.streamingEl) beginAssistant();
  t.streamingText += txt;
  // 节流渲染：delta 频率远高于人眼需要；定稿前缀不再逐 tick 重跑 markdown
  // （增量方案见上方说明），流结束由 finishAssistant 全量渲染兜底。
  // 间隔取 180ms（≥ 字符淡入 var(--dur-fast) 140ms + 错峰 30ms）：上一批
  // .stream-ch 一定播完，下一批重画才不会把它们拦腰拆掉。调 --dur-fast 时要
  // 同步这里（CSS 里这两处是一对，见 app.css 的 .stream-ch 段）
  if (!t._streamRenderTimer) {
    t._streamRenderTimer = setTimeout(() => {
      t._streamRenderTimer = null;
      if (!t.streamingEl) return;
      if (!t._streamBody) t._streamBody = initStreamBody(t.streamingEl.firstElementChild, true);
      const text = t.streamingText;
      t._streamBody.sealed = renderStreamPart(
        t._streamBody, text, t._streamBody.sealed, streamSealCut(text, t._streamBody.sealed));
      scrollLog();
    }, 180);
  }
}

function finishAssistant(rtMeta, seq, sources, tab) {
  // tab 只在「给非当前标签定稿」时传（如用户停止后按 queue_updated 收尾）；
  // 其余路径都走 curTab()，与事件路由保持同一个口径
  const t = tab || curTab();
  if (!t || !t.streamingEl) return;
  finishThinking(t);
  t.thinkEl = null;
  t.thinkText = "";
  if (t._streamRenderTimer) { clearTimeout(t._streamRenderTimer); t._streamRenderTimer = null; }
  // 定稿按整文精确重画一次（覆盖增量渲染的临时结构）；streaming 类先摘掉，
  // 光标随类消失——不占行高，气泡高度不会在定稿瞬间矮一行
  const mdEl = t.streamingEl.firstElementChild;
  mdEl.classList.remove("streaming");
  mdEl.innerHTML = renderMarkdown(t.streamingText);
  t._streamBody = null;
  addSourceChips(t.streamingEl, sources);
  if (rtMeta) {
    if (rtMeta.mode === "adversarial") {
      // 对抗报告：徽标 + 折叠上方的问题清单卡（报告已流进本气泡）
      addAdvBadge(t.streamingEl, rtMeta);
      t.streamingEl._rtMeta = rtMeta; // 重新生成时据此重跑同样的对抗配置
      rtMeta.seq = seq;
      t.advSeq = seq;
      t.advMeta = rtMeta;
      advAttachTransfer(t, rtMeta, seq);
      if (t.advCard && t.advCard.isConnected && !t.advCard.classList.contains("folded") && t.advFold) {
        t.advFold();
      }
    } else {
      addRtBadge(t.streamingEl, rtMeta);
      t.streamingEl._rtMeta = rtMeta; // 重新生成时据此重跑同样的圆桌配置
      // 融合结论已就位：自动折叠成员草稿卡（此前一直展开，占着大块空白）；标题栏随时可展开回看
      const rtEls = t.rtMemberEls || [];
      const allSettled = rtEls.length &&
        rtEls.every((x) => x.card.classList.contains("ok") || x.card.classList.contains("err")
          || x.card.classList.contains("skipped"));
      if (t.rtCard) {
        // 对照卡底部说明：A/B 与圆桌的结论去向不同，但不推断共识/分歧
        const note = t.rtCard.querySelector(".rt-note");
        if (note) {
          note.textContent = rtMeta.mode === "compare"
            ? "对照各方观点，展开全文查看完整回答；所选模型的回答见下方。"
            : "对照各方观点，展开全文查看完整回答，融合结论见下方回答。";
        }
        // 自动折叠后展开时用的副标题（rtMemberFinished 的进度文案已过时）
        t.rtCard._rtSubText = rtMeta.mode === "compare"
          ? `${rtEls.length} 个模型  已定稿  展开查看各成员草稿`
          : `${rtEls.length} 个模型  草稿完成，融合结论见下方回答`;
      }
      if (rtMeta.members && allSettled && t.rtCard && !t.rtCard.classList.contains("folded")) {
        t.rtCard.classList.add("folded");
        const foldBtn = t.rtCard.querySelector(".rt-fold");
        if (foldBtn) foldBtn.textContent = "展开";
        if (foldBtn) foldBtn.setAttribute("aria-expanded", "false");
        const sub = t.rtCard.querySelector(".rt-sub");
        if (sub) sub.textContent = `${rtEls.length} 个模型  已折叠  点击标题栏展开查看各成员草稿`;
      }
      // 融合结论已随本气泡落库（seq 就位）：挂「转为团队工单」入口（P5）
      const rtEx = rtToTeamActions(rtMeta, seq, t);
      if (rtEx && t.rtCard && t.rtCard.isConnected) t.rtCard.appendChild(rtEx);
    }
  }
  t.lastAssistantText = t.streamingText;
  const finalEl = t.streamingEl;
  t.streamingEl = null;
  t.streamingText = "";
  if (seq) finalEl.dataset.seq = String(seq);
  renderMermaidIn(finalEl);
  highlightCodeIn(finalEl);
  decorateFinalMessage(finalEl, t, () => t.lastAssistantText, seq);
}

// 用户按「停止」的那一轮：实时终态要跟落库终态一致。
// 取消路径不会再有 assistant_message / turn_finished（引擎在 yield 处被取消），
// 前端若就这么挂着流式气泡，光标（.md.streaming 的 ▍）会一直闪，看起来还在写；
// 而引擎在收尾时已把产出的正文连同「…（已停止）」注记并入历史
// （core/agent.py 的 _persist_partial_stream_text）——刷新后正文就多出这句，
// 实时与落库对不上。这里按同一口径就地定稿：补注记 + 摘 streaming 类。
// 调用时机：queue_updated(pending=0)（后端运行态的唯一事实源，取消后必发）。
function finishStoppedTurn(t) {
  if (!t) return;
  t._stopRequested = false;
  if (!t.streamingEl) return; // 这一轮已正常定稿（或还没产出正文气泡）
  if (t._streamRenderTimer) { clearTimeout(t._streamRenderTimer); t._streamRenderTimer = null; }
  // 一个字都还没产出就停了：空气泡没有信息量，直接摘掉（思考行已定格）
  if (!t.streamingText.trim()) {
    finishThinking(t);
    t.streamingEl.remove();
    t.streamingEl = null;
    t.streamingText = "";
    t._streamBody = null;
    return;
  }
  t.streamingText += "\n\n…（已停止）";
  finishAssistant(undefined, undefined, undefined, t);
}
