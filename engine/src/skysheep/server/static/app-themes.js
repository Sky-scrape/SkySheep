// app-themes.js —— 主题配色表（26 套：id → { name, dark }），从 app.js 拆出的数据表。
// 普通 script、零构建；必须先于 app.js 加载（before_app）：app.js 的主题系函数
// （applyThemeMode / renderThemeSwatches / xtermTheme / 命令面板主题动作…）事件期读本表。
// 本表是主题名单的**唯一事实来源**，改表要同步七处（清单见 app.css 主题段开头的
// 「新增主题同步清单」）；test_theme_alignment.py 以本文件为真源机械校验全部登记处。

// 浅色：纸墨 paper / 青瓷 celadon / 秋柿 kaki / 青提 mint / 豆沙 bean / 芥末 mustard
//       嫩芽 sprout / 银笺 silver / 刃银 blade / 樱粉 sakura / 草兰 meadow /
//       菀雾 heather / 醇巧 caramel；
// 深色：夜墨 night / 黛夜 indigo / 松烟 pine / 深海 abyss / 赤青 scarlet / 雾紫 lilac
//       青柠 lime / 胭脂 rouge / 曜金 obsidian / 墨朱 inkred / 沉海 glowsea /
//       浓巧 cocoa / 丹金 cinnabar。
// 每套主题是 app.css 文件尾部的一个完整 [data-theme=…] 变量组，切换 = 换 <html> 的 data-theme；
// auto 时浅色落纸墨、深色落夜墨。light/dark 是旧版两档值，读取时映射回 paper/night。
// backend_parts/remote.py 的 THEME_PREFS / THEME_AUTO_LIGHT / THEME_AUTO_DARK 由
// tools/gen_theme_prefs.py 从本表生成（勿手改，重跑工具即可）；index.html 的主题卡片
// 与首帧兜底仍需手工同步（test_theme_alignment.py 机械校验）。
const THEMES = {
  paper: { name: "纸墨", dark: false },
  celadon: { name: "青瓷", dark: false },
  kaki: { name: "秋柿", dark: false },
  mint: { name: "青提", dark: false },
  bean: { name: "豆沙", dark: false },
  mustard: { name: "芥末", dark: false },
  sprout: { name: "嫩芽", dark: false },
  silver: { name: "银笺", dark: false },
  blade: { name: "刃银", dark: false },
  sakura: { name: "樱粉", dark: false },
  meadow: { name: "草兰", dark: false },
  heather: { name: "菀雾", dark: false },
  caramel: { name: "醇巧", dark: false },
  night: { name: "夜墨", dark: true },
  indigo: { name: "黛夜", dark: true },
  pine: { name: "松烟", dark: true },
  abyss: { name: "深海", dark: true },
  scarlet: { name: "赤青", dark: true },
  lilac: { name: "雾紫", dark: true },
  lime: { name: "青柠", dark: true },
  rouge: { name: "胭脂", dark: true },
  obsidian: { name: "曜金", dark: true },
  inkred: { name: "墨朱", dark: true },
  glowsea: { name: "沉海", dark: true },
  cocoa: { name: "浓巧", dark: true },
  cinnabar: { name: "丹金", dark: true },
};
