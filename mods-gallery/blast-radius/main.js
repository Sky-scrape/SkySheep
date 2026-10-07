// 高危命令影响面 —— SkySheep 官方示例 Mod（对位 Claude Code Mods 的 Blast Radius）。
//
// 收紧路径（三层，全部「只收紧不放松」）：
//   1. 清单里的 declarative.require_confirm_tools = ["run_command"]：
//      即使白名单/自动允许写入档/完全访问档本会放行，run_command 一律回落逐次确认
//      （纯静态声明，由权限门在门内生效，不依赖 JS）；
//   2. permissionRequest：识别命令里的高危动作，在确认卡上给出影响面说明
//      （text widget + note，带 [Mod·blast-radius] 来源前缀）；
//   3. toolPre：对最终放行的高危调用补一条 Notice 留痕。
//
// 本示例刻意不 deny——收紧 = 强制确认 + 补信息；拒绝能力由用户自写清单声明。

// 精简高危词表：覆盖常见的高破坏性动作（Windows cmd / PowerShell / POSIX 各一批）
var HIGH_RISK_PATTERNS = [
  { re: "\\brd\\s+/s", label: "递归删除目录（rd /s）" },
  { re: "\\brmdir\\s+/s", label: "递归删除目录（rmdir /s）" },
  { re: "\\bdel\\s+/[fsq]", label: "强制/静默删除文件（del /f /s /q）" },
  { re: "\\bformat\\s+[a-z]:", label: "格式化磁盘（format）" },
  { re: "\\btaskkill\\s+/f", label: "强制结束进程（taskkill /f）" },
  { re: "Remove-Item[^\n]*-Recurse", label: "递归删除（PowerShell Remove-Item -Recurse）" },
  { re: "\\brm\\s+(-[a-zA-Z]*[rf][a-zA-Z]*\\s+)+", label: "递归/强制删除（rm -r/-f）" },
  { re: "\\bmkfs", label: "格式化文件系统（mkfs）" },
  { re: "\\bdd\\s+[^\\n]*\\bof=", label: "磁盘直写（dd of=）" },
  { re: ":\\(\\)\\s*\\{", label: "fork 炸弹特征" },
  { re: "\\b(shutdown|reboot|halt)\\b", label: "关机 / 重启" },
  { re: "\\bgit\\s+push\\s+[^\\n]*--force", label: "强推远端分支（git push --force）" },
];

function hitsOf(command) {
  var text = String(command || "");
  if (!text) return [];
  var hits = [];
  for (var i = 0; i < HIGH_RISK_PATTERNS.length; i++) {
    try {
      if (new RegExp(HIGH_RISK_PATTERNS[i].re, "i").test(text)) {
        hits.push(HIGH_RISK_PATTERNS[i].label);
      }
    } catch (e) { /* 词表条目坏了就跳过，不影响其它条目 */ }
  }
  return hits;
}

export default {
  // 权限确认卡：给「影响面」说明（≤500 字符的 text widget + 一句话 note）
  permissionRequest(p) {
    if (p.tool !== "run_command") return null;
    var command = p.input && p.input.command;
    var hits = hitsOf(command);
    if (!hits.length) return null;
    var lines = ["影响面：该命令包含以下高危动作——"];
    for (var i = 0; i < hits.length && i < 6; i++) lines.push("· " + hits[i]);
    lines.push("请确认命令与作用范围后再放行；拿不准就拒绝。");
    return {
      note: "检测到高危命令动作：" + hits.slice(0, 3).join("、"),
      ui: [{ kind: "text", slot: "perm", text: lines.join("\n") }],
    };
  },

  // 最终放行的高危调用补一条留痕（Notice）
  toolPre(p) {
    if (p.tool !== "run_command") return null;
    var hits = hitsOf(p.input && p.input.command);
    if (!hits.length) return null;
    return { note: "已放行的高危命令（" + hits.slice(0, 3).join("、") + "）：" +
      String((p.input && p.input.command) || "").slice(0, 120) };
  },
};
