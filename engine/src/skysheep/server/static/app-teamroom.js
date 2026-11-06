// ---------- 团队室（独立页签）：成员带  频道消息流  工单板 三分区同屏 ----------
// 设计与落点见 docs/团队模式设计.md §15。定位：把聊天流内团队卡的三块内容
// 提升为右侧面板的独立页签，两者同源渲染、双向即时同步——渲染全部复用团队卡
// 的 tc* 系函数（app.js），本文件只提供同形 _ui 的容器、页签装载（loadTeamRoom）
// 与五事件的转发（trRoomFeed，app.js 的团队事件分支在喂团队卡的同时喂这里）。
// 数据面：team.room 聚合探针（激活/切会话/重连各调一次）+ team.log 全量回放；
// 未读 badge 照设计 §15.4：last_seen_seq 按 team_id 随 saveUiPrefs 既有机制落
// ui.json，badge = latest_seq − last_seen_seq——team.room 专门带回的 latest_seq
// 是权威水位，刷新/断线重连空档的消息由它兜住，不靠内存计数；进页签即清零。
// 三条补线（第五批评审）：后台定稿按 team_id+seq 直接对水位记账、不依赖挂卡
// （空态/错误态/后台起队无容器照样计数）；起队即探针校准 trTeamId/trLatestSeq
// （眼前看过的消息即时推已见水位，切走不整表重计）；已见水位合并落盘（落盘前
// 重读服务端现值只并本队键，--browser 多页签并存不互相回退条目）。
// 写操作入口是团队卡既有 handler 原样可用，不新增写路径。

let trCard = null;  // 现役团队室容器（teamcard 形状，_ui 与团队卡同形）
let trSid = null;   // 容器对准的会话（页签内容永远对准当前会话，不跨会话缓存）
let trTeamId = "";  // 页签当前对准的团队（team.room 快照带回；后台起的新队先置空）
let trLatestSeq = 0; // 对准团队的频道最新 seq（team.room 权威值 + 后台定稿事件增量）
let trSeenMap = null; // ui.json team_last_seen 镜像 {team_id: last_seen_seq}（页签装载时拉取）
// 起动校准代次（§15.4 回归②）：team_started 不带 team_id，起队后探一次
// team.room 把 trTeamId/trLatestSeq 补齐；再次起队或权威校准（trTrackRoom）
// 都让在途探针作废，迟到的回包不回写旧队伍的水位
let trCalGen = 0;
// 已见水位落盘链（§15.4 回归③）：trMarkSeen 的合并写在链上逐条串行，
// 同页不并发读改写、后一条总在前一条落定后重读服务端现值
let trSeenFlushChain = Promise.resolve();

function trRoot() {
  return document.getElementById("teamroom-root");
}

// 页签此刻是否真的在眼前：打开着、面板没收起、激活的也是它
function trTabVisible() {
  return typeof rightViewVisible === "function" && rightViewVisible("teamroom");
}

// 页签标题旁的未读数（app.js 的 renderRightPanel 画页签时读取）：
// badge = latest_seq − last_seen_seq（§15.4），页签在眼前恒 0
function trTabBadge() {
  if (trTabVisible()) return 0;
  return Math.max(0, trLatestSeq - trSeenFor(trTeamId));
}

// ui.json 的 team_last_seen 镜像（§15.4：last_seen_seq 按 team_id 持久化）。
// 页签每次装载都重拉：--browser 多页签并存时别的页签可能推进过水位，本页
// 旧镜像只会把别人已见的算成未读；拉不到沿用本页旧镜像，再不行 badge 全计
//（只在持久化读取失败这条坏路径上出现，且进页签后必然重校准）
async function trSeenLoad() {
  const r = await request("ui.get").catch(() => null);
  const raw = r && r.prefs && r.prefs.team_last_seen;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) trSeenMap = raw;
  return trSeenMap;
}

function trSeenFor(teamId) {
  return (trSeenMap && Number(trSeenMap[teamId])) || 0;
}

// 已见水位推盘（§15.4：随 saveUiPrefs 既有机制落 ui.json，原子写在后端）。
// ui.save 对 team_last_seen 键内是整键替换，--browser 多页签并存时后写页签的
// 旧镜像整表会把先写页签刚推进的条目回退（回归③）——落盘前重读服务端现值，
// 读-改-写只并进本队键：别队的条目原样保留，本队别人已推得更远就不动盘。
// 镜像未拉到也照推（合并写不依赖整表，旧实现整表替换才要求镜像齐全）
function trMarkSeen(teamId, seq) {
  seq = Number(seq) || 0;
  if (!teamId || seq <= 0) return;
  if (trSeenFor(teamId) >= seq) return; // 同值不重发（badge 清零只在水位真前进时发生一次）
  if (!trSeenMap) trSeenMap = {};
  trSeenMap[teamId] = seq;
  trSeenFlushChain = trSeenFlushChain
    .then(() => trSeenFlush(teamId))
    .catch(() => {});
}

async function trSeenFlush(teamId) {
  const seq = trSeenFor(teamId); // 链上执行时镜像已是本页最新值
  if (seq <= 0) return;
  const r = await request("ui.get").catch(() => null);
  const raw = r && r.prefs && r.prefs.team_last_seen;
  const merged = (raw && typeof raw === "object" && !Array.isArray(raw))
    ? Object.assign({}, raw) : {};
  if ((Number(merged[teamId]) || 0) >= seq) return; // 别的页签已推得更远：不动盘
  merged[teamId] = seq;
  saveUiPrefs({ team_last_seen: merged });
}

// team.room 探针回来后的水位校准（§15.4）：latest_seq 以服务端为准——刷新/
// 断线重连空档的消息由它兜住；页签在眼前才推 last_seen（进室即清零），
// 在后台只抬水位让 badge = latest − last_seen 自然长大。无活动团队时
// teamId 传空串、latest 传 0：badge 无从谈起，等下一支队伍。
// 权威校准落在盘面上就作废在途的起动校准探针（trCalGen 前进）
function trTrackRoom(teamId, latestSeq, sid) {
  trCalGen++;
  trSid = sid || "";
  trTeamId = String(teamId || "");
  trLatestSeq = Number(latestSeq) || 0;
  if (trTabVisible()) trMarkSeen(trTeamId, trLatestSeq);
  trSyncBadge();
}

// 起动校准（§15.4 回归②）：team_started 事件不带 team_id，起队把 trTeamId
// 重置成空串——页签眼前的起队在此之后推不了已见水位（trMarkSeen("") 是空
// 操作）、trLatestSeq 也停在 0，用户看着消息进来再切走，第一条后台定稿按
// seq−0 算成全量未读。起队后立即探一次 team.room 把 team_id 与 latest_seq
// 校准回来：眼前在看的消息即时推已见水位，切走后 badge 只剩真正没看的。
// 后台起队同样受益——常驻团队复用同队 id 续频道，已见水位从上轮落盘值起算，
// badge 只计本轮新增。水位只进不退：探针在途时定稿事件可能已把 seq 抬得
// 更高（服务端 WS 单连接 FIFO，回包不早于发出前的事件，max 只防乱序抵达）
function trCalibrateStarted(sid) {
  const gen = ++trCalGen;
  const sideEpoch = trSideEpoch; // 侧栏角标同批校准：在途时团队先行终态则不作数
  request("team.room", sid ? { session_id: sid } : {})
    .then((r) => {
      // 迟到的回包不作数：期间又起队或权威校准（trTrackRoom）已接手
      if (gen !== trCalGen || trTeamId) return;
      if (sideEpoch !== trSideEpoch) return; // 团队已终态：侧栏角标的活动对齐不作数
      if (!r || !r.active || !r.team) return;
      trSid = sid || "";
      trTeamId = String(r.team.team_id || "");
      const latest = Number(r.latest_seq) || 0;
      if (latest > trLatestSeq) trLatestSeq = latest;
      if (trTabVisible()) trMarkSeen(trTeamId, trLatestSeq);
      trSyncBadge();
      trSideAlign(r.team, sid || ""); // 侧栏角标（§15.3.5）：起队即对齐，页签没开也计数
    })
    .catch(() => { /* 探针失败：等下一次装载（loadTeamRoom）的权威校准兜底 */ });
}

// 后台定稿事件（team_message）的 seq 增量：只补两次探针之间的空档，权威
// 水位仍以 team.room 为准。同会话才计数——页签对准当前会话，别的会话的
// 频道 seq 是另一条数轴，混进来只会把差值算错
function trTrackEventSeq(seq) {
  seq = Number(seq) || 0;
  if (seq > trLatestSeq) {
    trLatestSeq = seq;
    trSyncBadge();
  }
}

function trSyncBadge() {
  const btn = document.querySelector('.rp-tab[data-tab-id="teamroom"]');
  if (!btn) return;
  const n = trTabBadge();
  let b = btn.querySelector(".rp-tab-badge");
  if (!n) {
    if (b) b.remove();
    return;
  }
  if (!b) {
    b = document.createElement("span");
    b.className = "rp-tab-badge";
    btn.insertBefore(b, btn.querySelector(".rp-x"));
  }
  b.textContent = String(n);
}

// ---------- 侧栏「团队」常驻入口角标（§15.3.5，常驻团队 T3.2 落地后增设） ----------
// 侧栏项点击开团队室页签（app.js 导航分发 data-act="teamroom"，展开态 .nav-item
// 与折叠栏 #rail-team 两处入口）；角标计「活动团队数」＝常驻登记的团队 + 当前
// 会话进行中的团队（去重）。数据面纯前端、不新增后端方法：
// · 当前会话进行中的团队以 team.get / team.room 的权威回包对齐——切会话的
//   refreshTeamState、团队室装载 loadTeamRoom、起队校准 trCalibrateStarted
//   三处顺路喂入，不另发探针；
// · 跨会话的锚是常驻登记册 ~/.skysheep/team_resident.json 的 team_id 集合——
//   后端有意不给枚举入口（resident_get 只按 id 探询，dispatch 仅本机），本页
//   维护镜像（localStorage，沿 skysheep.team.members 既有先例）：凡从权威信号
//   （快照/回包的 resident 标志、resident_get 探询、set/clear 回包）得知某队
//   的登记真值就并入，正负都收，镜像向册子收敛。册子里本页从未探过的登记
//   计数不到（少计不虚计），下次探到即补上；多开窗口各自持镜像，同以权威
//   信号收敛（--browser 跨端口属不同 origin，各自重收敛）。

const TR_SIDE_LS_KEY = "skysheep.team.resident_ids";
let trResidentIds = null; // 常驻登记镜像（Set<team_id>，懒加载自 localStorage）
let trSideSid = "";       // 角标对齐的会话（切会话/装载时由权威回包喂入）
let trSideTeamId = "";    // 该会话进行中的团队（权威回包的 team_id）
let trSidePending = false; // 起队事件已见、team_id 未校准（探针回包前也即时 +1）
let trSideEpoch = 0;      // 终态代次：活动对齐在途时团队先行终态，迟到的校准不作数

function trSideLoad() {
  if (trResidentIds) return trResidentIds;
  trResidentIds = new Set();
  try {
    const raw = JSON.parse(localStorage.getItem(TR_SIDE_LS_KEY) || "[]");
    if (Array.isArray(raw)) {
      raw.forEach((id) => { id = String(id || "").trim(); if (id) trResidentIds.add(id); });
    }
  } catch (e) { /* 坏数据不致命：按空镜像起，下次权威信号重收敛 */ }
  return trResidentIds;
}

function trSideSave() {
  try { localStorage.setItem(TR_SIDE_LS_KEY, JSON.stringify([...trResidentIds])); } catch (e) {}
}

// 权威常驻真值并入镜像（正负都收：登记了收进来、确认解除了剔出去）并同步角标
function trSideNote(teamId, resident) {
  teamId = String(teamId || "").trim();
  if (!teamId) return;
  const ids = trSideLoad();
  if (resident ? ids.has(teamId) : !ids.has(teamId)) return; // 无变化不落盘
  if (resident) ids.add(teamId); else ids.delete(teamId);
  trSideSave();
  trSyncSideBadge();
}

// 会话级活动团队对齐（team.get / team.room 权威回包）：team 非空 = 该会话有
// 进行中的团队（顺路并入常驻真值）；null = 没有（清位）。调用方只在对齐时刻
// 的当前会话上调（refreshTeamState / loadTeamRoom 各自挡住过期回包）
function trSideAlign(team, sid) {
  sid = String(sid || "");
  if (!sid) return;
  trSideSid = sid;
  trSidePending = false;
  trSideTeamId = team ? String(team.team_id || "") : "";
  if (team && team.team_id) trSideNote(team.team_id, team.resident === true);
  trSyncSideBadge();
}

// 角标计数：常驻镜像 ∪ 当前会话进行中的团队（去重）
function trSideBadgeCount() {
  const ids = trSideLoad();
  let n = ids.size;
  if ((trSidePending || trSideTeamId) && !ids.has(trSideTeamId)) n += 1;
  return n;
}

// 角标上屏：展开态（#nav-team-badge）与折叠栏（#rail-team-badge）一起；
// 无团队时隐藏（常驻入口常在，角标只反映「有团队在场」）
function trSyncSideBadge() {
  const n = trSideBadgeCount();
  [["nav-team-badge", "nav-badge"], ["rail-team-badge", "rail-badge"]].forEach(([id, cls]) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = cls + (n ? "" : " hidden");
    el.textContent = n ? String(n) : "";
  });
}

// 五团队事件同源喂角标（trRoomFeed 入口转发，与团队卡/页签同一事件流）：
// 起队即时 +1（team_id 未校准先记 pending，起队校准回包对齐），终态清位；
// 别的会话的事件不进角标（角标的「进行中」只对当前会话口径）
function trSideFeed(name, data) {
  const sid = String((data && data.session_id) || "");
  if (sid && trSideSid && sid !== trSideSid) return;
  if (name === "trOnStarted") {
    trSidePending = true;
    trSyncSideBadge();
  } else if (name === "trOnFinish") {
    if (!trSidePending && !trSideTeamId) return; // 本就没有进行中的团队：不动
    trSidePending = false;
    trSideTeamId = "";
    trSideEpoch++;
    trSyncSideBadge();
  }
}

// 模块装载即画一次：镜像里已有的常驻登记让角标在重启后立刻在场（不等第一
// 次 team.get 对齐；登记集只在 UI 的 set/clear 上变化，镜像跨刷新仍准）
trSyncSideBadge();

// ---- 容器：复用 buildTeamCard 整卡，重排成三分区 ----

function buildTeamRoomCard(roster, directorMode, director) {
  const card = buildTeamCard(roster, directorMode, director);
  card.classList.add("teamroom-card", "view-channel");
  // 频道常显（三分区同屏不再切「工单/频道」页签）；_view 固定 channel 顺带
  // 关掉 tcMarkChannelNew 的未读圆点（频道就在眼前，圆点没有意义）
  card._view = "channel";
  // 头部：标题换叫法；折叠没有语义（tcFinish 会给终态卡加 .folded，
  // CSS 对 .teamroom-card.folded 放行正文，页签内容不被收起）
  const head = card.querySelector(".tc-head");
  const title = head.querySelector(".tc-title");
  if (title) title.textContent = "🧩 协作";
  head.onclick = null;
  if (card._ui.fold) card._ui.fold.hidden = true;
  // 收队/交付/接管/继续推进/另存为模板进头部（设计 §15.2）：按钮 DOM 从
  // foot 搬家，_ui 引用不变，团队卡同款 handler 原样生效
  const foot = card.querySelector(".tc-foot");
  if (foot) {
    Array.from(foot.children).forEach((b) => head.insertBefore(b, card._ui.fold));
    foot.remove();
  }
  // 「工单 / 频道」页签条不进团队室
  const views = card.querySelector(".tc-views");
  if (views) views.remove();
  // 三分区 grid：成员带（rosterEl）留在 body 顶部，频道主区 + 工单板右栏
  const body = card.querySelector(".tc-body");
  const grid = document.createElement("div");
  grid.className = "tr-grid";
  grid.append(card._ui.channelEl, card._ui.boardList);
  body.appendChild(grid);
  return card;
}

// ---- 装载：team.room 聚合探针（激活 / 切会话 / 重连都重拉） ----

async function loadTeamRoom(force) {
  const root = trRoot();
  if (!root) return;
  const t = typeof curTab === "function" ? curTab() : null;
  const sid = (t && t.sid) || "";
  await trSeenLoad(); // badge 基数（按 team_id 的 last_seen）先就位再谈差值
  let r = null;
  let err = null;
  try {
    r = await request("team.room", sid ? { session_id: sid } : {});
  } catch (e) {
    err = e;
  }
  const t2 = typeof curTab === "function" ? curTab() : null;
  if (((t2 && t2.sid) || "") !== sid) return; // 请求期间切了会话：丢弃，下次激活再拉
  if (err) {
    trRenderError(root, err);
    return;
  }
  if (r && r.active && r.team) {
    trTrackRoom(r.team.team_id, r.latest_seq, sid); // 水位校准在挂卡前（进室即清零）
    trSideAlign(r.team, sid); // 侧栏角标（§15.3.5）：当前会话进行中的团队对齐 + 常驻真值并入
    trMountSnapshot(root, r.team, sid);
    return;
  }
  // 无活动团队：badge 无从谈起（§15.4 的 badge 行只覆盖「页签在后台而团队在
  // 推进」），水位归零，等下一支队伍
  trTrackRoom("", 0, sid);
  trSideAlign(null, sid); // 侧栏角标（§15.3.5）：本会话没有进行中的团队，清位（中断记录不算在场）
  // 非活动：先看中断记录（重启/切项目前的团队），空态卡附只读回放入口
  if (r && r.interrupted && r.interrupted.team_id) {
    trRenderEmpty(root, { interrupted: r.interrupted });
    return;
  }
  // 再退一层：本会话最后一张团队卡的最近快照——终态团队（交付/收队/轮次
  // 耗尽/预算越线）渲染只读团队室（成员带照常展示，_finished 口径同回放卡）
  let lastTeam = null;
  if (t && typeof lastTeamCard === "function") {
    const last = (t.teamCard && t.teamCard.isConnected) ? t.teamCard : lastTeamCard(t);
    lastTeam = (last && last._lastTeam) || null;
  }
  if (lastTeam) {
    trMountSnapshot(root, lastTeam, sid);
    return;
  }
  trRenderEmpty(root, {});
}

// 一份团队快照上屏：同队复用现役容器做增量对齐，异队重建（团队室不做跨会话缓存）
function trMountSnapshot(root, team, sid) {
  root.classList.remove("tr-dash");
  const teamId = String(team.team_id || "");
  let card = (trCard && trCard.isConnected && trCard._teamId === teamId) ? trCard : null;
  const finished = (team.status || "active") !== "active";
  if (!card) {
    root.innerHTML = "";
    card = buildTeamRoomCard(
      team.roster || [], team.director_mode || "user", tcDirectorOfSnapshot(team));
    card._sid = sid;
    card._teamId = teamId;
    trCard = card;
    root.appendChild(card);
  }
  // 终态只读（设计 §15.4）：行内按钮在 buildTaskRow 重画时即不再挂
  if (finished && !card._finished) {
    card._finished = true;
    card.classList.add("ended");
  }
  updateTeamCard(card, team);
  // 快照里的频道是截断尾部：有 team_id 就拉 team.log 全量叠画（两级容错照旧，
  // tcFetchFullLog 按 team_id 缓存去重）
  if (teamId) tcFetchFullLog({ sid }, card, { team_id: teamId, session_id: sid || undefined });
  trSyncBadge();
}

// ---- 空态与降级（设计 §15.4） ----

function trRenderError(root, err) {
  root.classList.remove("tr-dash");
  root.innerHTML = "";
  const box = tcEl("div", "teamroom-error");
  box.appendChild(tcEl("span", "tr-error-text", "协作加载失败：" + ((err && err.message) || err)));
  const retry = tcEl("button", "rp-mini", "重试");
  retry.onclick = () => loadTeamRoom(true);
  box.appendChild(retry);
  root.appendChild(box);
}

let trTplLoading = false;

function trRenderEmpty(root, opts) {
  opts = opts || {};
  root.innerHTML = "";
  if (opts.interrupted) {
    root.classList.remove("tr-dash");
    // 中断记录（重启/切项目前留过团队）：附中断说明与只读回放入口。
    const box = tcEl("div", "teamroom-empty");
    box.appendChild(tcEl("div", "tr-empty-title", "上次团队协作已中断"));
    box.appendChild(tcEl("p", "dim small",
      "上次团队中断：" + (opts.interrupted.interrupt_reason || "应用重启，团队已中断")));
    const actions = tcEl("div", "tr-empty-actions");
    const replayBtn = tcEl("button", "rp-mini primary", "只读回放频道");
    replayBtn.onclick = () => trOpenInterruptedReplay(opts.interrupted);
    actions.appendChild(replayBtn);
    box.appendChild(actions);
    root.appendChild(box);
    return;
  }
  root.classList.add("tr-dash");
  // 空态不再是一大片居中空白：状态 + 快速开始 + 最近协作，组成协作仪表盘
  const status = tcEl("div", "tr-dash-status");
  status.appendChild(tcEl("div", "tr-empty-title", "本会话没有进行中的协作"));
  status.appendChild(tcEl("p", "dim small tr-empty-hint",
    "选一个模板开始；团队模板会在这里展开频道与工单板。"));
  root.appendChild(status);

  trRenderQuickStart(root);
  trRenderRecentRuns(root);

  const foot = tcEl("div", "tr-dash-foot");
  const manage = tcEl("button", "rp-mini", "管理模板");
  manage.onclick = () => { if (typeof openSettings === "function") openSettings("collab"); };
  const back = tcEl("button", "rp-mini", "回到对话");
  back.onclick = () => {
    const sw = document.getElementById("coop-switch");
    if (sw) sw.focus();
  };
  foot.append(manage, back);
  root.appendChild(foot);
}

// 「快速开始」：内置 + 我的模板直接摆出来，点一下即选到输入框（不必先开菜单）
function trRenderQuickStart(root) {
  const block = tcEl("div", "tr-dash-block");
  block.appendChild(tcEl("div", "tr-dash-title", "快速开始"));
  const list = (typeof collabTplAll !== "undefined" && Array.isArray(collabTplAll))
    ? collabTplAll : [];
  if (!list.length) {
    if (!trTplLoading && typeof collabRefreshTemplates === "function") {
      trTplLoading = true;
      collabRefreshTemplates().then(() => {
        trTplLoading = false;
        if (typeof loadTeamRoom === "function") loadTeamRoom(true);
      }).catch(() => { trTplLoading = false; });
    }
    block.appendChild(tcEl("div", "dim small", "正在加载模板…"));
    root.appendChild(block);
    return;
  }
  const selected = activeTab ? String(activeTab.template || "") : "";
  const grid = tcEl("div", "tr-tpl-grid");
  for (const t of list.slice(0, 6)) {
    const kind = (typeof COLLAB_KIND_LABELS === "object" && COLLAB_KIND_LABELS[t.kind]) || String(t.kind || "");
    const needs = (typeof collabTplNeeds === "function") ? collabTplNeeds(t) : "read";
    const needsLabel = (typeof COLLAB_NEEDS_LABELS === "object" && COLLAB_NEEDS_LABELS[needs]) || needs;
    const b = tcEl("button", "tr-tpl-card" + (selected === t.name ? " active" : ""));
    b.innerHTML = `<span class="tr-tpl-name">${escapeHtml(String(t.name || ""))}</span>` +
      `<span class="tr-tpl-meta">${escapeHtml(kind)} · ${escapeHtml(needsLabel)}</span>`;
    b.title = `使用模板「${t.name}」：已选到输入框，写下任务发送即可`;
    b.onclick = () => {
      if (typeof collabApplyTemplate === "function") collabApplyTemplate(t);
      const input = document.getElementById("input");
      if (input) input.focus();
    };
    grid.appendChild(b);
  }
  block.appendChild(grid);
  root.appendChild(block);
}

// 从已加载的会话历史里挑出协作轮（圆桌 / 团队 / 对抗）
function trCollabRuns() {
  const t = (typeof curTab === "function") ? curTab() : null;
  const all = (t && Array.isArray(t._historyAll)) ? t._historyAll : [];
  const runs = [];
  for (const m of all) {
    if (!m || m.role !== "assistant") continue;
    const adv = m.adversarial;
    const rt = m.roundtable;
    if (adv && typeof adv === "object") {
      const stats = adv.stats || {};
      runs.push({
        seq: m.seq, kind: "adversarial",
        name: adv.template || "对抗审查",
        detail: `${Number(stats.total || 0)} 条问题 · ${Number(stats.confirmed || 0)} 条成立`,
        status: String(adv.status || ""), text: String(m.text || ""),
      });
    } else if (rt && typeof rt === "object") {
      const isTeam = rt.mode === "team";
      const team = rt.team || {};
      const members = Array.isArray(rt.members) ? rt.members.length
        : (Array.isArray(team.roster) ? team.roster.length : 0);
      runs.push({
        seq: m.seq, kind: isTeam ? "team" : "roundtable",
        name: rt.template || (isTeam ? "团队协作"
          : (rt.mode === "compare" ? "圆桌对比" : "圆桌会诊")),
        detail: isTeam ? `${members} 名队员` : `${members} 个成员`,
        status: String(team.status || rt.status || ""), text: String(m.text || ""),
      });
    }
  }
  return runs.slice(-5).reverse();
}

function trRenderRecentRuns(root) {
  const runs = trCollabRuns();
  const block = tcEl("div", "tr-dash-block");
  block.appendChild(tcEl("div", "tr-dash-title",
    runs.length ? `最近协作（${runs.length}）` : "最近协作"));
  if (!runs.length) {
    block.appendChild(tcEl("div", "dim small",
      "还没有协作记录；用上面的模板发起一次，完成后会出现在这里。"));
    root.appendChild(block);
    return;
  }
  const list = tcEl("div", "tr-recent");
  for (const r of runs) {
    const kindLabel = r.kind === "team" ? "团队" : r.kind === "adversarial" ? "对抗" : "圆桌";
    const bad = (r.status === "error" || r.status === "cancelled")
      ? `<span class="tr-recent-bad">${r.status === "cancelled" ? "已停止" : "失败"}</span>` : "";
    const preview = String(r.text || "").replace(/\s+/g, " ").trim().slice(0, 46);
    const row = tcEl("button", "tr-recent-row");
    row.innerHTML = `<span class="tr-recent-kind k-${r.kind}">${kindLabel}</span>` +
      '<span class="tr-recent-main">' +
      `<span class="tr-recent-name">${escapeHtml(String(r.name || ""))}${bad}</span>` +
      `<span class="tr-recent-sub">${escapeHtml(r.detail)}` +
      (preview ? " · " + escapeHtml(preview) : "") + "</span></span>";
    row.title = "跳到这条协作消息";
    row.onclick = () => {
      if (typeof jumpToMessageSeq === "function") jumpToMessageSeq(curTab(), r.seq);
    };
    list.appendChild(row);
  }
  block.appendChild(list);
  root.appendChild(block);
}

// 「只读回放频道」：凭中断记录里的 team_id 拉全量频道，渲染只读时间线
function trOpenInterruptedReplay(rec) {
  const root = trRoot();
  const t = typeof curTab === "function" ? curTab() : null;
  if (!root || !t) return;
  const roster = (rec.roster || []).map((m) =>
    (m && typeof m === "object") ? m : { name: String(m || ""), provider: "", model: "" });
  const teamId = String(rec.team_id || "");
  root.innerHTML = "";
  const card = buildTeamRoomCard(roster, rec.director_mode || "user",
    rec.director_mode === "ai" ? { provider: "", model: "" } : null);
  card._sid = t.sid || trSid || "";
  card._teamId = teamId;
  card._ui.sub.textContent = "应用重启前的团队记录  只读回放";
  trCard = card;
  root.appendChild(card);
  tcFinish(card, "interrupted", ""); // 终态口径：操作按钮全收，纯读
  if (teamId) tcFetchFullLog(t, card, { team_id: teamId, session_id: t.sid });
}

// ---- 事件转发：五团队事件同源喂团队室（app.js 的 trRoomFeed 入口） ----

function trRoomFeed(name, data) {
  trSideFeed(name, data); // 侧栏「团队」角标同一事件源（§15.3.5，见下方角标节）
  const fn = { trOnStarted, trOnDelta, trOnMsg, trOnTask, trOnFinish }[name];
  if (fn) fn(data);
}

// 容器在眼前且事件属于它对准的会话（渲染路径：要动手画卡，必须先有卡）
function trRoomMatches(data) {
  if (!trCard || !trCard.isConnected) return false;
  return trBgRoomMatches(data);
}

// 后台记账的会话匹配（§15.4 回归①）：不要求挂卡——空态/错误态/后台起队
// 都没有现役容器，卡前提会让 trOnMsg 的后台分支永不抬水位、进页签即
// trMarkSeen(latest) 整表清零，页签开着停在空态时后台推进的团队消息未读
// 整体丢失（早期内存计数制对后台定稿一律计数，此处是等价记账的回归点）。
// 按 team_id+seq 直接对水位记账：只按会话过滤（别会话的 seq 是另一条数轴，
// 照旧不混计）
function trBgRoomMatches(data) {
  const sid = data && data.session_id;
  if (sid && trSid && sid !== trSid) return false;
  return true;
}

function trOnStarted(data) {
  const t = typeof curTab === "function" ? curTab() : null;
  const sid = (data && data.session_id) || (t && t.sid) || "";
  if (trSid && sid && trSid !== sid) return;
  // 新队 seq 从 1 重数、队 id 未知（事件不带 team_id）：无论页签在不在
  // 眼前，上一支队伍的已见水位都不跨队沿用
  trTeamId = "";
  trLatestSeq = 0;
  trSyncBadge(); // 水位重置后页签角标同步（眼前恒 0、后台按 seen=0 全计）
  // 起动校准（回归②）：把 team_id/latest_seq 补齐，眼前的起队才能即时推
  // 已见水位、常驻复用才不把上轮已见的重计成未读。后台路径页签一开
  // loadTeamRoom 就权威校准，这里先探一次只省一段 badge 失真的窗口
  trCalibrateStarted(sid);
  if (!trTabVisible()) return; // 后台不渲染：激活时 loadTeamRoom 重拉
  trCard = null; // 新团队另起一张
  const isAi = (data.director_mode || "user") === "ai";
  const card = trEnsureCard(data.roster || [], data.director_mode || "user",
    isAi ? { provider: data.director_provider || "", model: data.director_model || "" } : null);
  card._sid = sid;
  trSid = sid;
  tcSetStatus(card, "active");
}

function trEnsureCard(roster, directorMode, director) {
  const root = trRoot();
  if (!root) return null;
  if (trCard && trCard.isConnected) return trCard;
  root.innerHTML = "";
  trCard = buildTeamRoomCard(roster, directorMode, director);
  root.appendChild(trCard);
  return trCard;
}

function trOnDelta(data) {
  if (!trTabVisible() || !trRoomMatches(data)) return;
  tcOpenStreamRow(trCard, data);
}

function trOnMsg(data) {
  if (!trTabVisible()) {
    // 页签在后台：同会话的定稿按 team_id+seq 直接对水位记账，badge =
    // latest − last_seen（§15.4）。不依赖挂卡——空态/错误态/后台起队没有
    // 现役容器，卡前提会让后台水位永不抬、进页签即整表清零（回归①）
    if (trBgRoomMatches(data)) trTrackEventSeq(data && data.seq);
    return;
  }
  if (!trRoomMatches(data)) return;
  tcUpsertMsg(trCard, data);
  // 眼看过的消息即时推已见水位：切去别的页签后 badge 不把刚看过的算未读
  //（teamId 未知时这里是空操作，起动校准/进页签的 team.room 校准兜底）
  trMarkSeen(trTeamId, data && data.seq);
}

function trOnTask(data) {
  if (!trTabVisible() || !trRoomMatches(data)) return;
  // 事件里没有 deps/accept：与行上记住的旧数据合并后再重画（同团队卡口径）
  const row = trCard._ui.boardList.querySelector(`[data-task="${data.task_id}"]`);
  const prev = (row && row._task) || {};
  tcUpsertTask(trCard, Object.assign({}, prev, {
    id: data.task_id, title: data.title, assignee: data.assignee,
    type: data.type, status: data.status, redo: data.redo,
    detail: data.detail !== undefined ? data.detail : prev.detail,
    source: data.source !== undefined ? data.source : prev.source,
    auto_dispatch: data.auto_dispatch !== undefined ? data.auto_dispatch : prev.auto_dispatch,
  }));
}

function trOnFinish(data) {
  if (!trRoomMatches(data)) return;
  if (trCard) tcFinish(trCard, data.status, data.summary || "");
}
