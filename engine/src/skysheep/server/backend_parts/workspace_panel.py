"""工作区文件面板与提示词库：fs 读写预览、@ 文件清单、快捷指令（提示词）库。

D5 前端服务面板域（backend.py Mixin 拆分批 1 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

两个相邻职责区：
- 工作区文件面板的服务面：fs.read / fs.write / workspace.files 的实现虽在
  files 域分发，但实现是「前端文件面板的服务面」，与 textio 强绑定（编码 /
  行尾符安全读写），归 D5；
- 提示词库：内置示例 BUILTIN_SNIPPETS 与播种 / 找回 / 润色 / 导入导出。

字段所有权（D5）：`_text_meta_cache` —— 文本文件编码 / 行尾符探测缓存，
实例属性、由主类 __init__ 一次铺齐（本 Mixin 不带 __init__），本文件读写。

模块级 `BUILTIN_SNIPPETS` 随本文件落位；backend.py 顶部保留
`from .backend_parts.workspace_panel import BUILTIN_SNIPPETS`（re-export），
供 _dispatch_files.py 与既有测试继续从 backend 模块名引用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

from ...config import skysheep_home
from ...obs import warning as obs_warning
from ...security import leases
from ...textio import encode_text, read_text_file, write_bytes_atomic
from ._shared import collect_stream_text

# 内置示例快捷指令：首次启动时由 _seed_builtin_snippets 落成真实记录（设置页可见、
# 可编辑、可删除），之后与用户自建指令无异。删除是持久的——种过一次就不再补
# （ui.json 的 snippets_seeded 标记）；用户把指令删光后，/ 菜单仍用这批内容兜底
# （snippets.list 随响应下发，前端不再另存一份）。
BUILTIN_SNIPPETS = [
    {
        "name": "总结当前项目",
        "content": (
            "请浏览当前项目的结构和关键文件，用中文总结："
            "这个项目是做什么的、怎么运行、代码组织如何。"
        ),
    },
    {
        "name": "总结 PDF 文档",
        "content": (
            "请读取 @文件.pdf，提炼要点成一页速览："
            "核心结论（不超过 5 条）、关键数据、值得注意的风险或限制。"
        ),
    },
    {
        "name": "Excel 转图表报告",
        "content": (
            "请读取 @表格.xlsx，检查数据质量（缺失/重复/格式问题），"
            "清洗后生成一份汇总报告，并挑合适的维度画出趋势/分布图（保存为图片），"
            "告诉我有什么发现。"
        ),
    },
    {
        "name": "调研一个话题",
        "content": (
            "请联网调研「在此填写话题」，把最新进展整理成带信息来源的简报"
            "（重点、时间线、不同观点）。今天是 {{date}}，请优先采用近三个月内的信息来源。"
        ),
    },
    {
        "name": "帮我修报错",
        "content": (
            "我遇到了这个报错：\n\n{{clipboard}}\n\n"
            "请分析原因并给出修复方案；如果是代码问题，直接读文件帮我改好。"
        ),
    },
    {
        "name": "解释这段代码",
        "content": (
            "请解释下面这段代码：先一段话总览它做什么，再按关键步骤说明逻辑，"
            "最后指出潜在问题或可改进点。\n\n{{clipboard}}"
        ),
    },
    {
        "name": "写一份周报",
        "content": (
            "请把下面的工作内容整理成简洁的中文周报："
            "本周完成 / 进行中 / 风险与问题 / 下周计划。\n\n{{clipboard}}"
        ),
    },
]


class WorkspacePanelMixin:
    """工作区文件面板（fs.read / fs.write / workspace.files）与提示词库。

    方法自 backend.py 按职责注释逐字搬入（拆分批 1，D5 前端服务面板域）。
    """

    def _workspace_path(self, raw: str) -> Path:
        """把相对/绝对路径解析到工作目录内的真实路径；越界一律拒绝。

        fs_read / fs_write 共用：resolve 消掉 .. 后必须仍在工作目录里，
        Windows 非法字符（盘符冒号等）在 resolve/relative_to 时抛错同样拦下。
        无项目态（没有工作目录）直接给可读拒绝。
        """
        if self.working_dir is None:
            raise RuntimeError(
                "当前没有项目，文件面板不可用——先在侧栏「项目」区点 ＋ 添加项目并选择一个文件夹。"
            )
        target = Path(raw)
        if not target.is_absolute():
            target = self.working_dir / target
        try:
            target = target.resolve()
            target.relative_to(self.working_dir)
        except (OSError, ValueError):
            raise RuntimeError("只能访问工作目录内的文件") from None
        return target

    # Windows 文件名非法字符与控制字符（建新文件时逐段校验）
    _BAD_NAME_CHARS = '<>:"|?*'
    # Windows 保留设备名（含带扩展名形式，如 con.txt：旧系统上这类文件无法正常打开/删除）
    _RESERVED_NAMES = {"con", "prn", "aux", "nul",
                       *(f"com{i}" for i in range(1, 10)),
                       *(f"lpt{i}" for i in range(1, 10))}

    def _validate_rel_path(self, raw: str) -> str:
        """新建文件的相对路径校验：禁绝对路径、.. 段、非法字符、结尾点/空格。"""
        raw = raw.replace("\\", "/").strip("/")
        parts = [p for p in raw.split("/") if p]
        if not parts:
            raise RuntimeError("文件名不能为空")
        for seg in parts:
            if seg in (".", ".."):
                raise RuntimeError("路径里不能有 .. 段")
            if any(c in seg for c in self._BAD_NAME_CHARS) or any(ord(c) < 32 for c in seg):
                raise RuntimeError("文件名含非法字符：" + seg)
            if seg.endswith((" ", ".")):
                raise RuntimeError("Windows 文件名不能以空格或点结尾：" + seg)
            if seg.split(".", 1)[0].lower() in self._RESERVED_NAMES:
                raise RuntimeError("文件名是 Windows 保留设备名：" + seg)
        rel = "/".join(parts)
        if len(rel) > 240:
            raise RuntimeError("路径过长（上限 240 字符）")
        return rel

    async def fs_read(self, params: dict) -> dict:
        """只读预览工作区文件（文件树用）：文本直接读（自动识别编码）；
        PDF/Word/Excel/PPT 提取文本；其余二进制拒绝。沙箱限制在工作目录内。
        返回 mtime 供编辑器保存时做冲突检测，editable 标记「这份文本能不能写回」
        （提取文本 / 截断 / 编码不明的都不可写回）。"""
        raw = str(params.get("path", "") or "").strip()
        if not raw:
            raise RuntimeError("missing path")
        target = self._workspace_path(raw)
        if not target.is_file():
            raise RuntimeError("文件不存在（或这是目录）: " + raw)
        try:
            size = target.stat().st_size
            # 毫秒精度：ns 级时间戳(约 1.7e18)超出 JS Number 安全整数(9e15)，
            # 经 JSON 往返会丢精度导致冲突检测永远误报
            mtime = target.stat().st_mtime_ns // 1_000_000
        except OSError as e:
            raise RuntimeError("读取失败: " + str(e)) from None

        # 文档类文件：提取文本预览（与 read_document 工具同一套解析器）
        if target.suffix.lower() in (".pdf", ".docx", ".xlsx", ".pptx"):
            if size > 50_000_000:
                raise RuntimeError(f"文件太大（{size / 1048576:.0f}MB），上限 50MB")
            from ...tools.docs import extract_document_text

            try:
                text = await asyncio.to_thread(extract_document_text, target, 60_000)
            except Exception as e:  # noqa: BLE001 - ToolError/解析错误统一转可读提示
                raise RuntimeError(str(e)) from None
            return {
                "path": str(target.relative_to(self.working_dir)).replace("\\", "/"),
                "text": text,
                "size": size,
                "truncated": False,
                "doc": True,
                "mtime": mtime,
                "editable": False,
            }

        try:
            # 只读预览需要的 400KB 前缀：先整个 read_bytes 再切片会把几百 MB 的
            # 日志/数据集全量吞进内存，同步读盘还会卡住事件循环（UI 全部停摆）
            with target.open("rb") as fh:
                data = fh.read(400_000)
        except OSError as e:
            raise RuntimeError("读取失败: " + str(e)) from None
        if b"\x00" in data:
            raise RuntimeError("二进制文件不支持预览")
        # 文本文件：探测编码，GBK / GB18030 文件不再显示成替换字符
        from ...textio import decode_bytes

        loaded = decode_bytes(data)
        truncated = size > 400_000
        return {
            "path": str(target.relative_to(self.working_dir)).replace("\\", "/"),
            "text": loaded.text,
            "size": size,
            "truncated": truncated,
            "mtime": mtime,
            # 编码探不出来时不允许写回：用替换字符覆盖原文等于损坏文件
            "editable": not truncated and loaded.certain,
            "encoding": loaded.encoding,
            "encoding_text": "UTF-8" if loaded.encoding == "utf-8" else loaded.encoding.upper(),
            "newline": loaded.newline,
        }

    async def fs_write(self, params: dict) -> dict:
        """右侧文件面板编辑器的「保存」：把用户改的文本写回工作区文件。

        这是用户本人在界面上点保存（与 project.save_instructions 同级的
        「用户主动写」，不走 PermissionGate——那道门管的是 Agent 工具调用），
        但同样严格锁死在工作目录内、只收文本、限 2MB。base_mtime 与磁盘当前
        不一致时不落盘、返回 conflict=True，由前端让用户选覆盖（force=True）
        或放弃，避免无意盖掉 Agent / 其他程序正在做的改动。

        落盘沿用文件原本的编码与行尾符：缓存里只存了探测结果的小字典（不读全文，
        避免每次保存多读一遍盘）；拿不到时回退 UTF-8 + LF。
        """
        raw = str(params.get("path", "") or "").strip().replace("\\", "/")
        if not raw:
            raise RuntimeError("missing path")
        text = str(params.get("text", "") or "")
        force = bool(params.get("force"))
        base_mtime = params.get("base_mtime")
        if base_mtime is not None:
            try:
                base_mtime = int(base_mtime)
            except (TypeError, ValueError):
                raise RuntimeError("base_mtime 必须是整数（fs.read 返回的 mtime）") from None
        rel = self._validate_rel_path(raw)
        target = self._workspace_path(rel)
        # 沿用文件原本的编码与行尾符：探测结果有小缓存，拿不到时回退 UTF-8 + LF。
        # 前端编辑器按 HTML 规范会把内容统一成 LF，所以写回时必须还原行尾符，
        # 否则保存一次 CRLF 文件就变成 LF（或反之），整文件 diff 全是噪声。
        enc, nl = self._file_text_meta(target)
        try:
            data = encode_text(text, enc, nl)
        except (UnicodeEncodeError, LookupError):
            raise RuntimeError(
                f"内容无法用原编码（{enc}）保存：里面出现了该编码不支持的字符。"
                "请改用 UTF-8 另存，或删掉这些字符后重试。"
            ) from None
        if len(data) > 2_000_000:
            raise RuntimeError("文件太大（超过 2MB），请用系统编辑器处理")

        exists = target.is_file()
        if exists and base_mtime is not None and not force:
            try:
                cur = target.stat().st_mtime_ns // 1_000_000  # 毫秒精度，见 fs_read
            except OSError as e:
                raise RuntimeError("读取失败: " + str(e)) from None
            if int(base_mtime) != cur:
                # 不落盘，交给前端弹「覆盖 / 放弃」
                return {"saved": False, "conflict": True, "mtime": cur}
        # 写租约：用户保存也要和并行 Agent 写同一文件错开（短等待，面板是交互场景）；
        # 冲突注记随结果带回（前端当前未展示，agent 侧写完会带同样的注记给模型）
        hub = leases.hub_for(self.working_dir)
        lease = None
        if hub is not None:
            try:
                lease = await hub.claim(
                    [(target, rel)],
                    owner=(self.session.id if self.session else ""),
                    wait_s=3.0,
                )
            except Exception as e:  # noqa: BLE001 - 租约是协调不是闸门，故障不挡保存
                # fail-open 但 fail-visible：与 agent 侧（core/agent.py 的
                # lease_claim_failed）同款留痕——面板保存失去与并行 Agent 写
                # 的冲突协调，本次覆盖不再带注记，保存本身不受影响。
                obs_warning(
                    "lease_claim_failed",
                    "面板保存的写租约领取失败：本次保存失去并行冲突协调（照常保存）",
                    tool="fs.write",
                    target=rel,
                    session=(self.session.id if self.session else None),
                    error=f"{type(e).__name__}: {e}",
                )
                lease = None
        try:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                # 原子写：先落同目录临时文件再 replace，写一半被杀/替换失败
                # 都不会把旧内容毁成半个文件（面板保存与 Agent 写同一文件并存）
                write_bytes_atomic(target, data)
                mtime = target.stat().st_mtime_ns // 1_000_000
            except OSError as e:
                raise RuntimeError("写入失败: " + str(e)) from None
        finally:
            note = lease.release() if lease is not None else ""
        self._text_meta_cache[str(target)] = (mtime, enc, nl)
        return {"saved": True, "mtime": mtime, "size": len(data),
                "encoding_text": enc.upper(), **({"lease_note": note} if note else {})}

    # 文本文件编码 / 行尾符探测结果缓存：fs.read 写入、fs.write 读回。
    # 存的是探测结果（mtime + 两个短字符串），不存文件内容，避免每次保存多读一遍盘。
    # 实例属性而不是类属性（安全审查低危项）：类属性会被所有实例共享——测试里
    # 两个后端实例互相看到对方的探测结果，多工作区/多实例场景下也会串味。

    def _file_text_meta(self, target: Path) -> tuple[str, str]:
        """取文件原本的（编码, 行尾符）；缓存过期或文件已变时重探。

        探不出确凿编码时回退 UTF-8 + LF（与 fs_read 返回 editable=False 一致：
        那种文件前端不会让用户编辑，走不到这里）。
        """
        from ...textio import decode_bytes

        try:
            mtime = target.stat().st_mtime_ns // 1_000_000
        except OSError:
            return "utf-8", "\n"
        key = str(target)
        cached = self._text_meta_cache.get(key)
        if cached and cached[0] == mtime:
            return cached[1], cached[2]
        try:
            with target.open("rb") as fh:  # 同 fs_read：只探前缀，不整读大文件
                loaded = decode_bytes(fh.read(400_000))
        except OSError:
            return "utf-8", "\n"
        if loaded.binary or not loaded.certain:
            return "utf-8", "\n"
        self._text_meta_cache[key] = (mtime, loaded.encoding, loaded.newline)
        return loaded.encoding, loaded.newline

    async def workspace_files(self) -> dict:
        """/@ 文件提及的数据源：项目内文件相对路径清单（跳过依赖与构建目录）。"""
        if self.working_dir is None:
            return {"files": [], "dirs": [], "no_project": True}
        return await asyncio.to_thread(self._walk_workspace_files)

    SKIP_DIRS = {
        ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
        ".ruff_cache", ".mypy_cache", "dist", "build", ".skysheep", ".mimosa",
        ".next", "target", ".idea", ".vscode", ".preview", ".zcode", ".agents",
    }
    MAX_FILES = 400

    def _walk_workspace_files(self) -> dict:
        from ...core.ignore import IgnoreRules

        out: list[str] = []
        dirs: list[str] = []
        root = self.working_dir
        ignore = IgnoreRules.load(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in self.SKIP_DIRS and not d.startswith(".git")
                and not ignore.matches((Path(dirpath) / d).relative_to(root).as_posix(), is_dir=True)
            ]
            rel_base = Path(dirpath).relative_to(root)
            if rel_base != Path("."):
                # 目录也进索引（@补全里以 / 结尾）；只收录直接子层，避免清单爆炸
                dirs.append(rel_base.as_posix() + "/")
            for fn in filenames:
                rel = (rel_base / fn).as_posix()
                if ignore.matches(rel):
                    continue
                if len(out) >= self.MAX_FILES:
                    return {"files": sorted(out), "root": str(root), "truncated": True}
                out.append(rel)
        # 目录排在文件前（@菜单分组展示），各自排序
        return {"files": sorted(dirs)[: self.MAX_FILES // 4] + sorted(out),
                "root": str(root), "truncated": False}

    async def _seed_builtin_snippets(self) -> None:
        """首启把内置示例快捷指令落成真实记录：设置页里可见、可编辑、可删除。

        只做一次：哨兵文件放 SkySheep home（与指令库同生命周期，不进 ui.json、
        不随设置导出走）；已有自定义指令的老用户不种——保持「添加了自己的指令后
        示例不再出现」的旧约定；种过之后删光也不复活，删光是用户的明确决定。
        """
        mark = skysheep_home() / "snippets-seeded"
        if mark.exists():
            return
        if not await self.store.list_snippets():
            for s in BUILTIN_SNIPPETS:
                await self.store.add_snippet(s["name"], s["content"])
            logging.getLogger("skysheep").info(
                "首次启动：已把 %d 条内置示例提示词写入指令库（设置 · 提示词 里可编辑/删除）",
                len(BUILTIN_SNIPPETS),
            )
        try:
            mark.write_text("1", encoding="utf-8")
        except OSError:
            pass  # 写不进哨兵（只读盘等）：下次启动重查一遍，无副作用

    async def restore_builtin_snippets(self) -> int:
        """把内置示例提示词加回指令库（按名称去重，已存在的跳过），返回新增条数。

        与首启播种不同：这是用户的显式动作，删过的示例可以被主动找回；
        哨兵文件不动，重启仍不会自动补种。
        """
        existing = {s["name"] for s in await self.store.list_snippets()}
        added = 0
        for s in BUILTIN_SNIPPETS:
            if s["name"] in existing:
                continue
            await self.store.add_snippet(s["name"], s["content"])
            added += 1
        return added

    @staticmethod
    def snippet_initials(name: str) -> str:
        """提示词名称的拼音首字母串（~ 菜单首字母过滤用）。

        pypinyin 缺席或意外失败时退空串：过滤退化为按名称/内容子串，不影响其他功能。
        """
        try:
            from pypinyin import Style, lazy_pinyin

            return "".join(lazy_pinyin(name or "", style=Style.FIRST_LETTER)).lower()
        except Exception:  # noqa: BLE001 - 首字母是锦上添花，不值得为它报错
            return ""

    async def snippets_polish(self, params: dict) -> dict:
        """让当前模型把一条提示词改写得更清晰、更结构化（一次性调用，不落库）。

        与 _map_generate_run 同款边界：没有可用模型直接报错；结果只回给前端
        展示——用户看过、改过、点保存才生效。{{...}} 占位符要求模型原样保留。
        """
        text = str(params.get("content", "") or "").strip()
        if not text:
            raise RuntimeError("没有可润色的内容")
        if not self.provider:
            raise RuntimeError("没有可用模型——先在 设置 · 模型服务 里配好再试")
        prompt = (
            "请把下面这条给 AI 助手的提示词改写得更清晰、更结构化：保留原意与全部细节，"
            "补齐缺失的背景交代，可按「背景 / 任务 / 要求」等小块组织；"
            "{{...}} 形式的占位符必须原样保留、一个都不能删改。"
            "只输出改写后的提示词正文，不要任何解释或代码块围栏。\n\n---\n\n" + text
        )
        try:
            # 整段限时：模型挂住时不能让前端「✨ AI 润色」永远停在「润色中…」。
            # 超时转成可读错误走普通失败路径（TimeoutError 是 asyncio.timeout 的取消信号，
            # 不在这里拦截会让 async for 的清理也跟着丢）。
            async with asyncio.timeout(60):
                out = (await collect_stream_text(self.provider, prompt)).strip()
        except TimeoutError:
            raise RuntimeError("润色超时：模型 60 秒没有返回内容，请稍后重试") from None
        # 宽容剥壳：模型偶尔无视「不要围栏」的叮嘱
        if out.startswith("```"):
            out = out.split("\n", 1)[-1]
        if out.endswith("```"):
            out = out.rsplit("```", 1)[0]
        out = out.strip()
        if not out:
            raise RuntimeError("模型没有返回内容")
        return {"content": out}

    async def export_snippets(self) -> dict:
        """导出全部提示词为可分享的 JSON（只带名称与内容，不含排序/统计）。"""
        rows = await self.store.list_snippets()
        return {
            "format": "skysheep-snippets",
            "version": 1,
            # slash 只在非空时带上：不含命令的旧导出保持原 JSON 形状，
            # 外部脚本按 {name, content} 读取不会突然多出一个空字段
            "snippets": [
                {"name": s["name"], "content": s["content"],
                 **({"slash": s["slash"]} if s.get("slash") else {})}
                for s in rows
            ],
        }

    async def import_snippets(self, params: dict) -> dict:
        """合并导入提示词：params.data 为导出 JSON（对象或字符串），或 params.path 为文件路径。

        去重规则：名称与内容都相同的条目跳过；其余追加到列表最前（与新建一致）。
        条数上限 200、文件上限 1MB——提示词库是手写资产，超过这个量级不是正常用法。
        """
        raw = params.get("data")
        path = str(params.get("path") or "").strip()
        if raw is None and path:
            p = Path(path)
            if not p.is_file():
                raise RuntimeError("文件不存在：" + path)
            if p.stat().st_size > 1_000_000:
                raise RuntimeError("文件过大，不像是提示词导出文件")
            try:
                tf = read_text_file(p)
            except OSError as e:
                raise RuntimeError(f"读取失败：{e}") from None
            if tf.binary:
                raise RuntimeError("这是一个二进制文件，不是提示词导出 JSON")
            try:
                raw = json.loads(tf.text)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"不是有效的 JSON：{e}") from None
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as e:
                raise RuntimeError(f"不是有效的 JSON：{e}") from None
        if raw is None:
            raise RuntimeError("缺少导入内容")
        items = raw.get("snippets") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            raise RuntimeError("不是 SkySheep 提示词导出文件（缺少 snippets 列表）")
        existing = {(s["name"], s["content"]) for s in await self.store.list_snippets()}
        added = skipped = 0
        for it in items[:200]:
            if not isinstance(it, dict):
                skipped += 1
                continue
            name = str(it.get("name") or "").strip()[:40]
            content = str(it.get("content") or "").strip()[:8000]
            slash = str(it.get("slash") or "").strip().lstrip("/").lower()[:32]
            if not name or not content or (name, content) in existing:
                skipped += 1
                continue
            # 导入包里的别名可能非法/冲突：静默降级为「不注册命令」，
            # 不让一条脏数据阻断整份提示词导入
            if slash and not re.fullmatch(r"[a-z0-9_-]{1,32}", slash):
                slash = ""
            if slash and any(s.get("slash") == slash for s in await self.store.list_snippets()):
                slash = ""
            await self.store.add_snippet(name, content, slash=slash)
            existing.add((name, content))
            added += 1
        return {"added": added, "skipped": skipped}
