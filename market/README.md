# 技能广场索引

设置 · 技能与工具 → 技能广场 的在线索引就是这个 JSON。客户端行为：

1. 默认拉取 `https://raw.githubusercontent.com/Sky-scrape/SkySheep/main/market/index.json`
   （即**本仓库**的 `market/index.json`，与技能本体同仓维护，推上 GitHub 即生效）；
2. 拉不到（离线）时回退到内置清单（`engine/src/skysheep/skills/market.py` 的 `BUILTIN_INDEX`）；
3. 环境变量 `SKYSHEEP_MARKET_URL` 可把索引指向任意自建地址（GitHub raw / Gitee raw / 自己的服务器均可）。

## 发布

索引随主仓库一起发布：把 SkySheep 仓库推上 GitHub 后自动生效，无需额外操作。
之后新增技能条目只需编辑 `index.json` 再推送（客户端每次打开技能广场都会重新拉取，
服务端有 60 秒缓存）。

## 条目格式

```json
{
  "items": [
    {
      "name": "技能名（≤60 字符）",
      "description": "一句话说明（≤200 字符）",
      "url": "https://github.com/<user>/<repo>/tree/main/<技能子目录>",
      "author": "作者名"
    }
  ]
}
```

`url` 支持仓库子目录或整仓库；安装器会下载并抽取其中的 SKILL.md。
Gitee 地址同样支持（国内可达性更好时可把条目换成 Gitee 链接）。

## 官方种子技能

仓库根目录的 `skills-gallery/` 是官方维护的中文场景种子技能包（周报、会议纪要、
Excel 清洗、旅行规划等 10 个），每个技能一个文件夹、内含一个 `SKILL.md`。

它与本索引的关系：

1. `skills-gallery/` 是技能本体，随 SkySheep 主仓库一起维护、走 PR 审核；
2. 本目录的 `index.json` 是广场货架，收录其中每个技能的条目（`url` 指向
   `Sky-scrape/SkySheep` 仓库内 `skills-gallery/<name>` 子目录，`author` 为 `skysheep`）；
3. 用户在技能广场点「安装」时，安装器按 `url` 下载子目录并抽取 SKILL.md 装入
   `~/.skysheep/skills/`，因此新增或更新技能只需维护 `skills-gallery/` 并同步编辑本索引。

给 `skills-gallery/` 新增技能后，记得在 `index.json` 的 `items` 末尾追加对应条目再发布。
