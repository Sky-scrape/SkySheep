// 高危命令影响面 · Claude Code 插件格式示例
//
// 与老 quickjs 示例（只能收紧、只能 note/ui）的关键差异：
//   - tool.check 可以直接返回 allow / ask / deny
//   - 返回的 reason 会显示在权限卡上
//   - 可以改写 input（返回 input 字段）——本示例刻意不改写，只做判定

const HIGH_RISK_PATTERNS = [
  { re: "\\brd\\s+/s", label: "递归删除目录（rd /s）" },
  { re: "\\brmdir\\s+/s", label: "递归删除目录（rmdir /s）" },
  { re: "\\bdel\\s+/[fsq]", label: "强制/静默删除文件（del /f /s /q）" },
  { re: "\\bformat\\s+[a-z]:", label: "格式化磁盘（format）" },
  { re: "\\btaskkill\\s+/f", label: "强制结束进程（taskkill /f）" },
  { re: "Remove-Item[^\\n]*-Recurse", label: "递归删除（PowerShell Remove-Item -Recurse）" },
  { re: "\\brm\\s+(-[a-zA-Z]*[rf][a-zA-Z]*\\s+)+", label: "递归/强制删除（rm -r/-f）" },
  { re: "\\bmkfs", label: "格式化文件系统（mkfs）" },
  { re: "\\bdd\\s+[^\\n]*\\bof=", label: "磁盘直写（dd of=）" },
  { re: ":\\(\\)\\s*\\{", label: "fork 炸弹特征" },
  { re: "\\b(shutdown|reboot|halt)\\b", label: "关机 / 重启" },
  { re: "\\bgit\\s+push\\s+[^\\n]*--force", label: "强推远端分支（git push --force）" },
];

function hitsOf(command) {
  const text = String(command || "");
  if (!text) return [];
  const hits = [];
  for (const pat of HIGH_RISK_PATTERNS) {
    try {
      if (new RegExp(pat.re, "i").test(text)) hits.push(pat.label);
    } catch (e) { /* 词表条目坏了就跳过 */ }
  }
  return hits;
}

// 前置检查：高危命令一律 ask（强制逐次确认，即使本会免确认放行）
register("tool.check", ($, e, next) => {
  if (e.payload.tool !== "run_command") return next();
  const hits = hitsOf(e.payload.input && e.payload.input.command);
  if (!hits.length) return next();
  return {
    decision: "ask",
    reason: "检测到高危命令动作：" + hits.slice(0, 3).join("、") + "——请确认命令与作用范围后再放行",
  };
});

// 放行后补一条留痕
register("tool.pre", ($, e, next) => {
  if (e.payload.tool !== "run_command") return next();
  const hits = hitsOf(e.payload.input && e.payload.input.command);
  if (!hits.length) return next();
  return {
    note: "已放行的高危命令（" + hits.slice(0, 3).join("、") + "）：" +
      String((e.payload.input && e.payload.input.command) || "").slice(0, 120),
  };
});
