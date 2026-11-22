// app-sidebar.js —— 侧栏（经典视图渲染 / 折叠窄栏 rail / 无障碍键盘 / 分组视图 /
//              拖动排序状态机 / 会话搜索与消息跳转 / 会话操作菜单）。
// 从 app.js 拆出（路线图第 3b 批）；普通 script、零构建，加载顺序见 index.html：
// 后于 app.js（本文件的函数由 app.js 与各分区模块在事件回调里调用，运行时解析；
// app.js 对这些符号零加载期引用），先于 app-cards.js。
// 本文件顶层接线只依赖 DOM 与自身符号；弹层四件套（showModal 等）、标签命名、
// 下载助手与 setModelChip 留在 app.js，事件期互调。

// ---------- 侧栏 ----------
let sessionSearchActive = false; // 搜索结果展示期间，禁止列表刷新覆盖
let activeSessionSid = null; // 侧栏里被点开的会话 id（两种视图共用）：列表因折叠/快照等重画后，选中高亮照它恢复
// 经典视图选中的项目列表：null=当前项目；"quick"=快聊；"remote:<id>"=远程连接。
// 点哪行亮哪行（项目区同时最多一行高亮）；会话区高亮独立、最多一条（sess-active）
let classicViewGk = null;
// 本机桌面端才拿得到跨项目会话列表（session.list all_projects）：首次探测后
// 记住 true/false，远程端不再反复请求（false=项目区不放快聊/远程行）
let classicQuickAvailable = null;

async function refreshSessions(prefetched) {
  if (sessionSearchActive) return;
  // 分组视图：项目与合到一起，渲染走另一条路（见 refreshSessionsGrouped）
  if (sidebarView === "grouped") return refreshSessionsGrouped();
  // prefetched：站内切换项目时已先取回，直接渲染（不经过网络等待，避免列表先清空）
  const { sessions, empty_count, archived_count, quick_sessions } =
    prefetched || await request("session.list");
  const ul = document.getElementById("session-list");
  ul.innerHTML = "";
  // 经典视图同样按家族块（└/⑂ 子跟随父）+ 保存序排（键 = 当前项目 id，
  // 与分组视图共用 session_order）；classic 列表即当前项目，直接取
  const classicGk = bootSnap && bootSnap.project_id != null ? String(bootSnap.project_id) : null;
  // 无项目态默认陈列快聊（会话区只可能是快聊）；已选远程连接时不覆盖。
  // 远程客户端拿不到 quick_sessions（字段不下发），不强推快聊视图
  if (classicGk == null && classicViewGk == null && Array.isArray(quick_sessions)) {
    classicViewGk = "quick";
  }
  // 特殊列表（快聊 / 远程连接）：由项目行点击切换（见 refreshProjects）。
  // 不随标签切换自动进退——高亮与陈列都只由「点哪行」决定
  if (classicViewGk === "quick") {
    // 无快聊对话时不摆空态提示（入口就在项目区的「快聊」行上，不必再教一遍）
    const qlist = Array.isArray(quick_sessions) ? quick_sessions : [];
    const qOrdered = orderedSessionList(qlist, classicViewGk);
    lastGroupedListByGroup.set(classicViewGk, qOrdered); // 拖拽排序的家族归并数据源（commitSessionOrder 读）
    renderPlainList(ul, qOrdered, "quick", "");
    // 与 proj 视图同一套拖拽：列表内重排（序存 quick 键）、拖出进对话区=分屏
    // （快聊属固定项目、无 root_path，drop 侧不会触发切换，本机分屏列全可交互）
    wirePlainListDrag(ul, qOrdered, classicViewGk);
    const footer = document.getElementById("session-footer");
    if (footer) { // 页脚统计是当前项目的，特殊列表下不展示也不残留
      footer.classList.add("hidden");
      footer.innerHTML = "";
    }
    renderRailSessions(sessions);
    return;
  }
  if (classicViewGk && classicViewGk.startsWith("remote:")) {
    // 「远程连接」列表：跨项目拉取后按项目过滤（本机桌面端才有 all_projects）
    const rid = classicViewGk.slice("remote:".length);
    const all = await request("session.list", { all_projects: 1 }).catch(() => null);
    const rlist = ((all && all.sessions) || []).filter((s) => String(s.project_id) === rid);
    const rOrdered = orderedSessionList(rlist, classicViewGk);
    lastGroupedListByGroup.set(classicViewGk, rOrdered); // 拖拽排序的家族归并数据源（commitSessionOrder 读）
    renderPlainList(ul, rOrdered, classicViewGk,
      "还没有渠道对话——在飞书/微信里给机器人发条消息就会出现在这里");
    // 与 proj 视图同一套拖拽：列表内重排（序存 remote:<id> 键）、拖出进对话区
    // =分屏（渠道会话属固定项目、无 root_path，drop 侧不触发切换，本机列全可交互）
    wirePlainListDrag(ul, rOrdered, classicViewGk);
    const footer = document.getElementById("session-footer");
    if (footer) {
      footer.classList.add("hidden");
      footer.innerHTML = "";
    }
    renderRailSessions(sessions);
    return;
  }
  if (classicViewGk && classicViewGk.startsWith("proj:")) {
    // 「看其他项目」列表（点项目区其他项目行进入）：跨项目拉取后按项目过滤；
    // 行点击＝先切工作项目再打开（后端要求会话在其项目内激活，同分组视图
    // 跨项目行点击的流程）
    const pid = classicViewGk.slice("proj:".length);
    const all = await request("session.list", { all_projects: 1 }).catch(() => null);
    const plist = ((all && all.sessions) || []).filter((s) => String(s.project_id) === pid);
    const projs = ((await request("project.list").catch(() => null)) || {}).projects || [];
    const target = projs.find((p) => String(p.id) === pid);
    ul.innerHTML = "";
    if (!plist.length) {
      ul.innerHTML = '<li class="empty-hint">这个项目还没有会话</li>';
    } else {
      const ordered = orderedSessionList(plist, classicViewGk);
      lastGroupedListByGroup.set(classicViewGk, ordered); // 拖拽排序的家族归并数据源（commitSessionOrder 读）
      ordered.forEach((s) => {
        const li = renderSessionItem(s, ul);
        // 与经典/分组视图同一套拖拽：列表内重排（序存 proj:<id> 键）、
        // 拖出列表进对话区 = 转分屏打开（跨项目由 drop 侧先切项目再开列）
        wireSessionDrag(li, s, ordered, classicViewGk, () => refreshSessions(), "classic");
        if (target && target.root_path && !target.is_current) {
          li.title = "属于项目「" + (target.name || "") + "」—— 点击切换过去并打开";
          li.onclick = async () => {
            // 先记住目标会话再切项目：切完的列表重画会照它恢复选中高亮
            activeSessionSid = s.id;
            try {
              await request("project.switch", { path: target.root_path });
              await applyWorkspaceData(await fetchWorkspaceData());
            } catch (e) {
              addNotice("切换到该项目失败：" + e.message);
              return;
            }
            openTabForSession(s.id, s.title);
            clearTodoPanel();
            addNotice("已打开会话 " + (s.title || s.id));
          };
        }
        ul.appendChild(li);
      });
    }
    const footer = document.getElementById("session-footer");
    if (footer) {
      footer.classList.add("hidden");
      footer.innerHTML = "";
    }
    renderRailSessions(sessions);
    return;
  }
  const classicList = classicGk != null ? orderedSessionList(sessions, classicGk) : sessions;
  if (classicGk != null) lastGroupedListByGroup.set(classicGk, classicList);
  // 按标签分组：有标签的会话归入对应组（可属多组），无标签的在「未分组」；
  // 全部会话都没标签时不分组，列表与从前完全一致（空分组头只是噪声）
  const tagMap = new Map();
  sessions.forEach((s) => {
    (s.tags || []).forEach((t) => {
      if (!tagMap.has(t)) tagMap.set(t, []);
      tagMap.get(t).push(s);
    });
  });
  const grouped = tagMap.size > 0;
  const untagged = classicList.filter((s) => !(s.tags || []).length);
  const groups = [];
  if (grouped) {
    for (const [tag, list] of tagMap) groups.push({ tag, list });
    if (untagged.length) groups.push({ tag: "未分组", list: untagged });
  } else {
    groups.push({ tag: null, list: classicList });
  }
  groups.forEach(({ tag, list }) => {
    if (tag) {
      const head = document.createElement("li");
      head.className = "s-group";
      head.innerHTML = `<span class="s-group-name">${escapeHtml(tag)}</span>`;
      head.onclick = () => {
        // 点分组头 = 只看这一组（再点一次取消过滤）
        activeTagFilter = activeTagFilter === tag ? null : tag;
        refreshSessions(prefetched);
      };
      if (activeTagFilter === tag) head.classList.add("active");
      ul.appendChild(head);
    }
    if (activeTagFilter && tag !== activeTagFilter) return;
    list.forEach((s) => {
      const li = renderSessionItem(s, ul);
      if (classicGk != null) {
        wireSessionDrag(li, s, classicList, classicGk, () => refreshSessions(), "classic");
      }
      ul.appendChild(li);
    });
  });
  renderSessionExtras({ empty_count, archived_count });
  renderRailSessions(sessions);
}

/** 经典视图特殊列表（快聊 / 远程连接）的平铺陈列：无组头——项目区对应行
    就是入口。展开/「显示更多」状态按 gk 独立记忆（键 "quick" / "remote:<id>"）。
    不接拖动排序：经典视图的 commitSessionOrder 按整张列表收集会话 id，混入
    特殊列表会把当前项目的拖动序污染（分组视图里的对应组仍可拖，那份顺序在
    这里照常生效——orderedSessionList 会读偏好）。 */
function renderPlainList(ul, list, gk, emptyHint) {
  const st = groupState(gk);
  const ordered = orderedSessionList(list, gk);
  lastGroupedListByGroup.set(gk, ordered);
  if (!ordered.length) {
    // emptyHint 为空＝这个列表不摆空态提示（如快聊：入口就在项目区行上）
    ul.innerHTML = emptyHint ? `<li class="empty-hint">${escapeHtml(emptyHint)}</li>` : "";
    return;
  }
  const visible = st.all ? ordered : ordered.slice(0, GROUP_PREVIEW);
  visible.forEach((s) => ul.appendChild(renderSessionItem(s, ul)));
  if (ordered.length > visible.length) {
    const more = document.createElement("li");
    more.className = "pgroup-more";
    more.textContent = st.all ? "收起" : `显示更多 ${ordered.length - visible.length} 个`;
    more.onclick = () => { st.all = !st.all; refreshSessions(); };
    ul.appendChild(more);
  }
}

// 空会话清理入口与归档入口：经典/分组两种视图共用（都只看当前项目）
function renderSessionExtras({ empty_count = 0, archived_count = 0 } = {}) {
  // 空会话清理入口（仅当确实存在空会话时出现）
  const footer = document.getElementById("session-footer");
  if (footer) {
    if (empty_count > 0) {
      footer.classList.remove("hidden");
      footer.innerHTML = `<button class="link-btn">🧹 清理 ${empty_count} 个空会话</button>`;
      footer.querySelector(".link-btn").onclick = async () => {
        if (!(await confirmModal(`将删除 ${empty_count} 个没有任何消息的空会话，删除后无法恢复。`,
          { title: "清理空会话", okLabel: "清理", danger: true }))) return;
        const r = await request("session.cleanup_empty");
        closeTabsForSids(r.removed_ids); // 被清掉的会话若开着标签，一并收掉
        refreshSessions();
      };
    } else {
      footer.classList.add("hidden");
    }
  }

  // 归档入口（有归档会话时出现；点开弹窗查看/恢复/删除）
  const arc = document.getElementById("session-archive");
  if (arc) {
    if ((archived_count || 0) > 0) {
      arc.classList.remove("hidden");
      arc.innerHTML = `<button class="link-btn">${svgIcon("archive")}<span>归档会话（${archived_count}）</span></button>`;
      arc.querySelector(".link-btn").onclick = openArchiveModal;
    } else {
      arc.classList.add("hidden");
      arc.innerHTML = "";
    }
  }
}

// 当前生效的标签过滤（点分组头切换）；只在内存里，不写偏好
let activeTagFilter = null;

// ---------- 折叠态窄栏（mini rail）：最近会话的首字快捷列 ----------
// 折叠后侧栏整条退场，窄栏承接「扫一眼、快速切」：列最近的会话，每项是标题
// 首字（中文单字 / 英文首字母），当前会话高亮。数据复用侧栏那次请求，不额外发 WS。
const RAIL_SESSION_MAX = 12; // 上限：再多也没人挨个认

function railInitial(title) {
  const t = String(title || "").trim();
  if (!t) return "·";
  return [...t][0].toUpperCase(); // 迭代器取字符：emoji / 代理对不会被截半
}

function renderRailSessions(sessions) {
  const box = document.getElementById("rail-sessions");
  if (!box) return;
  const list = (sessions || []).slice(0, RAIL_SESSION_MAX);
  box.innerHTML = "";
  if (!list.length) {
    const empty = document.createElement("span");
    empty.className = "rail-sess-empty";
    empty.textContent = "空";
    box.appendChild(empty);
    return;
  }
  list.forEach((s) => {
    const b = document.createElement("button");
    const active = s.id === currentSessionId;
    b.className = "rail-sess" + (active ? " active" : "");
    b.textContent = railInitial(s.title);
    b.title = (s.title || "(未命名)") + (active ? "（当前会话）" : "") + "  点击切换";
    b.onclick = () => {
      if (pageViewOpen()) backToChat();
      openTabForSession(s.id, s.title);
    };
    box.appendChild(b);
  });
}

// ---------- 无障碍：侧栏列表行补键盘激活 ----------
// 会话/项目列表行是 li（非 button，历史结构），键盘用户原本 Tab 不进去。
// 补 tabindex=0 让 Tab 能走到；Enter/空格触发行上既有的 onclick
// （el.click() 派发到行本身，激活逻辑只有一份，不复制不改写）。
// 焦点在行内真正的控件上时不劫持：⋯/✕/＋ 等按钮有原生键盘激活，
// 内联重命名输入框有自己的按键语义——事件 target 不是行本身就直接放过。
function wireRowKeyboard(el) {
  if (!el) return;
  el.tabIndex = 0;
  el.addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    if (e.target !== el) return;
    e.preventDefault(); // 空格防页面滚动，Enter 防再触发外层默认行为
    el.click();
  });
}

function renderSessionItem(s, ul) {
  const li = document.createElement("li");
  const tagChips = (s.tags || [])
    .map((t) => `<span class="s-tag">${escapeHtml(t)}</span>`).join("");
  const stamp = s.updated_at ? new Date(s.updated_at * 1000).toLocaleString() : "";
  li.innerHTML = `
      ${s.pinned ? '<span class="pin" title="已置顶">📌</span>' : ""}
      <span class="s-title">${escapeHtml(s.title || "(未命名)")}</span>
      <span class="s-time" title="最近活跃：${escapeHtml(stamp)}">${fmtRuleAge(s.updated_at)}</span>
      <button class="s-archive" title="归档">${svgIcon("archive")}</button>
      <button class="s-more" title="更多操作">⋯</button>` +
    (tagChips ? `<span class="s-tags">${tagChips}</span>` : "");
  li.dataset.sid = s.id; // 组内拖动排序：commitSessionOrder 沿 DOM 收集会话 id
  // 恢复「当前会话」高亮：列表会因折叠/快照/切项目等反复重画，选中态不能丢
  if (activeSessionSid != null && String(s.id) === String(activeSessionSid)) {
    li.classList.add("sess-active");
  }
  // 悬浮时时间让位：右侧出现归档与 ⋯
  li.querySelector(".s-archive").onclick = (e) => {
    e.stopPropagation();
    archiveSession(s);
  };
  li.querySelector(".s-more").onclick = (e) => {
    e.stopPropagation();
    const r = e.currentTarget.getBoundingClientRect();
    showSessionMenu(s, li, { x: r.right - 175, y: r.bottom + 4 });
  };
  // 右键同样唤出会话管理菜单
  li.oncontextmenu = (e) => {
    e.preventDefault();
    e.stopPropagation();
    showSessionMenu(s, li, { x: e.clientX, y: e.clientY });
  };
  li.onclick = () => {
    // 只清会话行（带 data-sid 的行）的选中态：组头（项目组/标签组）的 active 是
    // 「当前项目/标签过滤」的蓝色高亮，点会话不能把它抹掉——项目高亮保留，
    // 会话行自己换用 .sess-active 的另一种高亮（见 app.css）
    ul.querySelectorAll("li[data-sid]").forEach((x) => x.classList.remove("sess-active"));
    activeSessionSid = s.id;
    li.classList.add("sess-active");
    const t = openTabForSession(s.id, s.title);
    clearTodoPanel();
    if (t && !t.running) t.needHistory = false;
  };
  wireRowKeyboard(li); // 键盘可达：Enter/空格触发上面的行 onclick（经典与分组视图共用本渲染）
  return li;
}

// ---------- 分组视图：会话按项目折叠收拢（项目一多，经典两区挤不下会话） ----------
// 与经典视图并存，sidebar_view 偏好记住选择（classic=原两区样式，保留不删）
let sidebarView = "classic";
let groupSeq = 0; // 渲染序号：两次并发的分组渲染，慢的那个回来后直接丢弃
const projGroupState = new Map(); // 分组 key -> { open, all }：折叠与「显示更多」记忆，重渲染不丢
let lastGroupedCurrentKey = null; // 上一次分组渲染时的当前项目 key：项目切换时自动展开新组（取代旧「当前置顶」）
// 「点组头亮它」：组头高亮平时跟活动会话所在组（快聊会话亮快聊组）；点了
// 某个组头后高亮先借给那个组（折叠/展开都算一次点击），切标签/点会话时交回。
// 单值状态，同一时刻最多一个组头亮
let groupedClickGk = null;
// 上次分组渲染实际点亮的高亮 key（groupedClickGk || 活动会话所在组 || 当前
// 项目）：与组头高亮同一来源——「新建会话」的落点也读它（newSessionFromHighlight）
let lastGroupedHighlightKey = null;
// 组内会话的自定义顺序（ui.json 的 session_order，偏好回包后由 initUiPrefs 填充）。
// 键 = 组 key（字符串化项目 id），值 = 会话 id 数组（只记手动拖过的，新的照时间追加）
let sessionOrderPrefs = {};
// 标签栏页签的拖动序（ui.json 的 tab_order，sid 数组，只记有 sid 的会话标签；
// 偏好回包后由 initUiPrefs 填充，renderTabs 按它排，空标签始终垫底）
let tabOrderPrefs = [];
// 快聊分组的锚点位置（ui.json 的 quick_pos，经典/分组两视图共用）：
// null=垫底（默认），"top"=最前，{before: 项目id}=插在该项目前。
// 快聊是伪组（没有项目 id）进不了 project_order，位置单独记；偏好回包后由
// initUiPrefs 填充，拖动提交时从最终序反推（见 commitProjectOrder）
let quickPosPref = null;

/** 快聊组/行的插入时机（渲染前算好）：锚点项目不在本次列表（已删除）时回落垫底 */
function quickAnchor(projects) {
  if (quickPosPref === "top") return { mode: "top" };
  if (quickPosPref && typeof quickPosPref === "object" && quickPosPref.before != null
      && projects.some((p) => String(p.id) === String(quickPosPref.before))) {
    return { mode: "before", id: quickPosPref.before };
  }
  return { mode: "bottom" };
}
const lastGroupedListByGroup = new Map(); // 组 key -> 该组本次渲染的会话列表（家族归并要用，渲染前写入）
const GROUP_PREVIEW = 5; // 每个项目默认露出的会话条数，其余收进「显示更多」
const SUB_MARK = "└"; // session.fork 生成的分支会话标题前缀（「└ 父标题」），即子会话标记。
// 旧标题用 ⑂（OCR fork 字符，中文字体下笔画过细看不清），2.0 后新分叉统一用 └；
// 兼容期两个都认，见 SUB_MARKS

const SUB_MARKS = [SUB_MARK, "⑂"]; // 兼容：旧版本分叉的会话标题仍是 ⑂ 前缀

function subParentTitleOf(title) {
  const t = title || "";
  for (const mark of SUB_MARKS) {
    if (t.startsWith(mark)) return t.slice(mark.length).trimStart();
  }
  return null;
}

/** 在组列表里找子会话的父会话（fork 标题截断到 40 字符，子标题去前缀后可能只
    是父标题的前缀，所以双向 startsWith 兜底）；找不到父时子会话按顶层对待 */
function findParentInGroup(list, s) {
  const pt = subParentTitleOf(s.title);
  if (!pt) return null;
  return list.find((p) => p.id !== s.id && !subParentTitleOf(p.title) &&
    (p.title === pt || p.title.startsWith(pt) || pt.startsWith(p.title))) || null;
}

/** 组内会话的展示序：保存过的 id 按保存序排前，其余（没拖过的/新会话）按
    后端默认序（置顶在前、新在前）接着排——旧偏好不藏新会话，同 project_order。
    之后做家族聚拢：⑂ 子会话强制紧跟其父之后——它们是分支关系不是平级，
    不能因 updated_at 更新就跑到父上面，也不能被手动排序漂进别的会话之间 */
function orderedSessionList(list, gkey) {
  const saved = sessionOrderPrefs[String(gkey)];
  let base = list;
  if (saved && saved.length) {
    const rank = new Map(saved.map((id, i) => [id, i]));
    base = [...list.filter((s) => rank.has(s.id)).sort((a, b) => rank.get(a.id) - rank.get(b.id)),
            ...list.filter((s) => !rank.has(s.id))];
  }
  const subsByParent = new Map(); // 父 id -> 子数组（保持 base 相对序）
  const isSub = new Set();
  for (const s of base) {
    if (!subParentTitleOf(s.title)) continue;
    const parent = findParentInGroup(base, s);
    if (parent) {
      if (!subsByParent.has(parent.id)) subsByParent.set(parent.id, []);
      subsByParent.get(parent.id).push(s);
      isSub.add(s.id);
    }
  }
  const out = [];
  for (const s of base) {
    if (isSub.has(s.id)) continue; // 子已跟随父输出
    out.push(s);
    const subs = subsByParent.get(s.id);
    if (subs) out.push(...subs);
  }
  return out;
}

function groupState(key) {
  // 项目 id 从 JSON 来是数字、dataset.gkey 读回是字符串，同一组会存成两个键——
  // 折叠全部（走 dataset）就会与渲染（走原始 id）各写各的。入口统一成字符串
  key = String(key);
  if (!projGroupState.has(key)) projGroupState.set(key, { open: true, all: false });
  return projGroupState.get(key);
}

function applySidebarView() {
  // 单按钮切换：图标显示当前所在视图（同原先分段控件高亮图标的含义），
  // 点击切换视图、图标随之翻到另一个
  const vt = document.getElementById("view-toggle");
  if (vt) {
    vt.dataset.current = sidebarView;
    vt.title = sidebarView === "classic"
      ? "经典视图（点击切换到分组视图：会话收进各自项目下，项目多时省空间）"
      : "分组视图（点击切换到经典视图：项目、会话分成两区）";
  }
  // 按钮组随视图搬移（单实例 DOM 移动，状态/监听天然保留）：
  //   经典视图：[☰][＋] 在「项目」标题行右端（第一条横线右方，用户指定）；
  //   分组视图：项目区隐藏，[⌄][📁][＋] 回「会话」标题行，⌄ 在 📁 左侧。
  const addBtn = document.getElementById("session-add-project");
  const projOps = document.getElementById("project-head-ops");
  const sessOps = document.getElementById("session-head-ops");
  if (vt && addBtn && projOps && sessOps) {
    const dst = sidebarView === "classic" ? projOps : sessOps;
    dst.appendChild(vt);
    dst.appendChild(addBtn);
  }
  const sec = document.getElementById("project-section");
  if (sec) sec.classList.toggle("hidden", sidebarView === "grouped");
  const sec2 = document.getElementById("session-section");
  if (sec2) sec2.classList.toggle("grouped", sidebarView === "grouped");
  const ul = document.getElementById("session-list");
  if (ul) ul.classList.toggle("by-project", sidebarView === "grouped");
  syncFoldAllBtn();
}

document.getElementById("view-toggle").onclick = () => {
  sidebarView = sidebarView === "grouped" ? "classic" : "grouped";
  classicViewGk = null; // 经典视图快聊态是视图内状态，切视图后随当前项目重置
  groupedClickGk = null; // 分组视图「点组头」的高亮同样是视图内状态
  applySidebarView();
  saveUiPrefs({ sidebar_view: sidebarView });
  // 两个视图的列表渲染不同：切过去就重画；搜索词还在就按新视图重搜
  const q = searchEl.value.trim();
  if (q) renderSessionList(q);
  else refreshSessions();
};

// 「折叠全部项目」（分组视图专属）：项目一多时一键全收，全收后变成「展开全部项目」。
// 开合记忆与单组折叠同源（projGroupState）；经典视图与搜索结果没有可折叠组，按钮隐藏
function groupHeads() {
  return [...document.querySelectorAll("#session-list .pgroup-head")];
}

function syncFoldAllBtn() {
  const btn = document.getElementById("btn-fold-all");
  if (!btn) return;
  const heads = groupHeads();
  const expand = heads.length > 0 && heads.every((h) => !groupState(h.dataset.gkey).open);
  btn.classList.toggle("hidden",
    sidebarView !== "grouped" || sessionSearchActive || !heads.length);
  btn.classList.toggle("expand", expand);
  const tip = expand ? "展开全部项目" : "折叠全部项目";
  btn.title = tip;
  btn.setAttribute("aria-label", tip);
}

const foldAllBtn = document.getElementById("btn-fold-all");
if (foldAllBtn) foldAllBtn.onclick = () => {
  const heads = groupHeads();
  const expand = heads.length > 0 && heads.every((h) => !groupState(h.dataset.gkey).open);
  heads.forEach((h) => { groupState(h.dataset.gkey).open = expand; });
  refreshSessionsGrouped(); // 尾部的 syncFoldAllBtn 会把按钮翻成展开态
};

async function refreshSessionsGrouped() {
  const seq = ++groupSeq;
  const [listRes, projRes] = await Promise.all([
    request("session.list", { all_projects: 1 }).catch(() => null),
    request("project.list").catch(() => ({ projects: [] })),
  ]);
  if (seq !== groupSeq) return; // 已有更新的一轮在后面，别拿旧数据盖新
  if (!listRes) {
    // 远程客户端拿不到跨项目列表（安全口径同 scope=all）：退回经典视图，
    // 偏好一并还原，免得远程端每次进来都要退一次
    sidebarView = "classic";
    applySidebarView();
    saveUiPrefs({ sidebar_view: null });
    addNotice("分组视图只在本机桌面端可用，已切回经典视图");
    refreshSessions();
    return;
  }
  const projects = projRes.projects || [];
  const sessions = listRes.sessions || [];
  // 按 project_id 分桶；不属于任何已列项目的（含 NULL 的快聊）收进结尾的兜底组
  const byProject = new Map();
  sessions.forEach((s) => {
    const k = s.project_id == null ? "quick" : s.project_id;
    if (!byProject.has(k)) byProject.set(k, []);
    byProject.get(k).push(s);
  });
  const ul = document.getElementById("session-list");
  const frag = document.createDocumentFragment();
  // 组内会话应用拖动保存的自定义序（orderedSessionList：保存过的排前，新会话按时间追加）
  byProject.forEach((list, k) => {
    byProject.set(k, orderedSessionList(list, k));
    lastGroupedListByGroup.set(String(k), byProject.get(k));
  });
  // 组序保持 project.list 原序：项目在侧栏的位置稳定，不因「对话页正显示哪个
  // 项目的会话」而上跳下蹿；当前项目改用「变化时自动展开它的组」来指示——
  // 只在切到新项目那一刻展开一次，手动折叠的组不会被反复顶开。
  // 上一次的当前项目记在模块级 lastGroupedCurrentKey，跨渲染对比才知道「变化」
  const curProj = projects.find((p) => p.is_current);
  const currentKey = curProj ? curProj.id : null;
  if (currentKey != null && String(currentKey) !== String(lastGroupedCurrentKey)) {
    groupState(currentKey).open = true;
  }
  lastGroupedCurrentKey = currentKey;
  // 组头高亮跟随「正在看的会话」所在组：快聊/远程连接的会话激活时引擎工作项目
  // 不变（切目录才有意义），若高亮仍挂在当前项目上，「看到的组」与「高亮的组」
  // 就对不上（用户报的「已切到快聊、高亮还在 A」）。没有活动会话（空标签）
  // 回落引擎当前项目——空标签属于当前项目的工作上下文。切换/删除/
  // 跨项目会话点击仍按 isCurrent（引擎当前）判定，高亮纯视觉。
  // 活动会话不在本次列表（超 50 条被截/已归档）时同样回落。点了组头
  // （groupedClickGk，见下）时高亮先借给点的那个组，切会话/切标签再交回。
  const knownIds = new Set(projects.map((p) => String(p.id)));
  const sidToGroup = new Map();
  sessions.forEach((s) => {
    sidToGroup.set(String(s.id),
      s.project_id == null ? "quick"
        : (knownIds.has(String(s.project_id)) ? String(s.project_id) : "loose"));
  });
  // 点了某个组头就先亮它（点哪行亮哪行）；那个组已不在（项目被移除、孤儿
  // 清空）就丢弃，落回活动会话——免得整列一个都不亮
  if (groupedClickGk != null) {
    const looseNow = [...byProject.keys()].some(
      (k) => k !== "quick" && !knownIds.has(String(k)));
    const stillThere = groupedClickGk === "quick"
      || (groupedClickGk === "loose" ? looseNow : knownIds.has(groupedClickGk));
    if (!stillThere) groupedClickGk = null;
  }
  const activeSid = activeTab && activeTab.sid != null ? String(activeTab.sid)
    : (currentSessionId != null ? String(currentSessionId) : null);
  const highlightKey = groupedClickGk ||
    (activeSid && sidToGroup.get(activeSid)) ||
    (currentKey != null ? String(currentKey) : null);
  lastGroupedHighlightKey = highlightKey; // 「新建会话」的落点＝这里点亮的那一项
  // 快聊组按锚点插序（quick_pos）：top=项目组前、before=某项目组前、bottom=
  // 项目组后（默认）。「其他」兜底组仍固定最末，不参与拖动
  const anchor = quickAnchor(projects);
  const renderQuickGroup = () => {
    // 「快聊」分组常驻：不绑定文件夹的对话都在这里（有时候只是想聊一句、
    // 做点小任务，不需要工作目录）。组头 ＋ 一键新建快聊对话。
    renderProjectGroup(frag, {
      key: "quick", name: "快聊", list: byProject.get("quick") || [],
      isCurrent: false, rootPath: "", project: null,
      isActive: highlightKey === "quick",
      headPlus: async () => {
        const r = await request("session.new_task", {});
        await openTabForSession(r.id, r.title);
        refreshSessionsGrouped();
      },
    });
  };
  if (anchor.mode === "top") renderQuickGroup();
  projects.forEach((p) => {
    if (anchor.mode === "before" && String(p.id) === String(anchor.id)) renderQuickGroup();
    renderProjectGroup(frag, {
      key: p.id, name: p.name, list: byProject.get(p.id) || [],
      isCurrent: !!p.is_current, rootPath: p.root_path || "", project: p,
      isActive: String(p.id) === highlightKey,
    });
    byProject.delete(p.id);
  });
  if (anchor.mode === "bottom") renderQuickGroup();
  const loose = [];
  byProject.forEach((list, key) => {
    if (key !== "quick") loose.push(...list);
  });
  if (loose.length) {
    renderProjectGroup(frag, {
      key: "loose", name: "其他", list: loose,
      isCurrent: false, rootPath: "", project: null,
      isActive: highlightKey === "loose",
    });
  }
  ul.innerHTML = "";
  ul.appendChild(frag);
  renderSessionExtras(listRes);
  renderRailSessions(sessions);
  syncFoldAllBtn();
}

function renderProjectGroup(frag, { key, name, list, isCurrent, rootPath, project, headPlus, isActive }) {
  const ul = document.getElementById("session-list");
  const st = groupState(key);
  const head = document.createElement("li");
  // is-empty 保留：空组头样式钩子（无会话行可藏）；高亮（active）跟
  // 「正在看的会话所在组」走（点组头时先借给点的那个组），isCurrent 只管切换/删除等逻辑
  head.className = "pgroup-head" + (isActive ? " active" : "") +
    (st.open ? " open" : "") + (list.length ? "" : " is-empty");
  head.dataset.gkey = key; // 折叠全部/展开全部要靠它找回各组的状态
  head.innerHTML = FOLDER_SVG +
    `<span class="pg-name">${escapeHtml(name)}</span>` +
    // 「远程连接」也可从列表移除（渠道再用时自动重建）；「其他」兜底组
    // （project=null 且非快聊）仍不提供新建：那些会话本属某项目，
    // 只是项目被移除后成了孤儿，新建无意义
    (project
      ? `<button class="pg-del" title="${isCurrent ? "重置这个项目（清空会话与记录）" : "从列表中移除这个项目"}">✕</button>` : "") +
    // 兜底组「其他」（project=null 且非快聊）不提供新建：那些会话本属某项目，
    // 只是项目被移除后成了孤儿，新建无意义
    (headPlus || project
      ? `<button class="pg-add" title="${headPlus ? "新建快聊（不需要文件夹，随时能聊）" : "新建会话"}">＋</button>` : "");
  // 组头交互：整行点击展开/折叠；右侧＋新建会话（箭头已移除，展开态靠组下
  // 会话行本身可见）。＋行为按组分流：快聊保持原 headPlus；当前项目=新标签
  // （发送第一条消息时才落库，见 create_task_chat）；其他项目先切过去再新建；
  // 远程连接没有工作目录，打开它名下最近的对话。
  head.title = `${name} —— 点击展开/折叠该项目下的会话；点＋新建会话`;
  const addBtn = head.querySelector(".pg-add");
  if (addBtn) {
    addBtn.onclick = async (e) => {
      e.stopPropagation();
      try {
        if (headPlus) {
          // 快聊组：沿用自带的新建流程
          await headPlus();
        } else if (isCurrent) {
          // 当前项目：立即落库一个新会话（无需先切；startNewTab 自带侧栏刷新）
          startNewTab();
        } else if (rootPath) {
          // 其他项目：先切工作项目再新建（applyWorkspaceData 会重画侧栏）
          await request("project.switch", { path: rootPath });
          await applyWorkspaceData(await fetchWorkspaceData());
          startNewTab();
        } else {
          // 远程连接：无工作目录，打开最近的渠道对话
          await switchToGroupProject(project, name, rootPath);
        }
      } catch (e2) {
        addNotice("新建会话失败：" + e2.message);
      }
    };
  }
  // 行点击＝展开/折叠＋把高亮收过来（点哪行亮哪行；切换项目走经典视图或会话行；
  // ＋/✕ 自行 stopPropagation）
  head.onclick = () => {
    st.open = !st.open;
    groupedClickGk = String(key);
    refreshSessionsGrouped();
  };
  // 拖动与点击是两套手势：拖组头换位仍走整行 dragstart，不受行点击影响。
  // 快聊是伪组也参与排序（位置记 quick_pos 锚点）；「其他」兜底组固定最末不接
  if (project || key === "quick") wireGroupDrag(head, key);
  wireRowKeyboard(head); // 组头也是一行：键盘 Enter/空格同样展开/折叠
  const del = head.querySelector(".pg-del");
  if (del) del.onclick = (e) => { e.stopPropagation(); deleteProjectModal(project); };
  frag.appendChild(head);
  if (!st.open) return;
  const visible = st.all ? list : list.slice(0, GROUP_PREVIEW);
  visible.forEach((s) => {
    const li = renderSessionItem(s, ul);
    if (rootPath && !isCurrent) {
      // 其他项目的会话不能在当前项目里直接激活：先切工作项目再打开
      // （同「全部项目」搜索命中的流程），覆盖 renderSessionItem 的默认点击
      li.title = `属于项目「${name}」—— 点击切换过去并打开`;
      li.onclick = async () => {
        // 先记住目标会话再切项目：切完的列表重画会照它恢复选中高亮
        activeSessionSid = s.id;
        try {
          await request("project.switch", { path: rootPath });
          await applyWorkspaceData(await fetchWorkspaceData());
        } catch (e) {
          addNotice("切换到该项目失败：" + e.message);
          return;
        }
        openTabForSession(s.id, s.title);
        clearTodoPanel();
        addNotice(`已打开会话 ${s.title || s.id}`);
      };
    }
    wireSessionDrag(li, s, list, key);
    frag.appendChild(li);
  });
  if (list.length > visible.length || (st.all && list.length > GROUP_PREVIEW)) {
    const more = document.createElement("li");
    more.className = "pgroup-more";
    if (st.all) {
      more.textContent = "收起";
      more.onclick = () => { st.all = false; refreshSessionsGrouped(); };
    } else {
      more.textContent = `显示更多 ${list.length - visible.length} 个`;
      more.onclick = () => { st.all = true; refreshSessionsGrouped(); };
    }
    frag.appendChild(more);
  }
}

// ---- 拖动排序（分组视图组头 / 组内会话行 / 经典视图项目行、会话行共用）----
// 顺序存 ui.json 的 project_order / session_order，拖拽只改顺序不改数据本身；
// 两种视图共享同一份顺序，分组视图调好序经典视图同享。

/** 通用拖拽接线：手势部分（start/over/leave/end）全在这，落点重定向与
    提交由 opts 决定：
      over(e, item, src) -> 悬停时的落点 { id, pos } 或 null（不允许落/不响应）；
                            不传则默认上/下缘对半判定；id 默认 item.id
      commit(dst, pos)   -> dragend 里真正落库，返回 promise，resolve 后 rerender()
      rerender()         -> 保存成功后的重渲染
    进行态存 wireListDrag.active（dataTransfer 只在 drop 可读，跨事件传状态用模块级） */
function wireListDrag(el, item, opts) {
  el.draggable = true;
  el.addEventListener("dragstart", (e) => {
    wireListDrag.active = { item, over: null, tag: opts.tag || null };
    el.classList.add("dragging", "sess-dragging");
    e.dataTransfer.effectAllowed = "move";
    e.dataTransfer.setData("text/plain", String(item.id ?? "")); // Firefox：不 setData 不起拖
  });
  el.addEventListener("dragend", () => {
    el.classList.remove("dragging", "sess-dragging");
    const st = wireListDrag.active;
    wireListDrag.active = null;
    clearDragVisual();
    clearSessionDragVisual();
    if (!st || !st.over) return;
    if (st.over.id !== item.id && opts.commit) {
      Promise.resolve(opts.commit(st.over, st.over.pos || "before")).then(() => {
        if (opts.rerender) opts.rerender();
      });
    }
  });
  el.addEventListener("dragover", (e) => {
    const st = wireListDrag.active;
    if (!st || st.item.id === item.id) return;
    const t = opts.over ? opts.over(e, item, st.item)
      : { id: item.id, pos: dragHalfPos(e, el) };
    if (!t || t.id === st.item.id) return;
    e.preventDefault(); // 允许作为落点
    e.dataTransfer.dropEffect = "move";
    if (st.over && st.over.id === t.id && st.over.pos === t.pos) return;
    clearDragVisual();
    clearSessionDragVisual();
    st.over = t;
    el.classList.add(t.pos === "before" ? "drop-above" : "drop-below");
  });
  el.addEventListener("dragleave", () => {
    const st = wireListDrag.active;
    if (st && st.over && st.over.id === item.id) {
      el.classList.remove("drop-above", "drop-below");
      st.over = null;
    }
  });
  el.addEventListener("drop", (e) => e.preventDefault()); // 提交统一在 dragend
}
wireListDrag.active = null;

function dragHalfPos(e, el) {
  const rect = el.getBoundingClientRect();
  return e.clientY < rect.top + rect.height / 2 ? "before" : "after";
}

/** 分组视图组头＋（远程连接分支用）：固定项目不可切工作目录，打开它名下
    最近的会话——与经典视图点「远程连接」同一行为。 */
async function switchToGroupProject(project, name, rootPath) {
  if (!rootPath) {
    // 远程连接：无真实目录，切工作目录无从谈起，直接开最近的渠道会话
    const r = await request("session.list", { all_projects: 1 }).catch(() => null);
    const list = ((r && r.sessions) || [])
      .filter((s) => s.project_id === project.id)
      .sort((a, b) => (b.updated_at || 0) - (a.updated_at || 0));
    if (!list.length) {
      addNotice("「远程连接」还没有会话——在飞书/微信里给机器人发条消息就会出现在这里");
      return;
    }
    await openTabForSession(list[0].id, list[0].title);
    refreshSessionsGrouped();
    return;
  }
  try {
    await switchProject(rootPath);
  } catch (e) {
    addNotice("切换失败: " + e.message);
  }
}

/** 给组头接上拖拽（分组视图）。head 的点击行为（折叠）不受影响：拖拽与点击是两套手势。 */
function wireGroupDrag(head, key) {
  wireListDrag(head, { id: String(key) }, {
    // commitProjectOrder 的签名是 (srcKey, dstKey, pos)：第一个参数必须是
    // 被拖的那个组，落点组与落点位置随后——曾经漏传 srcKey、把 pos 当成了
    // dstKey，indexOf 必然落空直接早退，分组视图拖组头从未真正保存过
    commit: (dst, pos) => commitProjectOrder(String(key), dst.id, pos),
    rerender: refreshSessionsGrouped,
  });
}

function clearDragVisual() {
  document.querySelectorAll("#session-list .pgroup-head.drop-above, #session-list .pgroup-head.drop-below, #project-list li.drop-above, #project-list li.drop-below, #chat-tabs .chat-tab.drop-left, #chat-tabs .chat-tab.drop-right")
    .forEach((el) => el.classList.remove("drop-above", "drop-below", "drop-left", "drop-right"));
}

/** 项目新序提交：listId 决定从哪个列表读当前 DOM 序（分组视图从组头、
    经典视图从项目行），两种视图保存同一份 project_order。
    快聊（quick）一起收集参与排序——它没有项目 id，落库时从最终序反拆：
    项目序照旧存 project_order，快聊的位置单独存 quick_pos 锚点（top/bottom/
    {before: 下一行的项目 id}），渲染时按锚点插回（quickAnchor）。「其他」
    （loose）兜底组不进任何偏好：拖到它上面按垫底处理。 */
function commitProjectOrder(srcKey, dstKey, pos, listId = "session-list", rowSel = ".pgroup-head", attr = "gkey") {
  const keys = [...document.querySelectorAll(`#${listId} ${rowSel}`)]
    .map((h) => String(h.dataset[attr]))
    .filter((k) => k && k !== "loose"); // loose 垫底不进序；quick 参与排序
  if (dstKey === "loose") { // 拖到「其他」上＝放到当前垫底组的后面
    dstKey = keys[keys.length - 1];
    pos = "after";
  }
  const from = keys.indexOf(srcKey);
  let to = keys.indexOf(dstKey);
  if (from < 0 || to < 0 || from === to) return Promise.resolve();
  keys.splice(from, 1);
  to = keys.indexOf(dstKey); // 抽走 src 后下标可能前移，重找
  if (pos === "after") to += 1;
  keys.splice(to, 0, srcKey);
  const qi = keys.indexOf("quick");
  const prefs = {
    project_order: keys.filter((k) => k !== "quick").map((k) => parseInt(k, 10)).filter((n) => n > 0),
  };
  if (qi === 0) prefs.quick_pos = "top";
  else if (qi === keys.length - 1) prefs.quick_pos = "bottom";
  else prefs.quick_pos = { before: parseInt(keys[qi + 1], 10) }; // quick 与下一行间无 loose，下一行必是项目
  quickPosPref = prefs.quick_pos; // 本地同步：重渲染（rerender）前锚点已生效
  return request("ui.save", { prefs }).catch(() => {});
}

/** 给会话行接上组内拖拽（分组/经典两种视图共用）。同级限制：└/⑂ 子会话跟随父
    （作为一整个家族块）参与排序；拖到子行上时落点重定向到它的父（即插到该
    家族块的前/后），不会插进父与子之间。经典视图必须传 view="classic"——
    commitSessionOrder 靠它决定沿 DOM 收集会话行的方式，漏传会按分组视图的
    .pgroup-head 去收集，经典视图里一个都找不到，拖动永远空转 */
function wireSessionDrag(li, s, list, gkey, rerender, view = "grouped") {
  wireListDrag(li, s, {
    tag: "session", // 对话区的落点认这个标记：会话行拖进对话区 = 转分屏打开
    over: (e, item, dragged) => {
      // 同级判定：拖到子行上重定向到父；同家族块不响应。
      // 注意三个来源别搞混：item＝被悬停的行（本闭包绑定的会话），
      // dragged＝wireListDrag 传进来的拖动源（st.item）——家族比较必须是
      // 「拖动源的家族 vs 悬停行的家族」。曾经误用本闭包的 srcFamily（＝
      // 悬停行自己的家族）自己比自己，永远相等，over 永远返回 null，
      // 两种视图的会话拖动从诞生起就没生效过
      const overRow = findParentInGroup(list, item);
      const overFamily = overRow ? overRow.id : item.id;
      const draggedRow = dragged ? findParentInGroup(list, dragged) : null;
      const draggedFamily = draggedRow ? draggedRow.id : (dragged ? dragged.id : null);
      if (!draggedFamily || overFamily === draggedFamily) return null;
      return { id: overFamily, pos: dragHalfPos(e, li) };
    },
    commit: (dst, pos) => commitSessionOrder(String(gkey), s.id, dst.id, pos, view),
    rerender: rerender || refreshSessionsGrouped,
  });
}

/** 快聊 / 远程连接列表（renderPlainList 渲染）的拖拽接线：按 sid 找回行元素，
    套上与会话行同一套 wireSessionDrag（列表内重排 + 拖出进对话区=分屏）。
    renderPlainList 的行结构对这里透明——没有 data-sid 的行（空态提示等）自然跳过。 */
function wirePlainListDrag(ul, ordered, gkey) {
  ordered.forEach((s) => {
    const li = ul.querySelector('li[data-sid="' + String(s.id) + '"]');
    if (li) wireSessionDrag(li, s, ordered, gkey, () => refreshSessions(), "classic");
  });
}

function clearSessionDragVisual() {
  document.querySelectorAll("#session-list li.drop-above, #session-list li.drop-below")
    .forEach((el) => el.classList.remove("drop-above", "drop-below"));
}

/** 会话新序提交（同级限制，分组/经典两种视图共用）：先按家族块（└/⑂ 子跟随父）
    把可见行归并为顶层块序，再按落点移动整个块；子行永远跟在父后，不会被拖散。
    经典视图传 view="classic"：整张 session-list 就是当前项目的会话（中间可能夹
    标签分组头，按 data-sid 收集即可），gkey 用 bootSnap.project_id。
    返回保存 promise。 */
function commitSessionOrder(gkey, srcId, dstId, pos, view = "grouped") {
  const gk = String(gkey);
  const listSel = view === "classic" ? "#session-list li[data-sid]"
                                     : "#session-list .pgroup-head";
  let visibleIds;
  if (view === "classic") {
    visibleIds = [...document.querySelectorAll(listSel)].map((el) => el.dataset.sid);
  } else {
    visibleIds = [...document.querySelectorAll("#session-list .pgroup-head")]
      .filter((h) => String(h.dataset.gkey) === gk)
      .flatMap((h) => {
        const out = [];
        let el = h.nextElementSibling;
        while (el && !el.classList.contains("pgroup-head")) {
          if (el.dataset && el.dataset.sid) out.push(el.dataset.sid);
          el = el.nextElementSibling;
        }
        return out;
      });
  }
  if (!visibleIds.includes(srcId) || !visibleIds.includes(dstId)) return Promise.resolve();
  // 用渲染时的组列表做家族归并（DOM 无标题信息）；lastGroupedListByGroup 由两种
  // 视图各自的渲染函数在渲染前写入（经典视图键 = 当前项目 id）
  const list = lastGroupedListByGroup.get(gk) || [];
  const byId = new Map(list.map((s) => [s.id, s]));
  const blockOf = new Map(); // 会话 id -> 块代表（顶层父）id
  const blockMembers = new Map(); // 块代表 id -> [父, ...子]（渲染序）
  for (const id of visibleIds) {
    const row = byId.get(id);
    if (!row || blockOf.has(id)) continue;
    blockOf.set(id, id);
    blockMembers.set(id, [id]);
    for (const cand of list) {
      if (blockOf.has(cand.id) || cand.id === id) continue;
      const pp = findParentInGroup(list, cand);
      if (pp && pp.id === id && visibleIds.includes(cand.id)) {
        blockOf.set(cand.id, id);
        blockMembers.get(id).push(cand.id);
      }
    }
  }
  const srcBlock = blockOf.get(srcId);
  const dstBlock = blockOf.get(dstId);
  if (srcBlock == null || dstBlock == null || srcBlock === dstBlock) return Promise.resolve();
  const heads = visibleIds.filter((id) => blockOf.get(id) === id);
  const from = heads.indexOf(srcBlock);
  let to = heads.indexOf(dstBlock);
  if (from < 0 || to < 0) return Promise.resolve();
  heads.splice(from, 1);
  to = heads.indexOf(dstBlock);
  if (pos === "after") to += 1;
  heads.splice(to, 0, srcBlock);
  // 块序展开回会话序（每块：父在前、子随后）；隐藏行（不在本次可见名单的）补尾
  const orderedIds = heads.flatMap((h) => blockMembers.get(h) || [h]);
  const merged = [...new Set([...orderedIds, ...(sessionOrderPrefs[gk] || [])])];
  sessionOrderPrefs[gk] = merged;
  return request("ui.save", { prefs: { session_order: sessionOrderPrefs } }).catch(() => {});
}

// 「引用此会话」：挂进输入框的引用托盘，随下一条消息把该会话内容注入上下文。
// 主栏与分屏列共用一条通路（pane 空 = 主栏）：右键菜单与拖拽入输入栏都汇到这里
function addSessionRef(sid, title, pane) {
  const cur = pane ? pane.sid : (activeTab && activeTab.sid);
  if (cur === sid) {
    addNotice("这就是当前正在对话的会话，直接提问即可");
    return false;
  }
  const list = faceRefs(pane);
  if (list.some((r) => r.id === sid)) {
    addNotice("已引用该会话");
    return false;
  }
  list.push({ id: sid, title: title || "未命名会话" });
  renderRefTray(pane);
  if (!pane) inputEl.focus();
  addNotice(`已引用「${title || "未命名会话"}」，随下一条消息发给 Agent`);
  return true;
}

function quoteThisSession(s) {
  addSessionRef(s.id, s.title, null);
}

// 归档当前会话：侧栏列表、搜索、启动续聊三处同时隐藏，可在归档弹窗恢复
async function archiveSession(s) {
  await request("session.archive", { id: s.id, archived: true });
  addNotice(`已归档「${s.title || "(未命名)"}」，点侧栏底部「归档会话」可找回`);
  refreshSessions();
}

// —— 归档弹窗：查看已归档会话，恢复或彻底删除（删除需二次确认） ——
async function openArchiveModal() {
  const r = await request("session.list_archived");
  const list = r.sessions || [];
  // 弹窗固定长宽（CSS :has(.archive-wrap)），列表区自己滚；批量操作条常驻列表上方
  const wrap = document.createElement("div");
  wrap.className = "archive-wrap";
  const bulk = document.createElement("div");
  bulk.className = "archive-bulk";
  bulk.innerHTML =
    '<label class="archive-pick"><input type="checkbox" data-f="all">全选</label>' +
    '<span class="archive-count" data-f="count">已选 0</span>' +
    '<span class="bulk-spacer"></span>';
  const restoreSel = document.createElement("button");
  restoreSel.className = "rp-mini";
  restoreSel.textContent = "恢复所选";
  const delSel = document.createElement("button");
  delSel.className = "rp-mini danger";
  delSel.textContent = "删除所选";
  bulk.appendChild(restoreSel);
  bulk.appendChild(delSel);
  const box = document.createElement("div");
  box.className = "archive-list";
  if (!list.length) {
    box.innerHTML = '<p class="dim small">没有已归档的会话。右键会话（或点 ⋯）选「归档」，它就会从侧栏消失并收进这里。</p>';
  }
  const closeIfEmpty = () => {
    if (!box.querySelector(".archive-row")) { hideModal(); refreshSessions(); }
  };
  const rowsOf = () => [...box.querySelectorAll(".archive-row")];
  const pickedOf = () =>
    rowsOf().filter((el) => el.querySelector(".archive-pick input").checked);
  let armedBatch = false; // 批量删除的二次确认：选择一变就要求重新确认
  const syncBulk = () => {
    const total = rowsOf().length;
    const n = pickedOf().length;
    const all = bulk.querySelector('[data-f="all"]');
    bulk.querySelector('[data-f="count"]').textContent = `已选 ${n}`;
    all.checked = total > 0 && n === total;
    all.indeterminate = n > 0 && n < total;
    armedBatch = false;
    delSel.textContent = n ? `删除所选（${n}）` : "删除所选";
    restoreSel.textContent = n ? `恢复所选（${n}）` : "恢复所选";
  };
  bulk.querySelector('[data-f="all"]').onchange = (e) => {
    rowsOf().forEach((el) => { el.querySelector(".archive-pick input").checked = e.target.checked; });
    syncBulk();
  };
  restoreSel.onclick = async () => {
    const rows = pickedOf();
    if (!rows.length) { addNotice("先勾选要恢复的会话"); return; }
    for (const el of rows) {
      try {
        await request("session.archive", { id: el.dataset.sid, archived: false });
        el.remove();
      } catch (e) { addNotice("恢复失败: " + e.message); }
    }
    syncBulk();
    closeIfEmpty();
  };
  delSel.onclick = async () => {
    const rows = pickedOf();
    if (!rows.length) { addNotice("先勾选要删除的会话"); return; }
    if (!armedBatch) { // 与单行删除同一套「确认删除」两步手感
      armedBatch = true;
      delSel.textContent = `确认删除 ${rows.length} 个？`;
      return;
    }
    armedBatch = false;
    const deadIds = [];
    for (const el of rows) {
      try {
        await request("session.delete", { id: el.dataset.sid });
        deadIds.push(el.dataset.sid);
        el.remove();
      } catch (e) { addNotice("删除失败: " + e.message); }
    }
    closeTabsForSids(deadIds); // 删掉的会话若开着标签，一并收掉
    syncBulk();
    closeIfEmpty();
  };
  list.forEach((s) => {
    const row = document.createElement("div");
    row.className = "archive-row";
    row.dataset.sid = s.id;
    row.innerHTML =
      '<label class="archive-pick"><input type="checkbox"></label>' +
      `<div class="archive-main"><b>${escapeHtml(s.title || "(未命名)")}</b>` +
      `<span class="archive-time">最近活跃：${fmtRuleAge(s.updated_at)}</span></div>`;
    row.querySelector("input").onchange = syncBulk;
    const ops = document.createElement("div");
    ops.className = "archive-ops";
    const restore = document.createElement("button");
    restore.className = "rp-mini";
    restore.textContent = "恢复";
    restore.onclick = async () => {
      await request("session.archive", { id: s.id, archived: false });
      row.remove();
      syncBulk();
      closeIfEmpty();
    };
    const del = document.createElement("button");
    del.className = "rp-mini danger";
    del.textContent = "删除";
    let armed = false;
    del.onclick = async () => {
      if (!armed) { armed = true; del.textContent = "确认删除"; return; }
      try { await request("session.delete", { id: s.id }); }
      catch (e) { addNotice("删除失败: " + e.message); return; }
      closeTabsForSids([s.id]); // 删掉的会话若开着标签，一并收掉
      row.remove();
      syncBulk();
      closeIfEmpty();
    };
    ops.appendChild(restore);
    ops.appendChild(del);
    row.appendChild(ops);
    box.appendChild(row);
  });
  if (list.length) wrap.appendChild(bulk); // 无条目时不摆批量操作条
  wrap.appendChild(box);
  syncBulk();
  showModal("归档会话", wrap, async () => {}, "关闭");
}

// ---------- 会话搜索（标题 + 消息全文，对标 Claude Code /resume 检索） ----------
const searchEl = document.getElementById("session-search");
let searchTimer = null;
searchEl.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => renderSessionList(searchEl.value.trim()), 200);
});

// 搜索命中高亮：先转义再逐段包 <mark>，大小写不敏感
function highlightSnippet(text, q) {
  text = String(text || "");
  if (!q) return escapeHtml(text);
  const low = text.toLowerCase();
  const ql = q.toLowerCase();
  let out = "";
  let i = 0;
  for (;;) {
    const p = low.indexOf(ql, i);
    if (p < 0) {
      out += escapeHtml(text.slice(i));
      break;
    }
    out += escapeHtml(text.slice(i, p)) +
      "<mark>" + escapeHtml(text.slice(p, p + q.length)) + "</mark>";
    i = p + q.length;
  }
  return out;
}

async function renderSessionList(query) {
  const ul = document.getElementById("session-list");
  if (!query) {
    sessionSearchActive = false;
    refreshSessions();
    return;
  }
  sessionSearchActive = true;
  // 搜索固定跨全部项目（含快聊）：一个范围少一个开关，跨项目命中自带项目名标注、
  // 点击自动切过去；切换器已删（2026-09-22），分组视图此前本就固定 all，两视图统一。
  const r = await request("session.search", { query, scope: "all" })
    .catch(() => ({ results: [] }));
  ul.innerHTML = "";
  syncFoldAllBtn(); // 搜索结果是平铺列表，没有组可折，「折叠全部」随之隐藏
  if (!r.results.length) {
    ul.innerHTML = '<li class="empty-hint">所有项目里都没有匹配的会话或消息</li>';
    revealSidebarListHead(ul);
    return;
  }
  r.results.forEach((s) => {
    const li = document.createElement("li");
    li.className = "has-snippet";
    li.innerHTML =
      `<span class="s-title">${escapeHtml(s.title || "(未命名)")}</span>` +
      // 搜索跨全部项目，标出来自哪个项目，否则一堆同名标题分不清
      `<span class="s-project dim small" title="${escapeHtml(s.project_path || "")}">📁 ${escapeHtml(s.project_name || "")}</span>` +
      `<span class="s-snippet">${highlightSnippet(s.snippet, query)}</span>`;
    li.title = s.seq != null
      ? "点击打开这个会话并跳到命中消息"
      : "点击打开这个会话";
    // 命中消息的 seq（旧后端不返回时为 undefined）：只在「这条结果本身是消息命中」
    // 时启用跳转。仅标题命中（无 seq）保持原行为只打开会话。
    const hitSeq = s.seq != null ? Number(s.seq) : null;
    li.onclick = async () => {
      // 跨项目命中：先切工作项目再打开，否则会话在当前项目列表里找不到
      if (s.project_path &&
          s.project_path.toLowerCase() !== (bootSnap?.working_dir || "").toLowerCase()) {
        try {
          await request("project.switch", { path: s.project_path });
          await applyWorkspaceData(await fetchWorkspaceData());
        } catch (e) {
          addNotice("切换到该项目失败：" + e.message);
          return;
        }
      }
      searchEl.value = "";
      const t = openTabForSession(s.session_id, s.title);
      clearTodoPanel();
      addNotice(`已打开会话 ${s.title || s.session_id}`);
      refreshSessions();
      // 命中消息跳转：等历史加载完再定位。openTabForSession 的 loadTabHistory
      // 是异步的（标签已激活但消息可能还没画完），所以轮询等待目标 seq 出现，
      // 超时静默放弃（跳转失败不该挡着会话打开）。
      if (hitSeq != null) jumpToMessageSeq(t, hitSeq, { query });
    };
    wireRowKeyboard(li); // 搜索结果行同样是 li：键盘也能打开命中的会话
    ul.appendChild(li);
  });
  revealSidebarListHead(ul);
}

// 矮视口下侧栏在 .side-nav 里滚动（滚动条不滚动时不显示）：搜索结果 / 空态
// 画在视口外时看起来像「没有结果」。画完把首行滚进可视区——首行本来就在
// 视口内时不动作，不打扰正常布局。
function revealSidebarListHead(ul) {
  const first = ul && ul.firstElementChild;
  if (first && first.getBoundingClientRect().bottom > window.innerHeight) {
    first.scrollIntoView({ block: "nearest" });
  }
}

/** 搜索结果 → 跳到命中消息：滚动到目标 seq 的消息并闪烁高亮。
 *
 * 历史窗口化（后端只下发尾部窗口，更早的经 messages.earlier 翻页）后，命中
 * 可能落在未加载的更早区间：先看当前已画节点，没有就按批「加载更早的消息」向前补画，
 * 直到目标出现或到底（补画上线防止极端会话里长循环）。消息画出来后
 * 再滚进视口居中并加 .msg-hit-flash 闪一下，用户立刻看得到落点。
 *
 * 轮询等待历史：openTabForSession 内部的历史拉取是异步的，调用时节点
 * 可能还没画完；jumpSeq.wait 以 60ms 间隔重试，至多 MJUMP_WAIT ms。
 * 同一标签并发跳转时后一次覆盖前一次（_jumpSeqGen 代际作废旧轮询）。
 */
const MESSAGE_JUMP_WAIT_MS = 4000;
const MESSAGE_JUMP_FLASH_MS = 1800;
const MESSAGE_JUMP_MAX_PRELOAD_ROUNDS = 60; // 每轮 HISTORY_CHUNK 条，上限 6000 条

function jumpToMessageSeq(tab, seq, opts = {}) {
  if (!tab || seq == null) return;
  const gen = (tab._jumpSeqGen = (tab._jumpSeqGen || 0) + 1);
  const t0 = Date.now();
  tab._jumpPreloadRounds = 0; // 本次跳转的补画轮数配额
  const attempt = () => {
    if (!tab._jumpSeqGen || tab._jumpSeqGen !== gen) return; // 已被更新跳转取代
    if (!tab.logEl) return;
    const el = tab.logEl.querySelector(`.msg[data-seq="${seq}"]`);
    if (el) { paintJumpTarget(tab, el, opts); return; }
    // 没画到：可能 (a) 历史还在异步拉取，(b) 命中在窗口外（更早区间）。
    // (b) 有「加载更早」按钮时点它经 messages.earlier 补一批（在途翻页复用
    // 同一 Promise，不会重复发请求）；(a) 没有按钮就再等等。
    // 轮数配额防极端会话里无限补画（每轮 HISTORY_CHUNK 条）。
    const more = tab.logEl.querySelector(".history-more");
    if (more && tab._historyStart > 0) {
      if (tab._historyLoading) { setTimeout(attempt, 60); return; } // 翻页在途：等下一拍
      tab._jumpPreloadRounds += 1;
      if (tab._jumpPreloadRounds <= MESSAGE_JUMP_MAX_PRELOAD_ROUNDS) {
        loadEarlierHistory(tab).then(() => setTimeout(attempt, 0)); // 画完下一拍再看
        return;
      }
    }
    if (Date.now() - t0 < MESSAGE_JUMP_WAIT_MS) {
      setTimeout(attempt, 60);
    }
  };
  setTimeout(attempt, 30);
}

function paintJumpTarget(tab, el, opts) {
  if (!el || !el.isConnected) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("msg-hit-flash");
  void el.offsetWidth; // 强制回流以重启动画
  el.classList.add("msg-hit-flash");
  setTimeout(() => el.classList.remove("msg-hit-flash"), MESSAGE_JUMP_FLASH_MS);
  // 命中词在消息里就顺手帮用户标上（对话内查找的同一效果，不占查找条）
  const q = String((opts || {}).query || "").trim();
  if (q.length >= 1 && q.length <= 80) showSearchHitInMessage(el, q);
}

/** 在单条消息里把搜索词包成临时高亮（仅视觉，不作 DOM 结构改动以外的事情）。
 * 与对话内查找（Ctrl+F）不同：只处理一条消息、 5s 后自动清除；不碰 pre/code。*/
function showSearchHitInMessage(msg, query) {
  const q = query.toLowerCase();
  const walker = document.createTreeWalker(msg, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(q)) return NodeFilter.FILTER_REJECT;
      const p = node.parentNode;
      if (!p) return NodeFilter.FILTER_REJECT;
      const tag = p.nodeName.toLowerCase();
      if (tag === "pre" || tag === "code" || tag === "script" || tag === "style") return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const targets = [];
  while (walker.nextNode()) targets.push(walker.currentNode);
  const marks = [];
  targets.forEach((node) => {
    const text = node.nodeValue;
    const low = text.toLowerCase();
    const frag = document.createDocumentFragment();
    let i = 0;
    for (;;) {
      const p = low.indexOf(q, i);
      if (p < 0) { frag.appendChild(document.createTextNode(text.slice(i))); break; }
      if (p > i) frag.appendChild(document.createTextNode(text.slice(i, p)));
      const mark = document.createElement("mark");
      mark.className = "search-hit-mark";
      mark.textContent = text.slice(p, p + query.length);
      marks.push(mark);
      frag.appendChild(mark);
      i = p + query.length;
    }
    node.parentNode.replaceChild(frag, node);
  });
  if (!marks.length) return;
  setTimeout(() => {
    marks.forEach((m) => {
      const parent = m.parentNode;
      if (!parent) return;
      parent.replaceChild(document.createTextNode(m.textContent), m);
      parent.normalize();
    });
  }, MESSAGE_JUMP_FLASH_MS + 2200);
}

// ---------- 会话操作菜单（⋯ 按钮或右键唤出） ----------
const menuEl = document.getElementById("session-menu");

/** 弹层落点：默认在指针下方展开；下方放不下（会被视口底缘裁掉）就整体翻到
    指针上方；上下都放不下（指针贴边或菜单比视口还高）才贴向较近的一缘，
    并限高内部滚动兕底。此前两处调用各自写死估高（-240/-140）往下钳：估高
    远小于会话菜单实高（十几项），侧栏底部右键仍然裁掉后半截——改为展开后
    量实际尺寸再定位。pos 与 window 尺寸是物理像素，菜单自带 zoom 定位在
    缩放布局坐标系，结论算完再除回 uiScale 落 style。 */
function placeMenuAt(menu, pos) {
  menu.style.bottom = "auto";
  menu.style.right = "auto";
  menu.style.maxHeight = ""; // 上一次「比视口还高」留下的限高要先撤掉再量
  menu.style.overflowY = "";
  const m = 8; // 视口边缘安全距离（物理像素）
  // 量布局尺寸而非 getBoundingClientRect：浮层有 ui-pop-in 入场动画（scale/translate），
  // 开菜单第一帧的 rect 是动画中间态，向上翻时会差出几像素；offset* 是不受
  // transform 影响的布局盒（已含 zoom），× uiScale 还原成物理像素
  const hPhys = menu.offsetHeight * uiScale, wPhys = menu.offsetWidth * uiScale;
  let topPhys;
  if (pos.y + hPhys <= window.innerHeight - m) {
    topPhys = pos.y; // 下方放得下：向下展开
  } else if (pos.y - hPhys >= m) {
    topPhys = pos.y - hPhys; // 上方放得下：向上翻
  } else {
    // 上下都放不下：限高内部滚动，菜单贴向指针较近的一缘
    const avail = window.innerHeight - 2 * m;
    menu.style.maxHeight = avail / uiScale + "px";
    menu.style.overflowY = "auto";
    topPhys = pos.y > window.innerHeight / 2 ? window.innerHeight - m - avail : m;
  }
  const xPhys = Math.max(m, Math.min(pos.x, window.innerWidth - m - wPhys));
  menu.style.top = topPhys / uiScale + "px";
  menu.style.left = xPhys / uiScale + "px";
}

// 线条风小图标（stroke 继承文字色，与界面线稿风格一致）
function svgIcon(name) {
  const P = {
    quote: '<path d="M13.9 3.2H2.1a.9.9 0 0 0-.9.9v6.3a.9.9 0 0 0 .9.9h2.6v2.6l3.3-2.6h5.9a.9.9 0 0 0 .9-.9V4.1a.9.9 0 0 0-.9-.9z"/>',
    pin: '<path d="M5.5 2.5h5v3l1.5 2.5H4l1.5-2.5v-3z"/><path d="M8 9v4.5"/>',
    move: '<path d="M2.5 5H12m0 0L9.5 2.5M12 5 9.5 7.5"/><path d="M13.5 11H4m0 0 2.5-2.5M4 11l2.5 2.5"/>',
    rename: '<path d="M11.2 2.4l2.4 2.4L6 12.4l-3.2.8.8-3.2 7.6-7.6z"/>',
    trash: '<path d="M2.5 4h11M6.5 4V2.8h3V4M4.2 4l.6 9h6.4l.6-9"/><path d="M6.6 6.5v4.5M9.4 6.5v4.5"/>',
    archive: '<path d="M2.5 2.8h11v2.4h-11z"/><path d="M3.6 5.2v7.1a.8.8 0 0 0 .8.8h7.2a.8.8 0 0 0 .8-.8V5.2"/><path d="M6.3 8.1h3.4"/>',
    tag: '<path d="M2.5 3.5h4.4l6.6 6.6-4.4 4.4-6.6-6.6z"/><circle cx="5.8" cy="5.8" r="1"/>',
    folder: '<path d="M1.8 4.2a.9.9 0 0 1 .9-.9h3.4l1.5 1.7h5.7a.9.9 0 0 1 .9.9v6a.9.9 0 0 1-.9.9H2.7a.9.9 0 0 1-.9-.9z"/>',
    copy: '<rect x="5.4" y="5.4" width="8" height="8" rx="1"/><path d="M10.6 5.4V3.6a1 1 0 0 0-1-1H3.6a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h1.8"/>',
    split: '<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1"/><path d="M8 2.8v10.4"/>',
  };
  return '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"' +
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + P[name] + "</svg>";
}

function showSessionMenu(s, li, pos) {
  menuEl.innerHTML = "";
  const items = [
    { icon: "quote", label: "引用此会话", act: () => quoteThisSession(s) },
    {
      icon: "pin",
      label: s.pinned ? "取消置顶" : "置顶会话",
      act: async () => {
        await request("session.pin", { id: s.id, pinned: !s.pinned });
        refreshSessions();
      },
    },
    { icon: "tag", label: "编辑标签", act: () => editSessionTags(s) },
    { icon: "move", label: "迁移到其他项目", act: () => moveSessionModal(s) },
    {
      icon: "archive",
      label: s.archived ? "取消归档" : "归档会话",
      act: async () => {
        await request("session.archive", { id: s.id, archived: !s.archived });
        // 归档后侧栏里找不到它了：留一句去哪找回；恢复回列表是可见的，不再提示
        if (!s.archived) addNotice(`已归档「${s.title || "(未命名)"}」，点侧栏底部「归档会话」可找回`);
        refreshSessions();
      },
    },
    { icon: "rename", label: "重命名", act: () => startInlineRename(s, li) },
    {
      icon: "split",
      label: "在分屏打开",
      act: () => openSplitPane(s.id, s.title),
    },
    // 定位/复制类：快聊（fast-talk 专用文件夹）/远程连接（remote-control）/项目会话
    // 都有真实工作目录，这两项对所有会话开放
    ...([
      {
        icon: "folder",
        label: "在资源管理器中打开",
        act: async () => {
          try { await request("session.reveal", { id: s.id }); }
          catch (e) { addNotice(e.message); }
        },
      },
      {
        icon: "copy",
        label: "复制项目路径",
        act: async () => {
          try {
            const r = await request("session.project_path", { id: s.id });
            if (!r.path) { addNotice("这个会话没有工作目录"); return; }
            await copyTextToClipboard(r.path);
            addNotice("已复制工作目录路径");
          } catch (e) { addNotice(e.message); }
        },
      },
    ]),
    {
      icon: "copy",
      label: "复制会话 ID",
      act: async () => {
        await copyTextToClipboard(s.id);
        addNotice("已复制会话 ID");
      },
    },
    { icon: "trash", label: "删除会话", danger: true, act: () => deleteSessionModal(s) },
  ];
  items.forEach((it) => {
    const b = document.createElement("button");
    b.innerHTML = svgIcon(it.icon) + `<span>${escapeHtml(it.label)}</span>`;
    if (it.danger) b.classList.add("danger");
    b.onclick = () => {
      menuEl.classList.add("hidden");
      it.act();
    };
    menuEl.appendChild(b);
  });
  menuEl.classList.remove("hidden");
  placeMenuAt(menuEl, pos);
}
document.addEventListener("click", (e) => {
  if (!menuEl.contains(e.target)) menuEl.classList.add("hidden");
});
document.addEventListener("scroll", () => menuEl.classList.add("hidden"), true);

