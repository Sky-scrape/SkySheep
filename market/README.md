# 技能广场索引

> **1.9 起客户端设置页不再有「技能广场」入口**（应用内一键安装界面下线；索引文件、
> 后端 `skills.market` 方法与版本标注能力按现状保留）。安装技能请走
> 设置 · 技能与工具 → 右上角「＋ 导入技能」（文件夹 / .zip / GitHub·Gitee 链接）
> 或「本机现存」。本目录继续作为索引发布件维护，供自建客户端或后续恢复入口使用。

这份 JSON 即技能广场的在线索引。客户端（`skills.market`）行为：

1. 默认拉取 `https://raw.githubusercontent.com/Sky-scrape/SkySheep/main/market/index.json`
   （即**本仓库**的 `market/index.json`，与技能本体同仓维护，推上 GitHub 即生效）；
2. 拉不到（离线）时回退到随包发布的完整索引（打包时把本文件同目录的 `index.json`
   作为数据文件带进安装包；开发态直接读仓库里的 `market/index.json`），
   再兜底才是 `engine/src/skysheep/skills/market.py` 的 `BUILTIN_INDEX` 精简清单；
3. 环境变量 `SKYSHEEP_MARKET_URL` 可把索引指向任意自建地址（GitHub raw / Gitee raw /
   自己的服务器均可）。

## 发布

索引随主仓库一起发布：把 SkySheep 仓库推上 GitHub 后自动生效，无需额外操作。
之后新增技能条目只需编辑 `index.json` 再推送（客户端拉取时有 60 秒缓存）。

## 条目格式

```json
{
  "items": [
    {
      "name": "技能名（≤60 字符）",
      "description": "一句话说明（≤200 字符）",
      "url": "https://github.com/<user>/<repo>/tree/main/<技能子目录>",
      "author": "作者名",
      "category": "分类名（可选，≤20 字符；界面按它出筛选标签）",
      "version": "1.0.0（可选，≤32 字符；与技能 SKILL.md 的 version 比对）",
      "updated_at": "2026-09-22（可选，≤32 字符；界面上展示最近更新时间）"
    }
  ]
}
```

`url` 支持仓库子目录或整仓库；安装器会下载并抽取其中的 SKILL.md。
Gitee 地址同样支持（国内可达性更好时可把条目换成 Gitee 链接）。

名称 / 描述 / 作者 / 地址四个字段都会进入客户端的搜索匹配（另按分类筛选、
名称命中排前），所以 `name` 与 `description` 写成用户会用来搜的词（做什么、
用什么格式、什么平台）比写成内部代号更有用。

## 版本与更新

- 技能本体的版本写在 `SKILL.md` frontmatter 的 `version:` 字段，索引条目的
  `version` 与它对应；改了 `skills-gallery/` 里的技能内容，要同时 bump 该技能的
  frontmatter `version` 和本索引对应条目的 `version` / `updated_at`。
- 客户端把两者比对：索引版本更新时，该条目的「安装」按钮会变成「更新」，
  点击走覆盖式重装（同名技能整目录替换，安装来源标记随之刷新）。
- 从网址安装的技能，安装器会在技能目录里写 `.source.json` 记录来源 url；
  广场据此判定「已安装 / 可更新」。没有来源标记的旧装技能按
  「条目 url 末段与技能名同名」兜底匹配。

## 官方种子技能

仓库根目录的 `skills-gallery/` 是官方维护的中文场景种子技能包（周报、会议纪要、
Excel 清洗、旅行规划等 20 个），每个技能一个文件夹、内含一个 `SKILL.md`。

它与本索引的关系：

1. `skills-gallery/` 是技能本体，随 SkySheep 主仓库一起维护、走 PR 审核；
2. 本目录的 `index.json` 是广场货架，收录其中每个技能的条目（`url` 指向
   `Sky-scrape/SkySheep` 仓库内 `skills-gallery/<name>` 子目录，`author` 为 `skysheep`）；
3. 用户在技能广场点「安装」时，安装器按 `url` 下载子目录并抽取 SKILL.md 装入
   `~/.skysheep/skills/`，因此新增或更新技能只需维护 `skills-gallery/` 并同步编辑本索引。
4. 索引条目按分类分组排列（自产场景技能在前、anthropics 官方文档技能在后）；
   客户端界面的分类筛选标签按索引里首次出现的顺序生成。

给 `skills-gallery/` 新增技能后，记得在 `index.json` 里追加对应条目
（带上 `category` / `version` / `updated_at`）再发布。
