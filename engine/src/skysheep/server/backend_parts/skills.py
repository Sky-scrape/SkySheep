"""技能管理：开关/范围/正文预览、程序内导入与删除、本机扫描、草稿保存。

D6 基础设施与迁移域（backend.py Mixin 拆分批 2 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

覆盖四个相邻职责区：清单操作（开关 / 使用范围 / SKILL.md 正文预览）、
程序内导入与删除（装前预览 / 安装 / 删除，global 与 project 两级落点）、
本机现存技能扫描（Claude Code 等来源，纯只读探测）、会话存为技能草稿
（/save-skill）。

跨域只读（不收敛，注记）：`self.skills` / `self.trust`（trust 属性随
TrustMixin 本批落位）、`self._refresh_system_prompts()`（内核系统提示词
刷新）、`self._build_full_registry()`（内核工具注册表装配）、
`self._get_owned_session` / `self.store`（D1 会话面）——均经共享 self
按 MRO 取用，本文件不持久化任何实例状态。
"""

from __future__ import annotations

import asyncio
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

from ...config import skysheep_home
from ...skills.draft import build_skill_draft, save_skill_draft
from ...skills.installer import (
    LOCAL_SKILL_SOURCES,
    SkillInstallError,
    _safe_members,
    _scan_skills_in_dir,
    _unpack_zip,
    _validate_skill_name,
    install_from_url,
    remove_skill,
    scan_computer_skills,
)
from ...skills.installer import install as install_skill
from ...skills.loader import _load_skill_from_dir
from ...tools import Safety
from ..audit import record_audit


class SkillsMixin:
    """技能管理（skills.* WS 方法的实现面）。方法自 backend.py 逐字搬入。"""

    async def toggle_skill(self, name: str, enabled: bool) -> dict:
        if not self.skills.set_enabled(name, enabled):
            raise RuntimeError("skill not found: " + name)
        self.skills.discover()
        self._refresh_system_prompts()
        record_audit(
            skysheep_home(), kind="skill", action="enable" if enabled else "disable",
            name=name,
        )
        return {"name": name, "enabled": enabled}

    async def set_skill_scope(
        self, name: str, mode: str, projects: list[str] | None = None
    ) -> dict:
        """设置全局技能的使用范围（跨项目生效，改完立即重建系统提示词）。"""
        try:
            result = self.skills.set_scope(name, mode, projects)
        except KeyError as e:
            raise RuntimeError(str(e).strip("'\"")) from e
        self.skills.discover()  # 重新套用范围（set_scope 只改了内存里的当前实例）
        self._refresh_system_prompts()
        return {"name": name, **result}

    def skill_body(self, name: str) -> dict:
        """读 SKILL.md 原文供界面预览（不受启用/范围限制，停用的技能也要能看）。"""
        try:
            text = self.skills.raw_text(name)
        except KeyError as e:
            raise RuntimeError(str(e).strip("'\"")) from e
        except OSError as e:
            raise RuntimeError(f"读取技能文件失败：{e}") from e
        return {"name": name, "text": text}

    # ---- 技能：程序内导入 / 删除 ----

    def _skill_root(self, scope: str) -> Path:
        """技能安装位置：global = 所有项目共用，project = 只在当前项目生效。"""
        if scope == "project":
            if self.working_dir is None:
                raise RuntimeError(
                    "项目技能需要先打开一个项目——先在侧栏「项目」区点 ＋ 添加项目。"
                )
            return self.working_dir / ".skysheep" / "skills"
        if scope != "global":
            raise RuntimeError("scope 只能是 global 或 project: " + str(scope))
        return skysheep_home() / "skills"

    async def preview_skill_install(self, source: str, scope: str = "global",
                                    overwrite: bool = False) -> dict:
        """装前预览：解析来源但不落盘，返回将要安装的技能清单（名称/描述/文件数）。

        网址来源会真的下载（不做预览就无法给出有意义的清单；下载本身已被
        域名白名单 + DNS 校验限制住），本地来源零写入。技能正文不返回——
        预览只给「装什么」的判断依据，内容审查靠装后的 skill.body 预览。
        """
        import tempfile

        source = str(source or "").strip()
        if not source:
            raise RuntimeError("缺少来源：本地路径或网址")
        existing = {s.name for s in self.skills.all()}
        is_url = source.lower().startswith(("http://", "https://"))
        tmp_zip: Path | None = None
        staged_tmp: Path | None = None
        try:
            zip_to_stage: Path | None = None
            if is_url:
                from ...skills.installer import download_to
                try:
                    tmp_zip = await asyncio.to_thread(download_to, source)
                except SkillInstallError as e:
                    raise RuntimeError(str(e)) from e
                zip_to_stage = tmp_zip
            else:
                staged = Path(source).expanduser()
                if not staged.exists():
                    raise RuntimeError("路径不存在：" + source)
                if staged.is_file() and staged.suffix.lower() == ".zip":
                    # 本地 .zip 技能包：与网址来源同一套「先解压暂存再扫描」——
                    # zip 文件本身扫不出 SKILL.md，直接当目录扫必然报「没找到技能」，
                    # 界面的两段式导入就会卡死在预览这一步
                    zip_to_stage = staged
            if zip_to_stage is not None:
                staged_tmp = Path(tempfile.mkdtemp(prefix="skysheep-skill-preview-"))
                with zipfile.ZipFile(zip_to_stage) as zf:
                    # 与 install_from_zip 同一套安全解压（挡绝对路径 / 穿越 / 符号链接）
                    _unpack_zip(zf, _safe_members(zf), staged_tmp)
                staged = staged_tmp
            roots = _scan_skills_in_dir(staged)
            if not roots:
                raise RuntimeError(
                    f"在「{staged.name}」里没找到技能：技能包必须是含 SKILL.md 的文件夹"
                )
            items = []
            invalid: list[dict] = []
            for r in roots[:20]:  # 上限防烂包撑爆返回值
                sk = _load_skill_from_dir(r, "global")
                if sk is None:
                    continue
                try:
                    _validate_skill_name(sk.name)
                except SkillInstallError as e:
                    # 预览与安装同一套名字校验：安装层必拒的名字不进「将要安装」清单
                    invalid.append({"name": sk.name, "reason": str(e)})
                    continue
                file_count = sum(1 for f in r.rglob("*") if f.is_file())
                items.append({
                    "name": sk.name,
                    "description": sk.description[:160],
                    "version": sk.version,
                    "files": file_count,
                })
            if not items and not invalid:
                raise RuntimeError("技能包里没有可识别的 SKILL.md")
            clashes = [it["name"] for it in items if it["name"] in existing]
            return {
                "items": items,
                "count": len(items),
                "scope": scope,
                "overwrite": overwrite,
                "clashes": clashes,
                "needs_overwrite": bool(clashes and not overwrite),
                "invalid": invalid,
            }
        finally:
            if tmp_zip is not None:
                try:
                    tmp_zip.unlink(missing_ok=True)
                except OSError:
                    pass
            if staged_tmp is not None:
                shutil.rmtree(staged_tmp, ignore_errors=True)

    async def install_skill(self, source: str, scope: str = "global", overwrite: bool = False) -> dict:
        """把技能装进技能目录，装完立即生效（无需重启）。

        source 可以是本机文件夹、.zip 路径，或 GitHub / Gitee 的仓库链接与
        .zip 直链（网址下载放到工作线程，不卡事件循环）。
        overwrite=True 供「更新/重装」用：同名技能整目录替换；
        网址安装会同时记录来源 url，供技能清单展示安装来源。
        """
        root = self._skill_root(scope)
        existing = {s.name for s in self.skills.all()}
        try:
            if str(source).strip().lower().startswith(("http://", "https://")):
                result = await asyncio.to_thread(
                    install_from_url, source, root, existing=existing, overwrite=overwrite
                )
            else:
                result = await asyncio.to_thread(
                    install_skill, source, root, existing=existing, overwrite=overwrite
                )
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        # 重新发现 + 重建系统提示词：新技能马上出现在清单里
        self.skills.discover()
        self._refresh_system_prompts()
        # 用户在界面上主动装技能：已信任的项目同步指纹，避免刚装完就回到「待确认」。
        # 指明本次动的来源（项目技能目录）：别的来源若也被改过（git pull 塞进来的
        # 项目级 mcp.json 等），不跟着一起洗白（安全审查低危项）
        if scope == "project":
            self.trust.refresh(touched=self._project_skills_dir())
        installed_names = result.get("installed") or []
        if installed_names:
            record_audit(
                skysheep_home(), kind="skill",
                action="update" if overwrite else "install", scope=scope,
                name="、".join(str(n) for n in installed_names),
                detail=(
                    f"网址安装（{str(source)[:120]}）"
                    if str(source).strip().lower().startswith(("http://", "https://"))
                    else "本机导入"
                ),
            )
        result["scope"] = scope
        result["skills"] = [
            {
                "name": s.name, "description": s.description,
                "source": s.source, "enabled": s.enabled,
                "scope": s.scope, "scope_projects": list(s.scope_projects),
                "version": s.version,
                "origin": dict(s.origin),
                "applies": self.skills.applies(s.name),
            }
            for s in self.skills.all()
        ]
        return result

    async def scan_local_skills(self) -> dict:
        """探测本机常见的别家技能目录，找出可以复用的技能候选。

        纯只读探测（Claude Code / agents / Codex 的 home 目录 + 本项目 .claude）：
        找到的候选交给设置页技能页就地列出（「本机现存」面板）勾选，真正的安装
        走既有 skills.install 复制流程，这里不移动、不删除、不修改探测到的任何文件。
        """
        roots = [(label, Path(p).expanduser()) for label, p in LOCAL_SKILL_SOURCES]
        if self.working_dir is not None:
            roots.append(("本项目", self.working_dir / ".claude" / "skills"))
        existing = {s.name for s in self.skills.all()}
        candidates = await asyncio.to_thread(scan_computer_skills, roots, existing)
        return {"candidates": candidates}

    async def delete_skill(self, name: str) -> dict:
        """删除技能目录。全局技能与项目技能都可能重名，按当前加载到的那一份删。

        同名技能两边各存一份时，发现逻辑让全局版遮蔽项目版（见 SkillLoader.discover）
        ——用户看到、点删除的是全局版，所以全局根必须排在候选首位；否则会静默删掉
        被遮蔽的项目版，清单里的技能纹丝不动。项目版被加载时反过来，项目根优先。
        """
        skill = self.skills.get(name)
        if skill is None:
            raise RuntimeError("skill not found: " + name)
        scope = "project" if skill.source == "project" else "global"
        roots = [skysheep_home() / "skills"]
        if self.working_dir is not None:
            roots.append(self.working_dir / ".skysheep" / "skills")
        if scope == "project":
            roots.reverse()
        try:
            result = remove_skill(name, roots)
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        # 顺手抹掉该技能的遗留状态（停用名单 / 使用范围）：不清理的话，
        # 同名技能重新安装后会莫名“装上了却是停用”，用户很难自己定位。
        self.skills.forget(name)
        self.skills.discover()
        self._refresh_system_prompts()
        if scope == "project":
            # 删掉项目技能也是用户自己的改动；来源限定在项目技能目录
            self.trust.refresh(touched=self._project_skills_dir())
        record_audit(skysheep_home(), kind="skill", action="remove", scope=scope, name=name)
        result["scope"] = scope
        return result

    # ---- 技能：会话存为草稿（/save-skill） ----

    def _skill_safety_lookup(self) -> Callable[[str], str]:
        """工具名 → safety 分级的查询函数（草稿生成的分类依据）。

        查不到的名字（MCP 服务器未连接 / 注册表构建失败）一律按写入/执行类
        保守处理：宁可把一条只读调用列进注意事项，也不把写操作漏成「无风险」。
        """
        try:
            registry = self._build_full_registry()
        except Exception:  # noqa: BLE001  启动早期 cfg 未就绪等；退化为全保守
            registry = None

        def safety_of(tool_name: str) -> str:
            tool = registry.get(tool_name) if registry is not None else None
            return tool.safety.value if tool is not None else Safety.WRITE.value

        return safety_of

    async def save_skill_from_session(self, session_id: str = "") -> dict:
        """把一个会话的做法整理成技能草稿（skills.save_from_session）。

        不调模型：生成逻辑在 skills/draft.py，从 SessionStore 的持久化消息
        机械提取（目标 = 首条用户消息、步骤 = 实际发生过的工具调用按轮次排列，
        只读与写入/执行分开）。返回的 name / description / 正文都会先过前端
        编辑框，用户改完确认才经 skills.save_draft 落盘。
        """
        sid = session_id or (self.session.id if self.session else "")
        if not sid:
            raise RuntimeError("当前没有活动会话，先聊出一轮任务再保存技能草稿")
        sess = await self._get_owned_session(sid)
        msgs = await self.store.load_messages(sid)
        draft = build_skill_draft(
            msgs,
            safety_of=self._skill_safety_lookup(),
            project_root=self.working_dir,
        )
        draft["session_title"] = sess.title or ""
        return draft

    async def save_skill_draft(self, name: str, content: str, scope: str = "global") -> dict:
        """保存技能草稿（skills.save_draft）：与技能安装同一套落点防线。

        名字形状（_validate_skill_name）与落点包含性（_skill_target）都过一遍，
        文件经 textio 原子写进技能目录；重名不覆盖。写完立即重新发现并重建
        系统提示词，新技能马上出现在清单里（与 install_skill 的收尾一致）；
        项目级保存同样同步工作区信任指纹，避免刚存完就回到「待确认」。
        """
        root = self._skill_root(scope)
        existing = {s.name for s in self.skills.all()}
        try:
            result = await asyncio.to_thread(
                save_skill_draft, root, name, content, existing=existing
            )
        except SkillInstallError as e:
            raise RuntimeError(str(e)) from e
        self.skills.discover()
        self._refresh_system_prompts()
        if scope == "project":
            self.trust.refresh(touched=self._project_skills_dir())
        result["scope"] = scope
        return result

    def _project_skills_dir(self) -> Path | None:
        """当前项目的技能目录（trust.refresh 指明来源用；无项目时为 None）。

        与 _project_skills_dir_if_trusted 的区别：这个不要求项目已信任——
        refresh 的场景恰恰是「用户刚动过它、要判断能否延续信任」。
        """
        if self.working_dir is None:
            return None
        return self.working_dir / ".skysheep" / "skills"
