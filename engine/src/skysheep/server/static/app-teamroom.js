// ---------- 团队室（独立页签）：成员带 · 频道消息流 · 工单板 三分区同屏 ----------
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
  request("team.room", sid ? { session_id: sid } : {})
    .then((r) => {
      // 迟到的回包不作数：期间又起队或权威校准（trTrackRoom）已接手
      if (gen !== trCalGen || trTeamId) return;
      if (!r || !r.active || !r.team) return;
      trSid = sid || "";
      trTeamId = String(r.team.team_id || "");
      const latest = Number(r.latest_seq) || 0;
      if (latest > trLatestSeq) trLatestSeq = latest;
      if (trTabVisible()) trMarkSeen(trTeamId, trLatestSeq);
      trSyncBadge();
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
  if (title) title.textContent = "🧩 团队室";
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
    trMountSnapshot(root, r.team, sid);
    return;
  }
  // 无活动团队：badge 无从谈起（§15.4 的 badge 行只覆盖「页签在后台而团队在
  // 推进」），水位归零，等下一支队伍
  trTrackRoom("", 0, sid);
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
  root.innerHTML = "";
  const box = tcEl("div", "teamroom-error");
  box.appendChild(tcEl("span", "tr-error-text", "团队室加载失败：" + ((err && err.message) || err)));
  const retry = tcEl("button", "rp-mini", "重试");
  retry.onclick = () => loadTeamRoom(true);
  box.appendChild(retry);
  root.appendChild(box);
}

function trRenderEmpty(root, opts) {
  opts = opts || {};
  root.innerHTML = "";
  const box = tcEl("div", "teamroom-empty");
  box.appendChild(tcEl("div", "tr-empty-title",
    opts.interrupted ? "本会话的团队已中断" : "本会话没有进行中的团队"));
  const actions = tcEl("div", "tr-empty-actions");
  if (opts.interrupted) {
    // 中断记录（重启/切项目前留过团队）：附中断说明与只读回放入口。
    // 注：中断记录不带时间戳，这里只给原因（设计 §15.4 的「中断于 <时间>」
    // 无处可取，按代码现状降级为显示 interrupt_reason）
    box.appendChild(tcEl("p", "dim small",
      "上次团队中断：" + (opts.interrupted.interrupt_reason || "应用重启，团队已中断")));
    const replayBtn = tcEl("button", "rp-mini primary", "只读回放频道");
    replayBtn.onclick = () => trOpenInterruptedReplay(opts.interrupted);
    actions.appendChild(replayBtn);
  } else {
    // 两个引导动作（设计 §15.4）：从模板建队走既有建队浮层，不重复造建队 UI；
    // 模板册拉不到/为空时按设计只留文字引导（按钮移除）
    const tplBtn = tcEl("button", "rp-mini primary", "从模板建队");
    tplBtn.onclick = () => trStartFromTemplate();
    const backBtn = tcEl("button", "rp-mini", "回到对话发起");
    backBtn.title = "回到对话，用输入区的「协作」开关发起团队";
    backBtn.onclick = () => {
      const sw = document.getElementById("coop-switch");
      if (sw) sw.focus();
      else addNotice("请切回对话页，用输入区的「协作」开关发起团队");
    };
    actions.append(tplBtn, backBtn);
    box.appendChild(tcEl("p", "dim small tr-empty-hint",
      "没有合适的模板？回到对话，点输入区的「协作」选「团队」，当场点名组队。"));
    request("team.template_list")
      .then((r) => {
        if (!((r && r.templates) || []).length) tplBtn.remove();
      })
      .catch(() => tplBtn.remove());
  }
  root.appendChild(box);
}

// 「从模板建队」：开启输入区团队开关并放出既有建队浮层（模板下拉在内）
async function trStartFromTemplate() {
  let templates = [];
  try {
    templates = ((await request("team.template_list")).templates) || [];
  } catch (e) { /* 模板册拉不到：浮层照常打开，模板区自然不出现 */ }
  if (!templates.length) {
    addNotice("还没有团队模板：回到对话，点输入区的「协作」选「团队」当场组队。");
    return;
  }
  // 建队浮层长在输入区旁：全页视图（设置/工厂）开着先回对话——与命令面板协作
  // 命令的「先回对话再开」同款语义（那边是 paletteBaseItems 的局部函数，全局
  // 查无此名；旧写法调它靠 typeof 守卫静默跳过，死引用已修，勿再引回）
  if (typeof pageViewOpen === "function" && pageViewOpen() && typeof backToChat === "function") backToChat();
  if (typeof setTeamOn === "function") setTeamOn(true);
  const menu = document.getElementById("coop-menu");
  if (typeof showCoopMenu === "function"
    && !(menu && !menu.classList.contains("hidden"))) {
    showCoopMenu({ stopPropagation() {} }); // 协作菜单先摆出来，弹层才有锚
  }
  if (typeof openSettingsBeside === "function") openSettingsBeside("team");
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
  card._ui.sub.textContent = "应用重启前的团队记录 · 只读回放";
  trCard = card;
  root.appendChild(card);
  tcFinish(card, "interrupted", ""); // 终态口径：操作按钮全收，纯读
  if (teamId) tcFetchFullLog(t, card, { team_id: teamId, session_id: t.sid });
}

// ---- 事件转发：五团队事件同源喂团队室（app.js 的 trRoomFeed 入口） ----

function trRoomFeed(name, data) {
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
