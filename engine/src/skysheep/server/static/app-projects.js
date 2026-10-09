// app-projects.js —— 项目列表（侧栏项目区：切换 / 删除 / 快聊 / 渲染刷新）
// 从 app.js 拆出的分区文件：同为零构建、普通 script、不引入 ES modules，
// 本文件只有声明与纯数据常量，零加载期执行。index.html 先载本文件再载
// app.js（app.js 的 boot 序列会调用 refreshProjects）；对 request /
// addNotice / escapeHtml / showModal 等 app.js 助手的引用是运行时调用。

// ---------- 项目列表（侧栏直接展示，点击即切换，对标 Obsidian 项目列表） ----------
const FOLDER_SVG = '<svg class="p-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

/** 切换工作项目：同一套流程给侧栏列表与「工作项目」弹窗用。
 *
 *  站内切换，不整页刷新：旧界面全程留在屏幕上，后端切好 + 新项目数据拉齐后
 *  一次性替换。此前走 location.reload()，整页重载必然先画出一屏空 DOM，
 *  不管拿什么遮罩去盖，用户看到的都是一屏「加载中」。
 *
 *  失败（有任务在跑 / 目录不存在）时界面完全不动，只弹提示。 */
let switchingProject = false;
async function switchProject(path) {
  if (switchingProject) return;
  switchingProject = true;
  setSwitchBusy(true);
  try {
    // 1) 后端先切：这一步失败就什么都没发生，界面不受影响
    await request("project.switch", { path });
    // 2) 先取数（此时旧界面还在屏幕上），取不到就报错回退，不动界面
    const data = await fetchWorkspaceData().catch((e) => {
      throw new Error("新项目数据加载失败：" + e.message);
    });
    // 3) 数据到手 → 清旧状态 → 同步重画，清与画之间不经过网络，不会出现空列表帧
    resetWorkspaceState();
    await applyWorkspaceData(data);
    // 4) 右侧面板里与项目绑定的页按需重拉（boot 不管这些）
    reloadProjectPanels();
    addNotice(`已切换到项目「${currentProjectName() || path}」`);
    // 5) 新内容整体淡入 180ms：欢迎卡/提示条/列表从「瞬间弹入」变成平滑过渡，
    //    消除切换时大块内容突然出现又（下次切换时）突然消失的「弹窗感」
    for (const el of [
      document.querySelector("#chat > .chat-log"),
      document.getElementById("session-list"),
      document.getElementById("project-list"),
    ]) {
      if (!el) continue;
      el.classList.remove("swap-in");
      void el.offsetWidth; // 强制重排，让同一元素在连续切换时也能重放动画
      el.classList.add("swap-in");
    }
  } catch (e) {
    // 右面板可能已被 resetWorkspaceState 压暗：失败路径必须撤掉，别把变暗留在屏上
    const body = document.getElementById("rp-body");
    if (body) body.classList.remove("reloading");
    setSwitchBusy(false);
    switchingProject = false;
    throw e;
  }
  setSwitchBusy(false);
  switchingProject = false;
}

/** 切换期间只做轻量提示（顶栏状态 + 项目列表置灰），不遮挡界面。
 *
 *  指示延迟 250ms 才亮出：本地项目切换通常几十毫秒完成，指示器跟着一闪
 *  而过反而像界面在抖（用户报的「切换项目时页面闪烁」主要来源之一）。 */
function setSwitchBusy(on) {
  const list = document.getElementById("project-list");
  // 提示位挂主栏中部的快捷键提示槽（status-text 已删，连接态只留圆点）：
  // 临时置换文字、切完还原，与原先同一套一次性置换手法
  const text = document.getElementById("cp-hint");
  if (on) {
    clearTimeout(switchBusyTimer);
    switchBusyTimer = setTimeout(() => {
      switchBusyTimer = null;
      switchBusyShown = true;
      if (list) list.classList.add("busy");
      switchStatusPrev = text ? text.textContent : null;
      if (text) text.textContent = "正在切换项目…";
    }, 250);
  } else {
    if (switchBusyTimer) { clearTimeout(switchBusyTimer); switchBusyTimer = null; }
    if (switchBusyShown) {
      switchBusyShown = false;
      if (list) list.classList.remove("busy");
      if (text && switchStatusPrev != null) text.textContent = switchStatusPrev;
    }
    switchStatusPrev = null;
  }
}
let switchStatusPrev = null;
let switchBusyTimer = null;
let switchBusyShown = false;

function currentProjectName() {
  const el = document.querySelector("#project-list li.active .s-title");
  return el ? el.textContent : "";
}

/** 清空一切与"当前项目"绑定的前端状态。
 *
 *  会话标签、消息 DOM、权限条、运行态、搜索态、@ 文件索引、右侧面板缓存、
 *  设置页缓存全部作废；清完由 boot() 重建。宠物位置（petPos / pet_x）是界面偏好，
 *  跨项目保留，不能动。 */
function resetWorkspaceState() {
  // —— 会话标签与聊天流 ——
  chatTabs.forEach((t) => t.logEl.remove());
  chatTabs = [];
  activeTab = null;
  routeTab = null;
  sessionMeta = {};
  currentSessionId = null;
  chatBox.querySelectorAll(":scope > .chat-log").forEach((el) => el.remove());
  // —— 分屏列：列里的会话属于旧项目，残留只会对着死会话报错（发送/换模型
  // 都被当前项目归属校验拒）——整列收掉，#split-pane 与 has-split 让位随
  // closeSplitPane 一并复原
  [...splitPanes].forEach((p) => closeSplitPane(p, { restore: false })); // 切项目：旧项目会话不搬回

  // —— 运行态 / 权限 / 用量 ——
  hidePermission();
  running = false;
  pendingTurns = 0;
  usageIn = 0;
  usageOut = 0;
  renderUsage();
  setContextUsage(0, 0);
  document.getElementById("btn-send").textContent = "发送";
  document.getElementById("btn-stop").hidden = true;

  // —— 会话列表与搜索 ——
  sessionSearchActive = false;
  classicViewGk = null; // 旧项目“正在看快聊”的状态随切项目作废
  groupedClickGk = null; // 分组视图「点组头」的落点也按旧项目作废
  searchEl.value = "";
  document.getElementById("session-list").innerHTML = "";

  // —— 任务清单（旧项目的 todo 属于旧项目）——
  clearTodoPanel();

  // —— @ 文件索引（30 秒缓存，指向旧项目目录树）——
  fileIndex = null;

  // —— 右侧面板：与项目绑定的缓存全部作废 ——
  resetProjectPanels();

  // —— 设置页缓存（白名单、项目记忆、技能/MCP 都跟项目走）——
  providerCfg = null;
  bootSnap = null;
  availCacheModels = null;
  availNoteState = null;
}

/** 右面板里跟项目绑定的数据缓存作废（DOM 不清空，由各自 loader 拉到新数据后整块替换）。
 *
 *  此前这里先把文件树/任务等 innerHTML 清空、再等 loader 异步回填，中间隔着
 *  一段网络等待——面板先变白再「啪」地出现内容，正是切换项目时右侧闪烁的来源。
 *  现在旧内容留屏，新数据到手后一次性换上；加载变暗由 reloadProjectPanels 延迟
 *  亮出（本函数不加点，快速切换连变暗都不出现）。 */
function resetProjectPanels() {
  filesLoaded = false;
  // 切项目时待发起的文件树防抖刷新一并作废：它请求的是旧项目的 fs.files，
  // 不取消就会在新项目里把旧目录树画上来（随后被新项目的刷新覆盖，但会先闪一下）
  if (filesRefreshTimer) { clearTimeout(filesRefreshTimer); filesRefreshTimer = 0; }
  agCache = [];
  // 项目记忆属于旧项目：内容与"已加载"标记一并作废，激活时由 loader 重读。
  // 文本框是可编辑的，不能留旧项目的 AGENTS.md 在屏上（Ctrl+S 会写进新项目），
  // 所以唯独这里仍然清空；只读列表才走「留屏 + 变暗」策略
  memoryLoaded = false;
  const memText = document.getElementById("rp-memory-text");
  if (memText) memText.value = "";
  const memStatus = document.getElementById("memory-status");
  if (memStatus) memStatus.textContent = "";
  loadTasks._snap = null; // 切项目后强制任务面板下一轮重绘一次
  const reviewDiff = document.getElementById("review-diff");
  if (reviewDiff) reviewDiff.classList.add("hidden");
  const preview = document.getElementById("files-preview");
  if (preview) preview.classList.add("hidden");
  const filesResizer = document.getElementById("files-resizer");
  if (filesResizer) filesResizer.classList.add("hidden");
  // 浏览器预览若停在本项目的 /preview 相对地址上：切项目后同一路径已是新项目的
  // 文件（或 404），留着只会「看着是旧页面、实际已是别的东西」。收回空态最诚实。
  {
    const frame = document.getElementById("browser-frame");
    const viewport = document.getElementById("browser-viewport");
    const empty = document.getElementById("browser-empty");
    if (frame && viewport && empty && (frame.getAttribute("src") || "").startsWith(location.origin + "/preview")) {
      frame.removeAttribute("src");
      viewport.classList.add("hidden");
      empty.classList.remove("hidden");
      const urlBox = document.getElementById("browser-url");
      if (urlBox) urlBox.value = "";
    }
  }
  // 终端：命令是在项目目录里跑的，旧项目的输出与运行态一并作废
  resetTermTabs();
}

/** 切换后重拉右侧面板里与项目绑定的页（boot 不管这些）。

  面板收起时也照拉：拉的是数据、不占屏幕，拉完再展开时就是一屏满的；
  以前这里早退，收起状态切项目再展开会看到上一个项目的内容。

  变暗指示延迟 200ms：本地项目切换大多几十毫秒完成，立刻变暗再复原
  会形成一次灰色脉冲（也是闪烁）；真的慢（大目录/网络盘）才压暗提示。 */
async function reloadProjectPanels() {
  const body = document.getElementById("rp-body");
  if (!rightTabs.length) return;
  // 面板收起时不压暗（用户看不见，压了也白压，还会在展开那一瞬闪一下）
  const dimTimer = rightCollapsed ? 0 : setTimeout(() => {
    if (body) body.classList.add("reloading");
  }, 200);
  try {
    await Promise.allSettled(
      rightTabs.map((id) => Promise.resolve().then(() => loadRightTab(id, true))),
    );
  } finally {
    clearTimeout(dimTimer);
    // 无论 loaders 是否跑过都要撤（面板收起等早退也不能把变暗留在屏上）
    if (body) body.classList.remove("reloading");
  }
}

async function refreshProjects(prefetched) {
  const ul = document.getElementById("project-list");
  if (!ul) return;
  const { projects } = prefetched || await request("project.list").catch(() => ({ projects: [] }));
  ul.innerHTML = "";
  // 「项目」区：标题 + 右端 ＋ 常显，列表两态只体现在 ul 有没有行。
  // 空态不再留「标题 + 空提示」的死区，加号常在标题右端。
  const sec = document.getElementById("project-section");
  if (sec) sec.classList.toggle("empty", !projects.length);
  if (!projects.length) {
    applySidebarView(); // 会话区的空态文案也跟着变，与项目区同帧
    // 无项目态：项目区只剩快聊一行（快聊是一等入口），不用锚点
    if (await ensureQuickAvailable()) buildQuickRow(ul);
    return;
  }
  // 快聊行按锚点插序（quick_pos，同分组视图的快聊组）：先完成可用性探测
  // （首次要发一次请求），建行才是同步的，才能插进下面的 forEach 中间
  await ensureQuickAvailable();
  const anchor = quickAnchor(projects);
  let quickPlaced = false;
  const placeQuickRow = () => {
    if (quickPlaced || !classicQuickAvailable) return;
    quickPlaced = true;
    buildQuickRow(ul);
  };
  if (anchor.mode === "top") placeQuickRow();
  projects.forEach((p) => {
    if (anchor.mode === "before" && String(p.id) === String(anchor.id)) placeQuickRow();
    const li = document.createElement("li");
    const remote = !p.root_path;
    // 高亮唯一（项目区同时最多一行亮）：看「项目列表」时亮当前项目；看
    // 「远程连接」列表时亮它；看快聊时亮快聊行（见 buildQuickRow）。
    // classicViewGk 由点击决定；is_current 仍用于切换/删除等逻辑判定
    if (remote ? classicViewGk === "remote:" + p.id
               : (classicViewGk === "proj:" + p.id
                  || (p.is_current && classicViewGk == null))) {
      li.classList.add("active");
    }
    li.innerHTML = FOLDER_SVG + `<span class="s-title">${escapeHtml(p.name)}</span>`;
    // 「远程连接」固定项目：无真实目录（root_path 报空），不可切换/删除，
    // 它名下是飞书/微信等渠道的对话——点它陈列这组对话，不切工作目录
    li.title = remote
      ? "远程连接 —— 飞书/微信等渠道的对话都归在这里（固定项目，不可切换）；点击查看它的对话"
      : p.root_path + (p.is_current ? "（当前项目）"
        : classicViewGk === "proj:" + p.id
          ? "（正在查看该项目的对话）—— 再次点击返回当前项目列表"
          : "—— 点击查看这个项目的对话（右侧对话界面不切换）");
    if (remote) li.classList.add("remote-fixed");
    // 拖动排序：与分组视图共用 project_order（project.list 已按它返回）
    wireListDrag(li, { id: String(p.id) }, {
      commit: (dst, pos) => commitProjectOrder(String(p.id), dst.id, pos, "project-list", "li", "pid"),
      rerender: () => refreshProjects(),
    });
    li.dataset.pid = p.id; // commitProjectOrder 从 DOM 收集当前序
    // 移除按钮：当前项目也能删（清空会话与记录后用同一目录重置重新开始）；
    // 远程连接是固定项目，不提供删除
    if (!remote) {
      const del = document.createElement("button");
      del.className = "p-del";
      del.textContent = "✕";
      del.title = p.is_current ? "重置这个项目（清空会话与记录）" : "从列表中移除这个项目";
      del.onclick = (e) => {
        e.stopPropagation();
        deleteProjectModal(p);
      };
      li.appendChild(del);
    }
    // 悬浮＋（新建对话）：与分组视图组头的＋同一行为——当前项目=新标签，
    // 其他项目=先切过去再新建；远程连接无工作目录，不提供（行点击已能
    // 打开最近对话，两个入口重复）。与 ✕ 同靠悬停才现，不挤常驻空间
    if (!remote) {
      const add = document.createElement("button");
      add.className = "p-add";
      add.textContent = "＋";
      add.title = p.is_current ? "新建对话" : "切换到该项目并新建对话";
      add.onclick = async (e) => {
        e.stopPropagation();
        try {
          if (!p.is_current) {
            await request("project.switch", { path: p.root_path });
            await applyWorkspaceData(await fetchWorkspaceData());
            addNotice(`已切换到项目「${p.name}」，新建对话`);
          }
          startNewTab(); // 自带侧栏刷新
        } catch (e2) {
          addNotice("新建对话失败：" + e2.message);
        }
      };
      li.appendChild(add);
    }
    li.onclick = async () => {
      if (remote) {
        // 「远程连接」固定项目：点它=高亮它并陈列渠道对话（不改工作目录）
        classicViewGk = "remote:" + p.id;
        refreshProjects();
        refreshSessions();
        return;
      }
      if (p.is_current) {
        // 当前项目：点它=高亮它并回到本项目会话列表（从快聊/远程列表切回）
        if (classicViewGk != null) {
          classicViewGk = null;
          refreshProjects();
          refreshSessions();
        }
        return;
      }
      // 其他项目：不切换工作项目（右侧对话界面保持不动），只把它高亮并在
      // 会话区陈列该项目的对话（同快聊/远程行的视图模式）。真正的切换发生
      // 在点该项目列表里的某条对话、或行上的＋（切过去并新建）时。
      // 再次点击已查看的项目行：回到当前项目的会话列表
      if (classicViewGk === "proj:" + p.id) classicViewGk = null;
      else classicViewGk = "proj:" + p.id;
      refreshProjects();
      refreshSessions();
    };
    wireRowKeyboard(li); // 项目行键盘可达：Enter/空格同样切换/高亮
    ul.appendChild(li);
  });
  placeQuickRow();
}

/** 项目区「快聊」行的可用性探测：本机桌面端才拿得到跨项目会话列表，远程端
    记住不可用、不放这行。探测只发一次请求（结果缓存在 classicQuickAvailable）。 */
async function ensureQuickAvailable() {
  if (classicQuickAvailable == null) {
    classicQuickAvailable = await request("session.list", { all_projects: 1 })
      .then((r) => Array.isArray(r && r.sessions))
      .catch(() => false);
  }
  return classicQuickAvailable;
}

/** 建「快聊」行（同步，调用前先 ensureQuickAvailable）：不绑文件夹的对话在这里
    当项目陈列，点它高亮并在会话区陈列快聊列表（不改引擎工作项目，同分组视图的
    快聊组语义）。悬浮＋新建快聊；高亮唯一——看快聊时项目行不亮，回项目列表点
    项目行。 */
function buildQuickRow(ul) {
  // 无项目态：会话区只可能是快聊，快聊行直接算选中（未选远程时）；否则跟 classicViewGk 走
  const noProject = !(bootSnap && bootSnap.project_id != null);
  const quickActive = classicViewGk === "quick" || (noProject && classicViewGk == null);
  const li = document.createElement("li");
  if (quickActive) li.classList.add("active", "quick-row");
  else li.classList.add("quick-row");
  li.innerHTML = FOLDER_SVG + '<span class="s-title">快聊</span>';
  li.title = "快聊 —— 不绑定任何文件夹的对话；点击高亮并查看快聊列表，点＋新建；按住可拖动排序";
  // 拖动排序：与项目行同一条 wireListDrag 链路。快聊没有项目 id，pid 记
  // "quick" 伪键，commitProjectOrder 收集时参与排序、落库拆成 quick_pos 锚点
  li.dataset.pid = "quick";
  wireListDrag(li, { id: "quick" }, {
    commit: (dst, pos) => commitProjectOrder("quick", dst.id, pos, "project-list", "li", "pid"),
    rerender: () => refreshProjects(),
  });
  const add = document.createElement("button");
  add.className = "p-add";
  add.textContent = "＋";
  add.title = "新建快聊（不需要文件夹，随时能聊）";
  add.onclick = async (e) => {
    e.stopPropagation();
    try {
      const r = await request("session.new_task", {});
      classicViewGk = "quick"; // 新建的快聊会话在快聊列表里看
      await openTabForSession(r.id, r.title);
      refreshProjects();
      refreshSessions();
    } catch (e2) {
      addNotice("新建快聊失败：" + e2.message);
    }
  };
  li.appendChild(add);
  li.onclick = () => {
    // 点它=高亮快聊并陈列快聊列表（回项目列表点任一项目行）
    classicViewGk = "quick";
    refreshProjects();
    refreshSessions();
  };
  wireRowKeyboard(li); // 快聊行键盘可达
  ul.appendChild(li);
}

// 删除项目：确认后连带删掉它的会话与白名单（磁盘文件夹不动）
function deleteProjectModal(p) {
  const box = document.createElement("div");
  const cur = !!p.is_current;
  box.innerHTML =
    `<p>确定从列表中移除项目 <b>${escapeHtml(p.name)}</b> 吗？</p>` +
    (cur
      ? `<p class="dim small">这是<b>当前正在使用的项目</b>：它的全部会话、白名单与任务会被删除，` +
        `然后切换到其他项目（没有其他项目就进入快聊）。</p>`
      : `<p class="dim small">该项目下的<b>会话记录与白名单会一并删除</b>；电脑上的文件夹和文件` +
        `<b>不受影响</b>，以后随时可以重新添加回来。</p>`) +
    `<span class="mono-path">${escapeHtml(p.root_path)}</span>`;
  showModal("删除项目", box, async () => {
    const r = await request("project.delete", { id: p.id });
    await applyWorkspaceData(await fetchWorkspaceData());
    if (r.switched_to) {
      addNotice(`已删除项目「${p.name}」，已切换到「${r.switched_to.name}」。`);
    } else {
      addNotice(`已删除项目「${p.name}」；文件夹仍保留在电脑上，当前处于快聊。`);
    }
  }, "移除");
}
