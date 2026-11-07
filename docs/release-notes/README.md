# Release 正文

每个版本发版前写一份 `docs/release-notes/<版本>.md`，发布脚本会把它作为 Release 正文上传。

- 功能版复制 `TEMPLATE-feature.md`，修复版复制 `TEMPLATE-fix.md`。
- 三处硬规矩：**必须有 1 张图**；**下载与校验信息放在正文上半**；**工程细节进折叠块**。
- 长句不要手动折行——GitHub Release 会把单个换行渲染成真实换行。
- 写完跑一遍：`uv run python tools/check_release_notes.py ../docs/release-notes/<版本>.md --version <版本>`。

`release.py --upload` 找不到对应正文时会拒绝上传；确需临时发布可加 `--allow-placeholder-notes`。
