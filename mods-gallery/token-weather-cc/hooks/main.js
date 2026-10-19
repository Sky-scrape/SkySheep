// 上下文天气 · Claude Code 插件格式示例
//
// 用 register(event, handler) 声明式挂事件；handler 形如 ($, e, next)：
//   $    Mod API（$.state / $.fs / $.clock / $.store / ...）
//   e    事件对象（e.payload 即该事件的载荷）
//   next 调用下一个 handler；不调用即中断链
//
// 与老 quickjs 示例（export default + sky.state）的关键差异：
//   - 事件名用 Claude 风格 tool.post / turn.stop（点号）
//   - 状态用 $.state.get/set（同步 KV，引擎代存）
//   - UI 用元素树（verdict.ui），不再只有 5 种固定 widget

const THROTTLE_MS = 2000;

function weather(pct) {
  if (pct >= 85) return { icon: "⛈", label: "风暴", level: "danger", hint: "该 /compact 了" };
  if (pct >= 60) return { icon: "🌫", label: "多云", level: "warn", hint: "" };
  return { icon: "☁️", label: "晴", level: "ok", hint: "" };
}

function widgetFor(payload) {
  const limit = Number(payload.contextLimitTokens) || 0;
  const used = Number(payload.contextTokens) || 0;
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const w = weather(pct);
  const text = w.icon + " " + w.label + " " + pct + "%" + (w.hint ? " · " + w.hint : "");
  return {
    slot: "pane",
    ui: {
      type: "box", direction: "row", children: [
        { type: "text", text: text, tone: w.level === "ok" ? "ok" : w.level },
      ],
    },
  };
}

register("tool.post", ($, e, next) => {
  const now = $.clock.now();
  const last = Number($.state.get("lastTs") || 0);
  if (now - last < THROTTLE_MS) return next();
  $.state.set("lastTs", now);
  return widgetFor(e.payload);
});

register("turn.stop", ($, e, next) => {
  const w = widgetFor(e.payload);
  $.state.set("lastPct", Number(e.payload.contextTokens) || 0);
  return w;
});
