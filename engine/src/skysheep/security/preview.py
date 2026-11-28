"""PermissionGate 的确认预览助手（diff 预览与移动/删除影响树清单）。"""

from __future__ import annotations

import difflib
from pathlib import Path

from ..tools.base import Tool


class PermissionPreviewMixin:
    """为 PermissionGate 提供写文件 diff 预览与 move/delete 影响范围枚举。"""

    DIFF_PREVIEW_MAX_LINES = 160
    TREE_PREVIEW_MAX = 120
    # 目录预览扫描的条目上限：node_modules 级目录动辄十万+，全量 rglob + 排序 +
    # 逐文件 stat 在极端目录上是 O(n log n) 次系统调用。确认弹窗只需要「大概
    # 多大、动哪些东西」，超过上限就按已扫到的部分给结论并注明「可能更多」。
    TREE_PREVIEW_SCAN_MAX = 20_000

    def _preview_diff(self, tool: Tool, input_dict: dict) -> str:
        if not self.working_dir:
            return ""
        if tool.name in ("move_file", "delete_file"):
            return self._preview_fs_impact(tool, input_dict)
        if tool.name not in ("write_file", "edit_file", "write_document"):
            return ""
        raw = str(input_dict.get("path", "") or "")
        if not raw:
            return ""
        target = Path(raw)
        if not target.is_absolute():
            target = self.working_dir / target
        try:
            target.resolve().relative_to(self.working_dir)
        except (OSError, ValueError):
            return ""  # 工作目录之外：不给预览（工具层还会拦）
        try:
            old = self._read_text_for_preview(target)
        except OSError:
            return ""
        if tool.name == "write_file":
            new = str(input_dict.get("content", "") or "")
        elif tool.name == "write_document":
            # 确认弹窗展示源内容（Markdown/JSON）预览；.csv 是文本可做真实 diff，
            # 覆盖已有 docx/xlsx 时旧内容是二进制，给不出有意义的文本 diff
            if target.exists() and target.suffix.lower() != ".csv":
                return ""
            new = str(input_dict.get("content", "") or "")
        else:  # edit_file：按工具语义模拟替换
            old_s = input_dict.get("old_string")
            new_s = input_dict.get("new_string")
            if not isinstance(old_s, str) or not isinstance(new_s, str) or old_s not in old:
                return ""
            count = -1 if input_dict.get("replace_all") else 1
            new = old.replace(old_s, new_s, count)
        if old == new:
            return ""
        lines = list(
            difflib.unified_diff(
                old.splitlines(), new.splitlines(),
                fromfile="改前", tofile="改后", lineterm="",
            )
        )
        if len(lines) > self.DIFF_PREVIEW_MAX_LINES:
            rest = len(lines) - self.DIFF_PREVIEW_MAX_LINES
            lines = lines[: self.DIFF_PREVIEW_MAX_LINES] + [f"…（余 {rest} 行省略）"]
        return "\n".join(lines)

    @staticmethod
    def _read_text_for_preview(target: Path) -> str:
        """读旧内容做 diff 预览：按探测到的编码读（GBK 文件预览不再是乱码）。

        探不出编码 / 是二进制时退回 utf-8 忽略错误，旧内容仅用于展示。
        """
        from ..textio import decode_bytes

        if not target.is_file():
            return ""
        loaded = decode_bytes(target.read_bytes()[:400_000])
        if loaded.binary or not loaded.certain:
            return target.read_text(encoding="utf-8", errors="replace")
        return loaded.text

    def _preview_fs_impact(self, tool: Tool, input_dict: dict) -> str:
        """move_file / delete_file 的确认预览：列出将被处置的实际路径。

        删除与移动没有「改前→改后文本 diff」可言，但用户需要看到的恰恰是
        「到底动哪些东西」。目录递归时把内容完整列出来（超长截断），
        避免确认弹窗上只有一个模糊的目录名。

        读取边界（既有确认设计）：预览只读取**工作目录内**的目录树/文件内容
        ——目标落在工作目录之外（含 ``../`` 相对逃逸）时不逐条枚举，但覆盖
        删除的披露不能静默省略：至少给出真实落点路径 + 类型/大小的元数据
        （authorize 期的 _delete_shape 本就对目录外落点做 is_dir 级 stat），
        并注明「预览不列出其内容」。
        """

        def resolve(raw: str) -> Path | None:
            if not raw:
                return None
            p = Path(raw)
            if not p.is_absolute():
                p = self.working_dir / p
            try:
                p = p.resolve()
                p.relative_to(self.working_dir)
            except (OSError, ValueError):
                return None
            return p

        def outside_landing_line(target: Path) -> str:
            """工作目录之外的覆盖落点：明确告知覆盖删除，披露真实路径与
            类型/大小（仅元数据），不枚举内容。"""
            try:
                kind = "目录" if target.is_dir() else "文件"
                size = f"，{target.stat().st_size:,} B" if kind == "文件" else ""
            except OSError:
                kind, size = "文件", ""
            note = "，预览不列出其内容" if kind == "目录" else ""
            return f"将覆盖删除：{target}（{kind}{size}，位于工作目录之外{note}）"

        if tool.name == "move_file":
            src = resolve(str(input_dict.get("source", "") or ""))
            if src is None:
                return ""
            dst_raw = str(input_dict.get("destination", "") or "")
            dst = resolve(dst_raw)
            if dst is not None:
                # 工作目录内：与执行侧（tools/fs.py）同一套落点语义——目标是
                # 已存在的目录就移进去保留原名，预览披露的删除面按真实落点算。
                if dst.is_dir():
                    dst = dst / src.name
            else:
                # 工作目录之外（含 ../ 相对逃逸）、解析失败或空目标：执行侧的
                # resolve_path（默认不限制工作目录）照样会解析出真实落点并按
                # overwrite 覆盖删除——预览不能因 resolve() 的目录内限定就漏
                # 披露（M02 复核缺口）。相对 raw 先拼 working_dir 再归一化，
                # 目录内移语义与执行侧保持一致。
                raw_dst = Path(dst_raw)
                if not raw_dst.is_absolute():
                    raw_dst = self.working_dir / raw_dst
                try:
                    raw_dst = raw_dst.resolve()
                except OSError:
                    pass
                dst = raw_dst / src.name if raw_dst.is_dir() else raw_dst
            lines = [f"移动：{src} → {dst}"]
            if src.exists():
                lines += self._list_tree_lines(src, label="将移动")
            # 审查 M02：overwrite=true 且真实落点已存在时，这次移动会先删掉
            # 落点上的既有内容（目录是 shutil.rmtree 整棵子树）——预览必须把
            # 「被覆盖删除的东西」连同递归删除警示一起摆到确认卡上，不能只
            # 展示将移入的文件。overwrite=false 时工具层会因「目标已存在」
            # 报错、什么都不会删，因此不做任何删除预告（不误报）。
            # 落点仍在工作目录内才逐文件列树；之外按 outside_landing_line
            # 明确披露（真实路径 + 类型/大小），不静默省略也不越界枚举。
            if input_dict.get("overwrite") and dst.exists():
                try:
                    dst.relative_to(self.working_dir)
                    inside = True
                except ValueError:
                    inside = False
                if dst.is_dir() and src.is_dir():
                    lines += (self._list_tree_lines(dst, label="将覆盖删除")
                              if inside else [outside_landing_line(dst)])
                    lines.append(
                        "⚠️ 覆盖会先递归删除上面列出的整个目录（含全部内容）再移入；"
                        "要保留请先换个目标名或把旧目录改名。"
                    )
                elif not dst.is_dir():
                    lines += (self._list_tree_lines(dst, label="将覆盖删除")
                              if inside else [outside_landing_line(dst)])
                    lines.append("⚠️ 覆盖将删除并替换上面这个已有文件（旧内容进检查点，可回滚）。")
                # src 是文件而落点是目录：工具层会拒绝执行（无法覆盖），不预告删除
            return "\n".join(lines)

        target = resolve(str(input_dict.get("path", "") or ""))
        if target is None:
            return ""
        lines = [f"删除：{target}"]
        if target.exists():
            lines += self._list_tree_lines(target, label="将删除")
        return "\n".join(lines)

    def _list_tree_lines(self, target: Path, label: str) -> list[str]:
        """列出文件 / 目录下的条目（确认弹窗用），超长截断。"""
        if target.is_file():
            try:
                size = target.stat().st_size
            except OSError:
                return [f"{label}：{target.name}"]
            return [f"{label}：{target.name}（{size:,} B）"]
        try:
            # 手动带计数地迭代，命上限即停——sorted 会先物化整棵树
            entries = []
            truncated_scan = False
            for i, e in enumerate(target.rglob("*")):
                if i >= self.TREE_PREVIEW_SCAN_MAX:
                    truncated_scan = True
                    break
                entries.append(e)
            entries.sort()
        except OSError:
            return []
        files = [e for e in entries if e.is_file()]
        total = 0
        for f in files:
            try:
                total += f.stat().st_size
            except OSError:
                pass
        lines = [f"{label}：整个目录，{len(files)} 个文件，共 {total:,} B"]
        for e in files[: self.TREE_PREVIEW_MAX]:
            try:
                lines.append("  " + str(e.relative_to(target)))
            except ValueError:
                lines.append("  " + str(e))
        if len(files) > self.TREE_PREVIEW_MAX:
            lines.append(f"  …（其余 {len(files) - self.TREE_PREVIEW_MAX} 个文件省略）")
        if truncated_scan:
            # 扫描本身就到了上限：不能把「已扫到的」当成「全部的」展示——
            # 确认卡上必须让用户知道实际规模可能更大（删目录是不可逆操作）
            lines.append(
                f"  …（目录过大，仅扫描前 {self.TREE_PREVIEW_SCAN_MAX:,} 个条目，"
                "实际文件数可能更多）"
            )
        return lines
