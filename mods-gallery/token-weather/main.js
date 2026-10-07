// 上下文天气 —— SkySheep 官方示例 Mod（对位 Claude Code Mods 的 Token Weather）。
//
// 把上下文占用做成天气式的常驻状态条（输入区右侧）：
//   ☁️ 晴（<60%）→ 🌫 多云（60–84%，warn）→ ⛈ 风暴（≥85%，storm：该 /compact 了）
//
// 数据来自引擎每次工具调用/回合结束附带的真实 token 统计（contextTokens）与
// 当前模型的上下文上限（contextLimitTokens）；精确值不是目标，趋势才是。
// 状态按会话键存在 sky.state（引擎代存），跨会话互不串。

// 节流：工具调用可能一波十几个，状态条不用每次都刷（≥2 秒才产一次）
var THROTTLE_MS = 2000;

function weather(pct) {
  if (pct >= 85) return { icon: "⛈", label: "风暴", level: "storm", hint: "该 /compact 了" };
  if (pct >= 60) return { icon: "🌫", label: "多云", level: "warn", hint: "" };
  return { icon: "☁️", label: "晴", level: "ok", hint: "" };
}

function widgetFor(p) {
  var limit = Number(p.contextLimitTokens) || 0;
  var used = Number(p.contextTokens) || 0;
  var pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  var w = weather(pct);
  var text = w.icon + " " + w.label + " " + pct + "%" + (w.hint ? " · " + w.hint : "");
  return { kind: "stat", slot: "tray", text: text, level: w.level, value: pct };
}

export default {
  // 每次工具调用后刷新（节流）
  toolPost(p) {
    var now = sky.now();
    var last = Number(sky.state.lastTs) || 0;
    if (now - last < THROTTLE_MS) return null; // 静默：离上一次刷新太近
    sky.state.lastTs = now;
    var w = widgetFor(p);
    sky.state.lastPct = w.value || 0;
    return { ui: [w] };
  },

  // 回合结束必刷一次：天气条定格在本轮结束时的占用
  turnStop(p) {
    var w = widgetFor(p);
    sky.state.lastPct = w.value || 0;
    return { ui: [w] };
  },
};
