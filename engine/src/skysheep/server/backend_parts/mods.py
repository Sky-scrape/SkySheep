"""Mods 扩展（实验性）的服务端接线：清单/安装（两段确认）/启停/删除/草稿/测试器。

ModManager 本体在 core/mods.py；这里只做服务层的目录落点、config 读写与热生效
（_reload_mods 推给所有活动 Agent，照 _reload_hooks 的模式）。全部写方法都登记为
本机专属（app.py 里 local_only=True）。
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from ...config import set_mods_in_config
from ...core.mods import (
    MANIFEST_NAMES,
    MAX_ENTRY_BYTES,
    ModError,
    ModManager,
    _mod_target,
    _parse_action_result,
    delete_mod_state,
    load_mod_state,
    mods_root,
    mods_template,
    parse_manifest,
    read_mod_readme,
)
from ...skills.installer import rmtree_force
from ...textio import read_text_file, write_text_atomic

# 单个 Mod 包的解包上限（skills 安装同量级：Mod 只有三个文件，上限是防误选整盘）
MAX_MOD_FILES = 200
MAX_MOD_UNPACKED_BYTES = 8 * 1024 * 1024


def _read_mods_cfg() -> tuple[bool, list[str]]:
    """读 [mods] 配置（load_config 的 SkySheepConfig 没有承载 mods 表，直接读原始数据）。"""
    from ...core.hooks import load_raw_config
    from ...core.mods import mods_config_from_raw

    return mods_config_from_raw(load_raw_config())


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ModsMixin:
    """Mods 扩展：安装/启停/删除/草稿/测试器与热生效。"""

    def _build_mod_manager(self) -> ModManager:
        enabled, ids = _read_mods_cfg()
        return ModManager(enabled=enabled, enabled_ids=ids)

    def sweep_mod_staging(self) -> None:
        """启动清扫：install 与 confirm 之间进程重启遗留的 .importing-* 暂存目录。

        待确认项只存内存（_pending_mod_install），重启即失——磁盘上的 staging
        无人认领，这里兜底清掉。只在 Backend.setup 启动期调一次：运行期
        （_reload_mods / 切项目）不能调，可能有未确认的安装预览正占着 staging。
        """
        root = mods_root()
        try:
            entries = list(root.iterdir())
        except OSError:
            return
        for path in entries:
            if path.name.startswith(".importing-") and path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    def _reload_mods(self, *, keep_recent: bool = False) -> None:
        """重建 ModManager 并推给基础 Agent 与所有会话 Agent（不重启即生效）。

        无已装 Mod 时 self.mods 置 None（Agent 侧零开销直通）；同时把
        extra_confirm 收紧查询接到当前权限门上（切项目会换门，_bind_project
        里也会接一次；这里兜住运行期改动）。keep_recent=True（启停/总开关路径）
        时把旧实例同名 Mod 的「最近执行」贴回新实例：那是挂在 LoadedMod 上的
        进程内展示状态，启停只切执行资格，不该被重建无声清空（安装/删除走
        默认 False——装的是新包、删的连 Mod 都不在了）。
        """
        old = self.mods
        manager = self._build_mod_manager()
        if keep_recent and old is not None:
            for mod_id, old_mod in old.mods.items():
                new_mod = manager.mods.get(mod_id)
                if new_mod is not None:
                    new_mod.recent = old_mod.recent
        # 旧实例的 per-Mod 沙箱执行线程随实例废弃：这里显式回收，否则每次
        # 启停/安装/删除/总开关/切项目都泄漏一条非 daemon executor 线程。
        # shutdown(wait=False, cancel_futures=True) 不打断在跑的 JS——空闲线程
        # 即刻退出，跑着的跑完自然退出。
        if old is not None:
            old.close()
        self.mods = manager if manager.mods else None
        if self.gate is not None:
            self.gate.extra_confirm = self.mods.extra_confirm if self.mods else None
        if hasattr(self, "_base_agent") and self._base_agent is not None:
            self._base_agent.mods = self.mods
        for ag in self._for_each_agent():
            if ag is not None:
                ag.mods = self.mods
        # 团队成员 Agent 不在 _for_each_agent（那是全局状态刷新的枚举面，成员
        # 系统词独立），但 Mod 拦截对队员同样生效：经各团队编排器补推一轮
        for orch in self._teams.values():
            for ag in orch.agents():
                ag.mods = self.mods

    def notify_mods_updated(self) -> None:
        """Mods 状态变化后广播事件（无在线连接时静默，照 notify_mcp_updated）。"""
        from ...bgtasks import spawn_bg

        for ws_emit in list(self.ws_emitters):
            try:
                spawn_bg(ws_emit({"kind": "mods_updated"}))
            except RuntimeError:
                continue  # 无事件循环（如纯测试环境）

    # ---- 清单 / 详情 / 模板 ----

    async def list_mods(self) -> dict:
        """清单（只读方法，远程连接也可调）：不携带本机绝对路径——mods_root 在
        用户主目录下，回传会给局域网令牌客户端暴露主目录结构，且前端并未使用。"""
        mods = self.mods if self.mods is not None else self._build_mod_manager()
        enabled, ids = _read_mods_cfg()
        items = [
            m.public_summary(active=enabled and m.id in ids)
            for m in mods.mods.values()
        ]
        return {
            "mods": items,
            "enabled": enabled,
            "runtime_available": mods.runtime_available,
            "runtime_version": mods.runtime_version,
        }

    async def get_mod(self, mod_id: str) -> dict:
        """详情（只读方法，远程连接也可调）：同 list_mods，不回本机绝对路径。"""
        mods = self.mods if self.mods is not None else self._build_mod_manager()
        mod = mods.mods.get(mod_id)
        if mod is None:
            raise RuntimeError("Mod 不存在：" + mod_id)
        return {
            "mod": mod.public_summary(active=mods.id_active(mod.id)),
            "manifest": dict(mod.manifest),
            "readme": read_mod_readme(mod.dir),
            "recent": list(mod.recent),
        }

    def mod_template(self) -> dict:
        return {"template": mods_template()}

    # ---- 安装（两段确认：needs_confirm + install_token，照 mcp stdio 导入模式） ----

    def _mod_source_candidates(self, staging: Path) -> list[Path]:
        """staging 里含清单的候选目录（mod.json/mods.json 任一，最浅层优先）。"""
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(staging):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            if any(name in MANIFEST_NAMES for name in filenames):
                found.append(Path(dirpath))
                dirnames[:] = []  # Mod 目录内部不再下钻
        # 最浅层优先：zip 外层包装目录里的才算
        found.sort(key=lambda p: len(p.relative_to(staging).parts))
        return found

    def _stage_mod_source(self, source: str) -> Path:
        """把安装来源（本机文件夹 / .zip）落到暂存目录，返回暂存路径。"""
        src = Path(str(source).strip()).expanduser()
        if not src.exists():
            raise ModError("路径不存在：" + str(src))
        root = mods_root()
        root.mkdir(parents=True, exist_ok=True)
        staging = root / f".importing-{uuid.uuid4().hex[:12]}"
        if src.is_file() and src.suffix.lower() == ".zip":
            self._unpack_mod_zip(src, staging)
        elif src.is_dir():
            shutil.copytree(src, staging)
        else:
            raise ModError("请选择 Mod 文件夹（含 mod.json）或 .zip 压缩包")
        return staging

    def _unpack_mod_zip(self, src: Path, staging: Path) -> None:
        """解压 Mod zip（skills._safe_members 同款防护：穿越/绝对路径/符号链接/超量）。"""
        if not zipfile.is_zipfile(src):
            raise ModError("不是有效的 .zip 文件：" + str(src))
        staging.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as zf:
            members: list[zipfile.ZipInfo] = []
            total = 0
            for info in zf.infolist():
                raw = info.filename.replace("\\", "/")
                if not raw.strip():
                    continue
                if raw.startswith("/") or ":" in raw.split("/")[0]:
                    raise ModError("压缩包里含绝对路径条目：" + raw)
                if Path(raw).is_absolute() or os.pardir in Path(raw).parts:
                    raise ModError("压缩包里含越界路径条目：" + raw)
                if (info.external_attr >> 16) & 0xF000 == 0xA000:
                    raise ModError("压缩包里含符号链接条目：" + raw)
                total += info.file_size
                if total > MAX_MOD_UNPACKED_BYTES:
                    raise ModError("Mod 包解压后过大，已中止")
                members.append(info)
            if len(members) > MAX_MOD_FILES:
                raise ModError("Mod 包文件数过多，已中止")
            zf.extractall(staging, members=members)

    def _preview_staged_mod(self, staging: Path, manifest_path: Path) -> dict:
        """安装预览：id/hooks/permissions 档/收紧表/文件数与字节/main.js 前 40 行。"""
        try:
            tf = read_text_file(manifest_path)
        except OSError as e:
            raise ModError(f"读清单失败：{e}") from e
        try:
            data = json.loads(tf.text)
        except ValueError as e:
            raise ModError(f"清单不是有效 JSON：{e}") from e
        manifest = parse_manifest(data, source_label=str(manifest_path))
        mod_dir = manifest_path.parent
        entry_path = mod_dir / manifest["entry"]
        # 入口必须 resolve 后仍在 Mod 目录内（防穿越）
        root_resolved = mod_dir.resolve()
        try:
            entry_resolved = entry_path.resolve()
        except OSError as e:
            raise ModError(f"入口文件不可读：{e}") from e
        if not entry_resolved.is_relative_to(root_resolved):
            raise ModError(f"入口指向 Mod 目录之外，已拒绝：{manifest['entry']}")
        if not entry_path.is_file():
            raise ModError(f"入口文件不存在：{manifest['entry']}")
        try:
            entry_bytes = entry_path.read_bytes()
        except OSError as e:
            raise ModError(f"读入口失败：{e}") from e
        if len(entry_bytes) > MAX_ENTRY_BYTES:
            raise ModError(f"入口超过 {MAX_ENTRY_BYTES // 1024}KB 上限")
        files = 0
        total = 0
        for p in mod_dir.rglob("*"):
            if p.is_file():
                files += 1
                total += p.stat().st_size
        head = entry_bytes.decode("utf-8", errors="replace").splitlines()[:40]
        return {
            "manifest": manifest,
            "staging": staging,
            "mod_dir": mod_dir,
            "preview": {
                "id": manifest["id"],
                "name": manifest["name"],
                "version": manifest["version"],
                "description": manifest["description"],
                "hooks": manifest["hooks"],
                "permissions": manifest["permissions"],
                "declarative": manifest["declarative"],
                "cc_mods": manifest["cc_mods"],
                "entry": manifest["entry"],
                "files": files,
                "bytes": total,
                "entry_head": "\n".join(head),
            },
        }

    async def install_mod(self, source: str) -> dict:
        """两段式安装第一步：校验来源并回 needs_confirm + 预览 + install_token。

        **未确认不落盘**（暂存目录在确认后移动或清理）；同一个时刻只保留一个
        待确认安装（新预览作废旧 token）。
        """
        staging = self._stage_mod_source(source)
        try:
            candidates = self._mod_source_candidates(staging)
            if not candidates:
                raise ModError("没有找到 Mod：目录里需要 mod.json（或 CC 别名 mods.json）")
            manifest_path = candidates[0] / (
                "mod.json" if (candidates[0] / "mod.json").is_file() else "mods.json"
            )
            preview = self._preview_staged_mod(staging, manifest_path)
        except ModError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        token = uuid.uuid4().hex[:16]
        # 同一时刻只保留一个待确认安装：被覆盖的旧预览不会再有人 confirm，
        # 它的暂存目录在这里清掉（否则 .importing-* 永久遗留）
        old = getattr(self, "_pending_mod_install", None)
        if old and old.get("staging"):
            shutil.rmtree(Path(str(old["staging"])), ignore_errors=True)
        self._pending_mod_install = {
            "token": token,
            "staging": str(preview["staging"]),
            "mod_dir": str(preview["mod_dir"]),
            "manifest": preview["manifest"],
        }
        return {"needs_confirm": True, "install_token": token, **preview["preview"]}

    async def confirm_install_mod(self, token: str) -> dict:
        """两段式安装第二步：凭 token 落盘安装（一次性）。"""
        pending = getattr(self, "_pending_mod_install", None)
        if not pending or pending.get("token") != str(token or ""):
            raise RuntimeError("安装确认已失效：请重新发起安装")
        staging = Path(pending["staging"])
        mod_dir = Path(pending["mod_dir"])
        manifest = pending["manifest"]
        try:
            target = _mod_target(mods_root(), manifest["id"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                rmtree_force(target)
            mod_dir.replace(target)
            # 来源标记（官方一键装会覆盖为 bundled）
            write_text_atomic(
                target / ".source.json",
                json.dumps({"source": "local", "installed_at": _now_iso()},
                           ensure_ascii=False),
            )
        finally:
            # 暂存目录一次性：落盘成功清残余包装，落盘失败（校验/移动抛错）
            # 也清——staging 已不存在，待确认项随之作废，用户需重新发起安装
            shutil.rmtree(staging, ignore_errors=True)
            self._pending_mod_install = None
        # 安装即启用：加进 enabled_mods 并打开总开关（用户主动装了却默认停用，
        # 会以为装失败；总开关此前是「实验性首版默认关」，随首次安装打开）
        _enabled, ids = _read_mods_cfg()
        if manifest["id"] not in ids:
            ids.append(manifest["id"])
        set_mods_in_config(enabled=True, enabled_mods=ids)
        self._reload_mods()
        self.notify_mods_updated()
        return {"installed": manifest["id"], "enabled": True}

    async def install_official_mod(self, mod_id: str) -> dict:
        """官方示例一键安装：打包内 mods-gallery 副本优先（bundled 优先，无网络）。"""
        from ...core.mods import bundled_gallery_dir, load_gallery_manifest

        manifest_list = load_gallery_manifest()
        entry = next((m for m in manifest_list if str(m.get("id")) == str(mod_id)), None)
        if entry is None:
            raise RuntimeError("不是官方示例 Mod：" + str(mod_id))
        bundled = bundled_gallery_dir()
        src = bundled / str(entry.get("dir", "")) if bundled else None
        if src is None or not src.is_dir():
            raise RuntimeError(
                "官方示例包在本机不可用（源码运行态没有打包副本）——请改用本地文件夹安装"
            )
        target = _mod_target(mods_root(), str(mod_id))
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            rmtree_force(target)
        shutil.copytree(src, target)
        write_text_atomic(
            target / ".source.json",
            json.dumps({"source": "bundled", "installed_at": _now_iso()},
                       ensure_ascii=False),
        )
        _enabled, ids = _read_mods_cfg()
        if str(mod_id) not in ids:
            ids.append(str(mod_id))
        set_mods_in_config(enabled=True, enabled_mods=ids)
        self._reload_mods()
        self.notify_mods_updated()
        return {"installed": str(mod_id), "enabled": True}

    def official_mods(self) -> dict:
        """官方示例清单（打包内副本 + 已装比对），纯本地只读。"""
        from ...core.mods import bundled_gallery_dir, load_gallery_manifest

        mods = self.mods
        installed_ids = set(mods.mods) if mods else set()
        entries = []
        for m in load_gallery_manifest():
            mid = str(m.get("id", ""))
            entries.append({
                "id": mid,
                "dir": str(m.get("dir", "")),
                "name": str(m.get("display_name") or mid),
                "description": str(m.get("description", "")),
                "hooks": m.get("hooks") or [],
                "installed": mid in installed_ids,
            })
        return {"mods": entries, "bundled": bundled_gallery_dir() is not None}

    async def delete_mod(self, mod_id: str) -> dict:
        """删除 Mod 目录（落点校验同安装）并从启用名单摘除。"""
        target = _mod_target(mods_root(), str(mod_id))
        if not target.is_dir():
            raise RuntimeError("Mod 不存在：" + str(mod_id))
        rmtree_force(target)
        delete_mod_state(str(mod_id))
        _enabled, ids = _read_mods_cfg()
        if str(mod_id) in ids:
            ids.remove(str(mod_id))
            set_mods_in_config(enabled=_enabled, enabled_mods=ids)
        self._reload_mods()
        self.notify_mods_updated()
        return {"deleted": str(mod_id)}

    async def toggle_mod(self, mod_id: str, enabled: bool) -> dict:
        mods = self.mods if self.mods is not None else self._build_mod_manager()
        if str(mod_id) not in mods.mods:
            raise RuntimeError("Mod 不存在：" + str(mod_id))
        cur_enabled, ids = _read_mods_cfg()
        if enabled and str(mod_id) not in ids:
            ids.append(str(mod_id))
        if not enabled and str(mod_id) in ids:
            ids.remove(str(mod_id))
        set_mods_in_config(enabled=cur_enabled, enabled_mods=ids)
        # 启停只切执行资格，不换 Mod 本体：「最近执行」贴回，否则停用再启用，
        # 用户视角执行历史无声消失，与「停用后保留安装」的文案预期相悖
        self._reload_mods(keep_recent=True)
        self.notify_mods_updated()
        return {"id": str(mod_id), "enabled": bool(enabled)}

    async def set_mods_enabled(self, enabled: bool) -> dict:
        _cur, ids = _read_mods_cfg()
        set_mods_in_config(enabled=bool(enabled), enabled_mods=ids)
        # 总开关同款：关开之间不清「最近执行」（同一进程周期的展示状态）
        self._reload_mods(keep_recent=True)
        self.notify_mods_updated()
        return {"enabled": bool(enabled)}

    # ---- 草稿（让 Agent 创建 Mod：保存 → 人工安装确认两道人工动作） ----

    async def save_mod_draft(self, params: dict) -> dict:
        """草稿落盘：<项目>/.skysheep/mods-drafts/<id>/。不进 Mods 根，装不装由用户。"""
        mod_id = str(params.get("id", "")).strip()
        main_js = str(params.get("main_js", "") or "")
        readme = str(params.get("readme", "") or "")
        manifest_raw = params.get("manifest")
        if isinstance(manifest_raw, str):
            try:
                manifest_raw = json.loads(manifest_raw)
            except ValueError as e:
                raise RuntimeError(f"清单不是有效 JSON：{e}") from e
        if not isinstance(manifest_raw, dict):
            raise RuntimeError("请提供 mod.json 清单内容（对象）")
        manifest = parse_manifest(manifest_raw, source_label="草稿")
        if manifest["id"] != mod_id:
            raise RuntimeError(
                f"清单 id（{manifest['id']}）与目录 id（{mod_id}）不一致"
            )
        if not main_js.strip():
            raise RuntimeError("main.js 内容为空")
        if len(main_js.encode("utf-8")) > MAX_ENTRY_BYTES:
            raise RuntimeError(f"main.js 超过 {MAX_ENTRY_BYTES // 1024}KB 上限")
        if self.working_dir is None:
            raise RuntimeError("当前没有项目，无法保存草稿——先添加并打开一个项目")
        draft_dir = self.working_dir / ".skysheep" / "mods-drafts" / manifest["id"]
        draft_dir.parent.mkdir(parents=True, exist_ok=True)
        draft_dir.mkdir(parents=True, exist_ok=True)
        # 引擎自有状态文件一律原子写（textio.write_text_atomic）
        write_text_atomic(
            draft_dir / "mod.json",
            json.dumps(manifest_raw, ensure_ascii=False, indent=2) + "\n",
        )
        write_text_atomic(draft_dir / "main.js", main_js)
        if readme.strip():
            write_text_atomic(draft_dir / "README.md", readme)
        return {"saved": manifest["id"], "path": str(draft_dir),
                "name": manifest["name"]}

    # ---- 测试器（照 test_hook 模式：实跑一遍，不记执行记录） ----

    async def test_mod(self, params: dict) -> dict:
        mods = self.mods if self.mods is not None else self._build_mod_manager()
        mod_id = str(params.get("id", "")).strip()
        hook = str(params.get("hook", "")).strip()
        mod = mods.mods.get(mod_id)
        if mod is None:
            raise RuntimeError("Mod 不存在：" + str(mod_id))
        if not mods.runtime_available:
            raise RuntimeError("JS 运行时不可用，无法测试（declarative 收紧声明不受影响）")
        js_hooks = {
            "tool_pre": "toolPre", "tool_post": "toolPost",
            "permission_request": "permissionRequest",
            "iteration_start": "iterationStart", "turn_stop": "turnStop",
        }
        if hook not in js_hooks:
            raise RuntimeError("hook 只能是 " + " / ".join(js_hooks))
        raw_input = params.get("input")
        input_dict = raw_input if isinstance(raw_input, dict) else {"path": "demo.txt"}
        tool = str(params.get("tool") or "write_file")
        session_id = str(params.get("session_id") or "test")
        payload: dict = {"tool": tool, "input": input_dict, "sessionId": session_id}
        if hook == "tool_post":
            payload.update({"resultPreview": "demo result", "isError": False,
                            "durationMs": 12})
        elif hook == "permission_request":
            payload.update({"safety": "dangerous", "detail": tool})
        elif hook == "iteration_start":
            payload.clear()
            payload.update({"sessionId": session_id, "iteration": 1})
        elif hook == "turn_stop":
            payload.clear()
            payload.update({"sessionId": session_id, "iterations": 3,
                            "stopReason": "end_turn", "contextTokens": 1024})
        state = load_mod_state(mod_id).get(session_id, {})
        raw, new_state, error = await mod.run_hook(hook, payload, state, record=False)
        errors: list[str] = []
        # 与实跑（ModManager._run）同口径：只有 tighten 档的 deny 有效。observe
        # 档硬放行 deny 会把「实跑必被丢弃」的动作报成有效，误导 Mod 作者。
        result = (
            _parse_action_result(
                raw, hook=hook, allow_deny=(mod.permissions == "tighten"),
                mod_id=mod_id, errors=errors,
            ) if not error else {"deny": None, "note": "", "ui": []}
        )
        return {
            "hook": hook,
            "raw": raw if isinstance(raw, (dict, list, str, int, float, bool, type(None)))
            else str(raw),
            "result": result,
            "state_after": new_state,
            "error": error,
            "parse_errors": errors,
        }
