"""设置导出/导入与整机迁移包的导入侧（与 MigrationMixin 同一安全模型的两半）。

D6 基础设施与迁移域（backend.py Mixin 拆分批 2 外迁件）。从 backend.py
按职责注释整段搬入，方法体逐字保留，逻辑不变。

安全模型与 migration.py 同域同源（批 2 注记，设计 §2.1）：迁移红线
「降防护不随包生效」两边各实现一遍——本文件 settings.import / 完整导入
把 ui.json 的 accept_edits 剥掉（_strip_accept_edits_from_ui_export），
migration.py 导入侧对库内自动化/白名单/渠道绑定表与 config.toml 防护总开关
做同款再消毒；两处必须同批核对，不许分域（分域会漂移）。

落盘纪律：全部走 textio 原子入口（write_bytes_atomic / write_text_atomic），
回归在 tests/test_settings_io.py（patch 锚点随迁到本模块）。

搬移注记：原方法体内 3 处 `ServerBackend._xxx` 静态自引用改为
`SettingsIOMixin._xxx`——同一实现、MRO 上仅此一份，语义不变；新文件不能
反向 import 宿主类。

跨域只读（不收敛，注记）：`self._backup_busy_message` /
`self._rebuild_after_db_replace`（D1 LifecycleMixin）、`relay_freeze_all`
（D2 RelaySettingsMixin）、`self._sync_mcp_changes`（D6 McpMixin）、
`self._apply_gate_accept_pref`（D1 PermissionModesMixin）、`self._read_ui_prefs`
（D6 PreferencesMixin）、`self.skills` / `self.subagent_store` / `self.store`
（装配型单例，消费在本域）——经共享 self 取用。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import date
from pathlib import Path

from ...config import load_config, skysheep_home
from ...skills.installer import SkillInstallError
from ...textio import write_bytes_atomic, write_text_atomic

logger = logging.getLogger(__name__)


class SettingsIOMixin:
    """全量导出导入与设置包迁移（settings.* / data.* WS 方法的实现面）。

    方法自 backend.py 逐字搬入；`ServerBackend._xxx` 自引用改为本类名。
    """

    # 设置导出/导入覆盖的文件（~/.skysheep 下；会话数据库与检查点不导，体积大且含隐私对话）
    SETTINGS_EXPORT_FILES = (
        "config.toml", "mcp.json", "ui.json", "memory.md", "instructions.md",
        "subagents.json", "skills-scope.json",
    )

    async def settings_export(self) -> dict:
        """把配置类文件打包为 zip（base64 回传前端下载）。用于换机迁移/备份。"""
        import base64
        import io
        import zipfile

        buf = io.BytesIO()
        included = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name in self.SETTINGS_EXPORT_FILES:
                p = skysheep_home() / name
                if p.is_file():
                    try:
                        zf.writestr(name, p.read_bytes())
                        included.append(name)
                    except OSError:
                        pass
            # 项目级 AGENTS.md 不在 home 下，跳过；技能目录打包清单文件（体积考虑，不含技能正文）
            skills = skysheep_home() / "skills"
            if skills.is_dir():
                manifest = [
                    {"name": s.name, "enabled": s.enabled}
                    for s in self.skills.all() if s.source == "global"
                ]
                zf.writestr("skills-manifest.json", json.dumps(manifest, ensure_ascii=False))
                included.append("skills-manifest.json")
        return {
            "filename": f"skysheep-settings-{date.today().isoformat()}.zip",
            "b64": base64.b64encode(buf.getvalue()).decode("ascii"),
            "included": included,
        }

    async def export_full_data(self, params: dict) -> dict:
        """一键完整导出：配置 + 会话数据库快照 + 技能目录 + 记忆，打包落盘并定位。

        与 settings.export 的区别：这是「整机迁移包」——含会话库快照与技能正文，
        数据目录里导出（exports/），不 base64 回传（体量可能几百 MB）。
        include_secrets=False（默认）时 config.toml 用打码版；True 时原样（含
        明文/密文 Key，导入后可直接用）。数据库快照用 backup API，不直接拷
        使用中的库文件。
        """
        import zipfile
        from datetime import datetime as _dt

        from ... import support

        busy = self._backup_busy_message("导出")
        if busy:
            raise RuntimeError(busy)
        home = skysheep_home()
        include_secrets = bool(params.get("include_secrets", False))
        out_dir = home / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        target = out_dir / f"skysheep-full-{_dt.now().strftime('%Y%m%d-%H%M%S')}.zip"
        included: list[str] = []

        # 数据库快照：拷到临时文件再入包（直接读使用中的库文件会拿到撕裂状态）
        db_tmp = out_dir / f".export-tmp-{_dt.now().strftime('%H%M%S%f')}.db"
        try:
            from ...session.store import _snapshot_or_copy

            await asyncio.to_thread(_snapshot_or_copy, self.store.path, db_tmp)
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(db_tmp, "skysheep.db")
                included.append("skysheep.db")
                # 配置文件：config.toml 按开关决定打码版或原样
                for name in self.SETTINGS_EXPORT_FILES:
                    p = home / name
                    if not p.is_file():
                        continue
                    if name == "config.toml" and not include_secrets:
                        zf.writestr(name, support._redact_toml(p))
                    else:
                        zf.write(p, name)
                    included.append(name)
                # 技能正文（全局技能目录整个打包；数量上限已由安装层限制）
                skills = home / "skills"
                if skills.is_dir():
                    count = 0
                    for f in sorted(skills.rglob("*")):
                        if f.is_file() and ".backups" not in f.parts:
                            zf.write(f, f"skills/{f.relative_to(skills).as_posix()}")
                            count += 1
                    if count:
                        included.append(f"skills/（{count} 个文件）")
                zf.writestr("README.txt", (
                    "SkySheep 完整数据导出\n"
                    f"导出时间：{_dt.now().isoformat(timespec='seconds')}\n"
                    f"包含：{'、'.join(included)}\n"
                    + ("config.toml 已打码（不含 API Key）。\n" if not include_secrets
                       else "⚠ config.toml 为原样（含 API Key），请妥善保管本文件。\n")
                    + "\n恢复方式：在目标电脑的 SkySheep 里打开 设置 → 关于，\n"
                    "点「从导出包恢复…」选中本文件即可；会自动校验并保留恢复前副本。\n"
                ))
        except Exception as e:  # noqa: BLE001
            target.unlink(missing_ok=True)
            raise RuntimeError(f"导出失败：{e}") from None
        finally:
            db_tmp.unlink(missing_ok=True)
        try:
            support.reveal(target)
        except Exception:  # noqa: BLE001 - 打不开文件夹不是失败
            pass
        return {
            "path": str(target),
            "size": target.stat().st_size,
            "included": included,
            "include_secrets": include_secrets,
        }

    async def settings_preview_import(self, params: dict) -> dict:
        """导入前预览：只解析 zip 不落盘，列出将要覆盖的文件与关键提示。

        返回 items（文件名/大小/是否含 API Key 特征）、unknown（不在白名单的
        文件）、contains_secrets（config.toml 里是否有 key/token 行）。前端拿到
        清单让用户确认后再调 settings.import 真正写入。
        """
        import base64
        import io
        import zipfile

        raw = str(params.get("b64", "") or "")
        if not raw:
            raise RuntimeError("缺少 zip 内容（b64）")
        try:
            data = base64.b64decode(raw, validate=True)
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"zip 解析失败: {e}") from None
        items, unknown = [], []
        contains_secrets = False
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name not in self.SETTINGS_EXPORT_FILES:
                unknown.append(name)
                continue
            entry: dict = {"name": name, "size": info.file_size}
            if name == "config.toml":
                try:
                    text = zf.read(name).decode("utf-8", errors="replace")
                    contains_secrets = any(
                        marker in text for marker in ("api_key", "token", "secret")
                    )
                    entry["has_secrets"] = contains_secrets
                except Exception:  # noqa: BLE001
                    pass
            items.append(entry)
        return {
            "items": items,
            "unknown": unknown,
            "contains_secrets": contains_secrets,
            "note": (
                "包里包含 API Key / 令牌等敏感内容，导入后会覆盖本机当前配置。"
                if contains_secrets else "将恢复上列文件到本机数据目录（覆盖同名文件）。"
            ),
        }

    async def settings_import(self, params: dict) -> dict:
        """从导出的 zip 恢复设置（base64 上传）。全有或全无：失败自动回滚。

        先整体校验（config.toml 必须是合法 TOML、ui.json 必须是合法 JSON），
        再写入；写入阶段任一文件失败，或写完后 config.toml 仍加载失败，
        都按导入前快照把本批文件整体还原——不留「导入了一半」的中间态。
        返回恢复清单；config.toml 恢复后由前端 boot() 重载生效。
        """
        import base64
        import io
        import zipfile

        raw = str(params.get("b64", "") or "")
        if not raw:
            raise RuntimeError("缺少 zip 内容（b64）")
        try:
            data = base64.b64decode(raw, validate=True)
            zf = zipfile.ZipFile(io.BytesIO(data))
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"zip 解析失败: {e}") from None
        # 白名单过滤（skills-manifest.json 等暂不自动恢复，避免覆盖现有技能状态）
        pending: dict[str, bytes] = {}
        skipped: list[str] = []
        for name in zf.namelist():
            if name not in self.SETTINGS_EXPORT_FILES:
                skipped.append(name)
                continue
            pending[name] = zf.read(name)
        # 写入前整体校验：内容不合法的包一个字节都不落盘。
        # config.toml 用 tomllib 按真实加载口径解析；ui.json 先做与
        # _strip_accept_edits_from_ui_export 相同的解析（不是 dict 的内容照旧
        # 拒绝写入——旧实现「保留原行为」在回滚语义下会变成静默不写入，
        # 不如 upfront 说清楚）。
        if "config.toml" in pending:
            try:
                import tomllib

                tomllib.loads(pending["config.toml"].decode("utf-8"))
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"包里的 config.toml 不是合法 TOML，已取消导入：{e}") from None
        if "ui.json" in pending:
            try:
                parsed = json.loads(pending["ui.json"].decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                raise RuntimeError(f"包里的 ui.json 不是合法 JSON，已取消导入：{e}") from None
            if not isinstance(parsed, dict):
                raise RuntimeError("包里的 ui.json 内容不是 JSON 对象，已取消导入")
        # 导入前快照：原文件字节（不存在记 None），失败时按它整体还原
        home = skysheep_home()
        snapshot: dict[str, bytes | None] = {}
        for name in pending:
            p = home / name
            try:
                snapshot[name] = p.read_bytes() if p.is_file() else None
            except OSError:
                snapshot[name] = None

        def _rollback() -> None:
            """把本批文件还原到导入前状态；还原失败只记日志（尽力而为）。"""
            for name, old in snapshot.items():
                p = home / name
                try:
                    if old is None:
                        p.unlink(missing_ok=True)  # 导入前不存在：删掉新写的
                    else:
                        write_bytes_atomic(p, old)
                except OSError:
                    logger.warning("设置导入回滚 %s 失败", name, exc_info=True)

        restored: list[str] = []
        for name, blob in pending.items():
            target = home / name
            try:
                if name == "ui.json":
                    # 「自动允许写入」属于降低防护的开关，只能由本机用户主动切换：
                    # 导入包里带来的该字段一律不生效（换机/别人给的包不应该顺手放宽写入）
                    self._strip_accept_edits_from_ui_export(target, blob)
                else:
                    write_bytes_atomic(target, blob)
            except OSError as e:
                _rollback()
                raise RuntimeError(f"写入 {name} 失败，已回滚本次导入（未改动任何文件）：{e}") from None
            restored.append(name)
        if "config.toml" in restored:
            try:
                self.cfg = load_config()
            except Exception as e:  # noqa: BLE001
                # 校验已挡住绝大多数坏配置；走到这里（如本机路径相关的加载错误）
                # 同样整体还原，不让坏 config.toml 留在盘上
                _rollback()
                try:
                    self.cfg = load_config()
                except Exception:  # noqa: BLE001 - 还原后仍加载失败：保持错误信息聚焦
                    pass
                raise RuntimeError(f"配置恢复后加载失败，已回滚本次导入：{e}") from None
        if "ui.json" in restored:
            # 导入后同步内存里的档位（否则要等重启才与文件一致）
            self._apply_gate_accept_pref(self._read_ui_prefs().get("accept_edits", 0))
        return {"restored": restored, "skipped": skipped}

    @staticmethod
    def _strip_accept_edits_from_ui_export(target: Path, blob: bytes) -> None:
        """写入导入的 ui.json 时去掉 accept_edits（其余偏好照常恢复）。"""
        try:
            data = json.loads(blob.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            data = None
        if not isinstance(data, dict):
            return  # 内容不是合法 JSON：保留原行为，不写入半个文件
        if data.pop("accept_edits", None) is not None:
            logger.info("导入的 ui.json 里带着 accept_edits，已丢弃（该开关只能在本机切换）")
        write_text_atomic(target, json.dumps(data, ensure_ascii=False), encoding="utf-8")

    # ---- 完整导出包的导入（与 export_full_data 配对；仅本机） ----

    # 导入包里除配置与技能外还认识的顶层文件：读了不报错，但不需要恢复
    FULL_IMPORT_KNOWN_TOP = ("skysheep.db", "README.txt", "skills-manifest.json")

    @staticmethod
    def _full_import_zip_path(params: dict) -> Path:
        """取参数里的导出包路径：必须是已存在的 .zip 本机文件。"""
        raw = str(params.get("path", "") or "").strip().strip('"')
        if not raw:
            raise RuntimeError("缺少导出包路径（path）")
        path = Path(raw).expanduser()
        if not path.is_file():
            raise RuntimeError(f"找不到导出包：{raw}")
        if path.suffix.lower() != ".zip":
            raise RuntimeError("请选择完整导出生成的 .zip 文件")
        return path

    @staticmethod
    def _validate_staged_db(db_file: Path) -> int:
        """导入前校验暂存库：quick_check + 版本降级防护，返回库版本号。

        与 connect() 的降级守卫同一口径，但提前到替换主库**之前**——否则新版
        库换上去后 connect 拒开，主库已被换掉、store 停在断开状态，只能靠
        安全副本手工救回。先验后换，坏库根本不进主库位置。
        """
        import sqlite3

        from ...session.store import SCHEMA_VERSION
        try:
            conn = sqlite3.connect(db_file)
        except sqlite3.Error as e:
            raise RuntimeError(f"导出包里的会话库打不开：{e}") from None
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
            check = str(row[0]) if row else ""
            if check != "ok":
                raise RuntimeError(f"导出包里的会话库自检未通过（{check}），已取消导入")
            ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as e:
            raise RuntimeError(f"导出包里的会话库不是有效的 SQLite 库：{e}") from None
        finally:
            conn.close()
        if ver > SCHEMA_VERSION:
            raise RuntimeError(
                f"导出包来自更新版本的 SkySheep（数据版本 {ver}，本机支持 "
                f"{SCHEMA_VERSION}）。请先升级本机 SkySheep 再导入，"
                "或改用只含配置的「导入设置」。"
            )
        return ver

    def _stage_full_import(self, zf, dest: Path) -> tuple[list[str], list[str]]:
        """把导出包安全解包到 dest：白名单顶层名 + 逐段防路径逃逸。

        返回 (staged, unknown)：staged 是已解包的包内相对路径（/ 分隔），
        unknown 是不在白名单里的条目（不解包，原样回报给用户）。技能只解
        skills/<名>/… 两层以上形状；skills 根下的散文件不认识就不收。
        """
        staged: list[str] = []
        unknown: list[str] = []
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            parts = name.split("/")
            # 每一段都不得是空/点/点点/含盘符或流分隔符（冒号）——zip slip 的
            # 两套常见变形（绝对路径、上级引用）都在这里被挡住
            if any(p in ("", ".", "..") or ":" in p for p in parts):
                unknown.append(info.filename)
                continue
            top, rest = parts[0], parts[1:]
            if top in self.FULL_IMPORT_KNOWN_TOP and not rest:
                pass  # 单文件条目
            elif top in self.SETTINGS_EXPORT_FILES and not rest:
                pass
            elif top == "skills" and len(parts) >= 3:
                pass  # skills/<技能名>/<文件…>
            else:
                unknown.append(info.filename)
                continue
            target = dest / "/".join(parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            except OSError:
                unknown.append(info.filename)
                continue
            staged.append(name)
        return staged, unknown

    @staticmethod
    def _snapshot_config_files(staged: dict[str, bytes]) -> dict[str, bytes | None]:
        """在任何写入之前把全部目标文件的现状读成快照（名字 → 原字节或 None）。

        旧实现把建快照和写入放在同一个循环里、且只在函数正常返回时才把快照
        交给调用方：第二个文件写入失败时，第一个已写入文件的快照丢了，回滚
        拿到空字典，声称「已回滚」却留下半写状态。现在快照先行、无条件可用。
        """
        home = skysheep_home()
        snapshot: dict[str, bytes | None] = {}
        for name in sorted(staged):
            p = home / name
            try:
                snapshot[name] = p.read_bytes() if p.is_file() else None
            except OSError:
                snapshot[name] = None
        return snapshot

    @staticmethod
    def _restore_config_files_from(
        staged: dict[str, bytes],
        snapshot: dict[str, bytes | None] | None = None,
    ) -> list[str]:
        """把暂存的配置文件写入数据目录，返回已恢复清单。

        snapshot 由调用方提前调 _snapshot_config_files 建好（回滚靠它）；
        不传时内部补建一次（仅供单独调用这个 helper 的场景）。
        """
        home = skysheep_home()
        if snapshot is None:
            snapshot = SettingsIOMixin._snapshot_config_files(staged)
        restored: list[str] = []
        for name in sorted(staged):
            p = home / name
            if name == "ui.json":
                # 与 settings.import 同一防护：accept_edits 只能在本机切换
                SettingsIOMixin._strip_accept_edits_from_ui_export(p, staged[name])
            else:
                write_bytes_atomic(p, staged[name])
            restored.append(name)
        return restored

    @staticmethod
    def _rollback_config_files(snapshot: dict[str, bytes | None]) -> list[str]:
        """按快照把本批配置文件还原到导入前状态；返回未能还原的名字。

        尽力而为，但不再静默：调用方拿这个列表决定错误文案，有残留就如实
        告知用户（旧实现只记日志后照样声称「已回滚」）。
        """
        home = skysheep_home()
        failed: list[str] = []
        for name, old in snapshot.items():
            p = home / name
            try:
                if old is None:
                    p.unlink(missing_ok=True)
                else:
                    write_bytes_atomic(p, old)
            except OSError:
                failed.append(name)
                logger.warning("完整导入回滚 %s 失败", name, exc_info=True)
        return failed

    async def _replace_store_db(self, staged_db: Path) -> Path | None:
        """用暂存库整体替换当前会话库，返回替换前的安全副本路径（可能为 None）。

        关库 → 清 WAL/SHM 残留 → 原子换文件 → 重开。替换前的现状先快照到
        backups/（恢复前标签，界面可辨认），失败路径靠它回滚。
        """
        from ...session.store import _snapshot_or_copy

        store = self.store
        safety = None
        if store.path.exists() and store.path.stat().st_size > 0:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safety = store.backup_dir() / f"{store.path.stem}-{stamp}{store.SAFETY_TAG}.db"
            store.backup_dir().mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(_snapshot_or_copy, store.path, safety)
        if store._db is not None:
            await store._db.close()
            store._db = None
        # 常驻写连接同样攥着库文件句柄，不收掉 os.replace 会被拒绝访问
        await store._close_writer()
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(store.path) + suffix).unlink()
            except OSError:
                pass
        await asyncio.to_thread(os.replace, staged_db, store.path)
        await store.connect()
        return safety

    async def _undo_store_db_replace(self, safety: Path | None, *, had_db: bool) -> None:
        """把主库回滚到替换前状态：有安全副本就换回去，全新目录则删掉新库。"""
        store = self.store
        if store._db is not None:
            try:
                await store._db.close()
            except Exception:  # noqa: BLE001 - 回滚路径上的关闭失败不掩盖原错误
                pass
            store._db = None
        # 常驻写连接同样攥着库文件句柄，回滚换文件前必须收
        try:
            await store._close_writer()
        except Exception:  # noqa: BLE001 - 回滚路径上的关闭失败不掩盖原错误
            pass
        for suffix in ("-wal", "-shm"):
            try:
                Path(str(store.path) + suffix).unlink()
            except OSError:
                pass
        try:
            if safety is not None and safety.is_file():
                await asyncio.to_thread(os.replace, safety, store.path)
            elif not had_db:
                store.path.unlink(missing_ok=True)
        except OSError:
            logger.warning("会话库回滚失败，请从备份目录手工恢复：%s", safety, exc_info=True)
        try:
            await store.connect()
        except Exception:  # noqa: BLE001 - 重连失败如实附在日志，原错误继续向上抛
            logger.exception("会话库回滚后重开失败")

    async def preview_import_full(self, params: dict) -> dict:
        """完整导入前预览：解析导出包但不落盘，列出将恢复的内容与校验结果。

        会话库在这里就做 quick_check + 版本校验——问题包在预览阶段就被拦下，
        不等用户确认后才报错。返回 items（配置/库文件清单）、skills_count、
        db（大小/版本/校验结果）、contains_secrets、unknown。
        """
        import tempfile
        import zipfile

        path = self._full_import_zip_path(params)
        try:
            zf = zipfile.ZipFile(path)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"打不开导出包（不是合法 zip？）：{e}") from None
        with zf:
            names = [i.filename.replace("\\", "/") for i in zf.infolist() if not i.is_dir()]
            has_db = "skysheep.db" in names
            cfg_files = [n for n in self.SETTINGS_EXPORT_FILES if n in names]
            skills_files = [n for n in names
                            if n.startswith("skills/") and len(n.split("/")) >= 3]
            known_top = set(self.FULL_IMPORT_KNOWN_TOP) | set(self.SETTINGS_EXPORT_FILES)
            unknown = [n for n in names
                       if n.split("/")[0] not in known_top and not n.startswith("skills/")]
            contains_secrets = False
            if "config.toml" in cfg_files:
                try:
                    text = zf.read("config.toml").decode("utf-8", errors="replace")
                    contains_secrets = any(m in text for m in ("api_key", "token", "secret"))
                except Exception:  # noqa: BLE001 - 读不出来留到导入阶段报错
                    pass
            db_info: dict | None = None
            if has_db:
                # 解包到临时文件再校验（quick_check 需要真实文件；zip 里的库
                # 可能几百 MB，不整读进内存）
                with tempfile.TemporaryDirectory(prefix="skysheep-import-preview-") as td:
                    staged_db = Path(td) / "skysheep.db"
                    with zf.open("skysheep.db") as src, open(staged_db, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    size = staged_db.stat().st_size
                    try:
                        ver = self._validate_staged_db(staged_db)
                        db_info = {"size": size, "version": ver, "ok": True, "reason": ""}
                    except RuntimeError as e:
                        db_info = {"size": size, "version": None, "ok": False, "reason": str(e)}
            items = []
            if has_db:
                items.append({"name": "skysheep.db", "size": zf.getinfo("skysheep.db").file_size,
                              "kind": "db"})
            items.extend({"name": n, "size": zf.getinfo(n).file_size, "kind": "config"}
                         for n in cfg_files)
        skills_names = sorted({n.split("/")[1] for n in skills_files})
        return {
            "path": str(path),
            "items": items,
            "skills": skills_names,
            "skills_count": len(skills_names),
            "db": db_info,
            "contains_secrets": contains_secrets,
            "unknown": unknown,
            "note": (
                "将用包里的会话库整体替换当前会话数据（替换前自动留「恢复前」副本）；"
                "配置文件覆盖同名项；技能按名字合并恢复（同名覆盖、多出的保留）。"
                if has_db else
                "包里没有会话库，只恢复配置与技能。"
            ) + (" ⚠ 包里含 API Key / 令牌。" if contains_secrets else ""),
        }

    async def import_full_data(self, params: dict) -> dict:
        """从完整导出包恢复：会话库 + 配置 + 技能，全有或全无，失败自动回滚。

        与 export_full_data 配对，跨机迁移的第二步。顺序：安全解包 → 校验库
        → 换库（替换前留安全副本）→ 恢复配置 → 合并技能 → 热重载（配置 /
        技能 / MCP / 子代理）→ 重建会话内存状态。任何一步失败都把已动的
        部分（库 + 配置）还原到导入前，不留中间态。
        """
        import zipfile

        busy = self._backup_busy_message("导入")
        if busy:
            raise RuntimeError(busy)
        # 接力冻结（接力模式设计 §7：导入备份/替换 store 先冻结与收敛接力；
        # 库文件将被整体替换，按重启恢复在 _rebuild_after_db_replace 内做）
        await self.relay_freeze_all()
        path = self._full_import_zip_path(params)
        home = skysheep_home()
        out_dir = home / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        # datetime.strftime 才支持 %f（time.strftime 在 Windows 上没有该指令）
        from datetime import datetime as _dt

        stage = out_dir / f".import-tmp-{_dt.now().strftime('%Y%m%d-%H%M%S%f')}"
        stage.mkdir(parents=True)
        store = self.store
        had_db = store.path.exists() and store.path.stat().st_size > 0
        safety: Path | None = None
        cfg_restored: list[str] = []
        cfg_snapshot: dict[str, bytes | None] = {}
        db_replaced = False
        skills_installed: list[str] = []
        skills_overwritten: list[str] = []
        skills_backup: dict[str, Path] = {}
        skills_root = home / "skills"
        skills_backup_root = stage / ".skill-backup"

        async def _undo_all(residual: list[str]) -> list[str]:
            """按逆序整体回滚：技能 → 配置 → 库。返回未能还原的名字清单。

            旧实现只在配置/热重载两处分别回滚，技能复制不在回滚面内；且回滚
            失败只记日志后照样声称「已回滚」。现在统一收口并回报残留。
            """
            failed: list[str] = []
            for name in reversed(skills_installed):
                dst = skills_root / name
                backup = skills_backup.get(name)
                try:
                    if backup is not None and backup.is_dir():
                        # 原本就存在的技能：先删新内容再放回备份
                        shutil.rmtree(dst, ignore_errors=True)
                        shutil.copytree(backup, dst, dirs_exist_ok=True)
                    else:
                        # 原本不存在：删掉新增目录
                        shutil.rmtree(dst, ignore_errors=True)
                except OSError:
                    failed.append("skills/" + name)
            if cfg_snapshot:
                failed.extend(SettingsIOMixin._rollback_config_files(cfg_snapshot))
            if db_replaced:
                await self._undo_store_db_replace(safety, had_db=had_db)
            failed.extend(residual)
            return failed

        try:
            try:
                zf = zipfile.ZipFile(path)
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(f"打不开导出包（不是合法 zip？）：{e}") from None
            with zf:
                staged, unknown = self._stage_full_import(zf, stage)
            if not staged:
                raise RuntimeError("包里没有可恢复的内容（既没有会话库、配置，也没有技能）")
            staged_db = stage / "skysheep.db"
            if staged_db.is_file():
                self._validate_staged_db(staged_db)  # 新版库 / 坏库在这里被拦下

            # 1) 换库（替换前自动留「恢复前」安全副本）
            if staged_db.is_file():
                try:
                    safety = await self._replace_store_db(staged_db)
                    db_replaced = True
                except Exception as e:  # noqa: BLE001
                    await self._undo_store_db_replace(safety, had_db=had_db)
                    raise RuntimeError(f"会话库恢复失败，已回滚：{e}") from None

            # 2) 配置文件（内存快照先行，失败按它还原）
            staged_cfg = {n: (stage / n).read_bytes()
                          for n in self.SETTINGS_EXPORT_FILES if (stage / n).is_file()}
            if staged_cfg:
                # 先校验再建快照再写入：任何一步失败时快照已完整，回滚可用；
                # 校验不通过时尚未写盘，连回滚都不需要（旧实现是边写边校验，
                # 第二项写失败时才手忙脚乱回滚）。
                try:
                    if "config.toml" in staged_cfg:
                        import tomllib

                        tomllib.loads(staged_cfg["config.toml"].decode("utf-8"))
                    if "ui.json" in staged_cfg:
                        parsed_ui = json.loads(staged_cfg["ui.json"].decode("utf-8"))
                        if not isinstance(parsed_ui, dict):
                            raise ValueError("ui.json 不是 JSON 对象")
                except Exception as e:  # noqa: BLE001
                    raise RuntimeError(
                        f"导入包配置校验不通过，未写入任何文件（本机数据未改动）：{e}"
                    ) from None
                cfg_snapshot = self._snapshot_config_files(staged_cfg)
                try:
                    cfg_restored = self._restore_config_files_from(staged_cfg, cfg_snapshot)
                except Exception as e:  # noqa: BLE001
                    failed = await _undo_all([])
                    raise RuntimeError(
                        "配置恢复失败，已回滚"
                        + (f"（残留未还原：{'、'.join(failed)}）" if failed else "")
                        + f"：{e}"
                    ) from None

            # 3) 技能合并恢复：同名覆盖、包里没有的保留；目录名先过安装器
            # 同款校验（挡 ..、分隔符、盘符），再逐棵复制。技能也在回滚面内：
            # 记下每个目标目录的导入前状态（不存在 = None），失败时整体撤销。
            staged_skills = stage / "skills"
            if staged_skills.is_dir():
                from ...skills.installer import _skill_target

                skills_root.mkdir(parents=True, exist_ok=True)
                try:
                    for src_dir in sorted(p for p in staged_skills.iterdir() if p.is_dir()):
                        try:
                            dst = _skill_target(skills_root, src_dir.name)
                        except SkillInstallError as e:
                            raise RuntimeError(
                                f"包里的技能名不合法（{src_dir.name}）：{e}"
                            ) from None
                        if dst.is_dir():
                            # 原本就存在的技能先完整备份，回滚时整体换回（不能只
                            # 删新增文件——copytree(dirs_exist_ok=True) 是合并写入）
                            skills_overwritten.append(src_dir.name)
                            backup = skills_backup_root / src_dir.name
                            backup.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copytree(dst, backup, dirs_exist_ok=True)
                            skills_backup[src_dir.name] = backup
                        shutil.copytree(src_dir, dst, dirs_exist_ok=True)
                        skills_installed.append(src_dir.name)
                except Exception as e:  # noqa: BLE001
                    failed = await _undo_all([])
                    raise RuntimeError(
                        "技能恢复失败，已回滚"
                        + (f"（残留未还原：{'、'.join(failed)}）" if failed else "")
                        + f"：{e}"
                    ) from None

            # 4) 热重载：配置 → 档位 → 技能/系统提示词 → 子代理 → MCP。
            # config 加载失败说明本机路径等环境与包不兼容，同样整体回滚
            try:
                self.cfg = load_config()
            except Exception as e:  # noqa: BLE001
                failed = await _undo_all([])
                raise RuntimeError(
                    "导入后配置加载失败（本机环境与导出包可能不兼容），已回滚"
                    + (f"（残留未还原：{'、'.join(failed)}）" if failed else "")
                    + f"：{e}"
                ) from None
            if "ui.json" in cfg_restored:
                self._apply_gate_accept_pref(self._read_ui_prefs().get("accept_edits", 0))
            if skills_installed:
                self.skills.discover()
                self._refresh_system_prompts()
            if "subagents.json" in cfg_restored:
                try:
                    self.subagent_store.load()
                except Exception as e:  # noqa: BLE001 - 子代理清单读不进来不拦导入
                    logger.warning("导入后重建子代理清单失败：%s", e)
            mcp_warnings: list[str] = []
            if "mcp.json" in cfg_restored:
                try:
                    await self._sync_mcp_changes()
                except Exception as e:  # noqa: BLE001 - MCP 重连失败提示重试/重启，不回滚
                    mcp_warnings.append(f"MCP 配置已恢复但重连失败（重启应用后生效）：{e}")

            # 5) 会话内存状态按新库重建（与备份恢复同一套）
            rebuilt = await self._rebuild_after_db_replace(reason="会话库已从导出包恢复")
            return {
                "db_restored": db_replaced,
                "config_restored": cfg_restored,
                "skills_installed": skills_installed,
                "skills_overwritten": skills_overwritten,
                "unknown": unknown,
                "safety_copy": str(safety) if safety else "",
                "session": rebuilt["session"],
                "projects": rebuilt["projects"],
                "warnings": mcp_warnings,
            }
        finally:
            shutil.rmtree(stage, ignore_errors=True)
