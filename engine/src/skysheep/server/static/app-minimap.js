/** 消息导航条（minimap）分区文件：从 app.js 拆出（O5 第二批拆块）。
 *  普通 script、零构建：只定义函数与常量，不顶层执行；
 *  主栏那一条的实例现在 app-chat.js 里创建（`const mainMinimap = ...`），
 *  所以本文件必须排在 app-chat.js 之前加载（index.html 里已如此登记）。
 *  依赖的全局：chatBox / uiScale（分别由 app-chat.js / app.js 提供，调用时已就绪）。
 */

// ---------- 消息导航条（minimap）：右侧一列小圆点，一颗对应一条消息 ----------
// 平时半透明圆点，当前视口所在消息的那颗拉长成实色亮条，相邻圆点按距离
// 渐变（近长远短，4 步外回到圆点）；点它平滑跳到对应消息，
// 悬停提示消息开头文字。消息太多匀不下时等距抽样（一颗代表一段，跳到段首）：
// 先按消息数封顶（超过 MINI_MAX_MSGS 条先对消息抽样，rebuild 不再逐条全量测量），
// 再按可用高度封顶（短视口下圆点更少）。
// 主栏与每个分屏列各挂一条（createChatMinimap 一实例一流）：分屏列是独立
// 对话面，不能共用主栏那条——共用时列里滚动的高亮/点击目标全指向主栏。
// 只在内容可滚动且不止一条消息时出现；窄屏（≤860px）整个隐藏。
const MINI_ITEM_H = 10, MINI_GAP = 4; // 与 app.css 的圆点热区高 / 列间距保持一致
const MINI_MAX_MSGS = 150; // 消息数上限：超过就先对消息等距抽样，圆点从样本里选代表

// 消息在聊天流内容坐标系（= scrollTop / scrollTo 所在的坐标系）里的纵位。
// 两个坑：
//  1) getBoundingClientRect 返回物理像素，而 #app 上有 zoom（--ui-zoom），
//     不除回 uiScale 会与 scrollTop 差一个缩放倍率（0.9 缩放时偏小 10%）；
//     长对话里偏差累积，点圆点会落到错误位置、高亮也跟着错位。
//  2) 入场动画 rise 带 translateY transform，会被 rect 算进去，
//     所以必须在动画结束后校准（见 rebuild 里的 remap）。
function miniTopOf(log, el, logRect) {
  const r = logRect || log.getBoundingClientRect();
  return (el.getBoundingClientRect().top - r.top) / uiScale + log.scrollTop;
}

function miniMsgsFor(log) {
  if (!log) return [];
  const out = [];
  for (const el of log.children) {
    // 只收真正的消息卡片：user / assistant / error。
    // 不能用 classList.contains("msg") 粗筛——.msg.notice（系统提示胶囊，如
    // 「已切换到规划模式」）与 .msg.plan-actions（计划执行按钮条）也带 msg 前缀，
    // 会被当成消息，多出圆点、与对话对不上（空态引导那类没有 msg 的节点同理）
    if (!el.classList.contains("msg")) continue;
    if (el.classList.contains("notice") || el.classList.contains("plan-actions")) continue;
    // 过程时间线里的中间说明（模型动手前说的话）：属于「操作过程」而非对话消息，
    // 不算右侧导航刻度（正常路径会把它收进过程框；没有过程框时留在流里也不该占一格）
    if (el.classList.contains("proc-note")) continue;
    // 工具截图回填的伪 user 消息（“[screenshot] 上述工具…”）与系统补记不是
    // 用户说的「对话」：算进刻度会出现「刻度条数明显多于对话」（接力一轮里
    // 截图工具多的时候尤其明显，2026-11-14 用户实例）
    if (el.classList.contains("screenshot-note")
        || el.classList.contains("system-note")) continue;
    out.push(el);
  }
  return out;
}

// 一个圆点必须对应一个「真能滚到」的位置：目标位置钳到可滚动范围内。
// 不钳制的话，末尾几条消息的目标会超出 maxScroll，点了只能滚到底、
// 高亮一律落到最后一颗（「点不动第 N 个圆点」的成因）。
function miniScrollTarget(log, el, logRect) {
  const maxScroll = Math.max(0, log.scrollHeight - log.clientHeight);
  return Math.min(maxScroll, Math.max(0, miniTopOf(log, el, logRect) - 10));
}

/** 一条消息导航条实例：主栏一条，每个分屏列各一条（各自绑自己的聊天流）。 */
function createChatMinimap(host, opts = {}) {
  const root = document.createElement("div");
  root.className = "chat-minimap hidden";
  if (opts.id) root.id = opts.id;
  host.appendChild(root);
  let log = null, mut = null, ro = null, targets = [], tops = [];
  let timer = 0, raf = 0, remap = 0, gen = 0;

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(rebuild, 160); // 流式输出逐字长高，防抖后统一重排
  }

  function bind(next) {
    if (log === next) { schedule(); return; }
    if (mut) { mut.disconnect(); mut = null; }
    if (ro) { ro.disconnect(); ro = null; }
    // 旧流不再可见前先解绑它的 scroll 监听：来回切标签每次都重新 bind，
    // 不解绑的话同一个 log 会越积越多的 scroll 回调（内存慢性泄漏）
    if (log) log.removeEventListener("scroll", onScroll);
    clearTimeout(remap);
    gen++; // 换流后旧流的校准定时器作废
    log = next;
    root.textContent = "";
    root.classList.add("hidden");
    if (!log) return;
    mut = new MutationObserver(schedule);
    // 只观察 childList：流式渲染走 innerHTML 整树替换（childList 命中），
    // 高度变化还有 ResizeObserver 兜底；characterData 会在流式期间被
    // 逐字变更轰炸，防抖形同虚设
    mut.observe(log, { childList: true, subtree: true });
    ro = new ResizeObserver(schedule);
    ro.observe(log);
    log.addEventListener("scroll", onScroll, { passive: true });
    schedule();
  }

  function onScroll() {
    if (raf) return;
    raf = requestAnimationFrame(() => { raf = 0; updateActive(); });
  }

  function rebuild() {
    if (!log || !log.isConnected) { root.classList.add("hidden"); return; }
    const msgs = miniMsgsFor(log);
    const logH = log.clientHeight;
    const scrollable = msgs.length > 1 && log.scrollHeight > logH + 4;
    root.classList.toggle("hidden", !scrollable);
    if (!scrollable) { root.textContent = ""; return; }
    // 容器 rect 只读一次：循环里每条消息各读一次 getBoundingClientRect
    // 会强制布局多次（长会话 rebuild 期间每秒多次全量布局的来源之一）
    const logRect = log.getBoundingClientRect();
    // 消息数超上限先对消息等距抽样：下面这段逐条测量每条消息各读一次
    // getBoundingClientRect，几千条消息的会话流式期间每轮 rebuild 全量量一遍太重。
    // 抽样后每颗圆点仍对准样本里的真实消息（高亮与点击都按它算），末尾留一个
    // 名额给最后一条消息——点最末一颗、贴底强制高亮都对准真正的结尾；
    // 不超上限时 pool 就是原数组，行为与逐条测量完全一致。
    let pool = msgs;
    if (msgs.length > MINI_MAX_MSGS) {
      pool = new Array(MINI_MAX_MSGS);
      const s0 = msgs.length / MINI_MAX_MSGS;
      for (let i = 0; i < MINI_MAX_MSGS; i++) {
        pool[i] = i === MINI_MAX_MSGS - 1 ? msgs[msgs.length - 1] : msgs[Math.floor(i * s0)];
      }
    }
    // 目标位置相同的连续消息合并成一颗（保留最后一条——滚到底时看到的正是末尾那几条）。
    // 内容只比视口高一点点时，末尾好几条的目标会被一起钳到底部；
    // 不合并就会出现多颗点了没反应、高亮全落最后一颗的死圆点。
    const slots = [];
    for (const el of pool) {
      const target = miniScrollTarget(log, el, logRect);
      const last = slots[slots.length - 1];
      if (last && Math.abs(last.target - target) < 1) last.el = el;
      else slots.push({ el, target });
    }
    const availH = Math.max(120, Math.min(logH - 130, 560));
    const maxDots = Math.max(1, Math.floor((availH + MINI_GAP) / (MINI_ITEM_H + MINI_GAP)));
    const n = Math.min(slots.length, maxDots);
    const step = slots.length / n;
    targets = []; tops = [];
    // 就地复用已有圆点，只在数量变化时增删——不能像以前那样每次 textContent=""
    // 重建整列：新插入的元素首帧就带着目标宽度，浏览器没有「起始值」可插值，
    // transition 被直接跳过，于是滚动时宽度是一档一档跳变而不是渐变（实测复现）。
    // 复用同一批元素后，宽度从旧值平滑变到新值，--mini-w 的 transition 才真正生效。
    const bars = root.children;
    for (let i = 0; i < n; i++) {
      const el = slots[Math.floor(i * step)].el;
      let d = bars[i];
      if (!d) {
        d = document.createElement("div");
        d.className = "chat-minimap-dot";
        root.appendChild(d);
      }
      // title 只在消息定稿（有 seq）后算一次并缓存；流式中的消息内容未定，
      // 每次重算（几千字的消息一次 rebuild 就是几千字符的扫描）
      let txt;
      if (el.dataset.seq) {
        txt = el._miniTitle;
        if (txt === undefined) {
          txt = (el.textContent || "").trim().replace(/\s+/g, " ");
          txt = txt.length > 80 ? txt.slice(0, 80) + "…" : txt;
          el._miniTitle = txt;
        }
      } else {
        txt = (el.textContent || "").trim().replace(/\s+/g, " ");
        txt = txt.length > 80 ? txt.slice(0, 80) + "…" : txt;
      }
      d.title = txt;
      // onclick 每轮重绑：闭包捕获的是本条消息的滚动目标，复用元素时必须跟着换
      d.onclick = () => log.scrollTo({ top: miniScrollTarget(log, el), behavior: smoothBehavior() });
      targets.push(el);
      tops.push(miniTopOf(log, el, logRect));
    }
    // 多余的圆点从尾部移除（消息变少 / 容器变矮时）
    while (root.children.length > n) root.removeChild(root.lastChild);
    updateActive();
    // 入场动画（320ms）结束的位置才作数：等它播完再校准一遍缓存的纵位与高亮
    const g = ++gen;
    clearTimeout(remap);
    remap = setTimeout(() => {
      if (g !== gen || !log || !log.isConnected) return;
      // 必须包一层箭头函数：.map(miniTopOf) 会把数组索引当第二个参数传进去，
      // 而 miniTopOf 的第二参是 logRect（矩形对象）——拿到索引后 r.top 是
      // undefined，整列纵位全变 NaN，高亮就永远卡在第一颗。
      tops = targets.map((el) => miniTopOf(log, el));
      updateActive();
    }, 480);
  }

  function updateActive() {
    if (!log || root.classList.contains("hidden")) return;
    const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 4;
    const mark = log.scrollTop + 24; // 视口顶缘略下取当前消息：点哪颗亮哪颗，滚动时跟随最上可见消息
    let on = 0;
    for (let i = 0; i < tops.length; i++) if (tops[i] <= mark) on = i;
    if (atBottom) on = tops.length - 1; // 贴底时末尾短消息到不了顶缘，强制亮最后一根
    // 从亮条向上下渐变：紧邻的略长、越远越短，4 步外回到圆点；
    // 宽度写在 --mini-w 上（CSS 的 width: var(--mini-w) 接住），transition 负责平滑
    const bars = root.children;
    for (let i = 0; i < bars.length; i++) {
      const dist = Math.abs(i - on);
      bars[i].classList.toggle("on", i === on);
      bars[i].style.setProperty("--mini-w", (dist ? 10 + 22 * Math.max(0, 1 - dist / 4) : 32).toFixed(1) + "px");
    }
  }

  function destroy() {
    if (mut) { mut.disconnect(); mut = null; }
    if (ro) { ro.disconnect(); ro = null; }
    if (log) { log.removeEventListener("scroll", onScroll); log = null; }
    clearTimeout(timer);
    clearTimeout(remap);
    if (raf) { cancelAnimationFrame(raf); raf = 0; }
    root.remove();
  }

  if (opts.log) bind(opts.log);
  return { bind, destroy, refresh: schedule, root };
}
