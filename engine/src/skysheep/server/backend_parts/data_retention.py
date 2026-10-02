"""数据保留策略：只进不出目录的过期清理（截图副本 / 隔离区 / 子代理报告）。

三类文件「只写不删」，随使用无声累积（写入点就是治理对象，别处不再新增类别）：

- 截图副本 ``<数据目录>/screenshots/*.png``——tools/computer.py 每次截屏存一份，
  已有 KEEP_SCREENSHOTS=60 的条数淘汰，但没有按时间的保留；
- 隔离区 ``<数据目录>/quarantine/<日期>/<sha256>.txt``——tools/web.py 的
  web_fetch 隔离模式把抓取正文全文落盘，没有淘汰；
- 子代理报告 ``<项目>/.skysheep/reports/<task_id>-<type>.md``——core/subagent.py
  超内联阈值的长报告落盘，没有淘汰。

本模块按 ``[retention]`` 配置的天数清理过期文件（默认 30 天，0 = 该类关闭）：
只删这三类**已知目录**里引擎自己写入形状的文件（截图 .png、隔离区日期子目录
下的 .txt、报告 .md），不递归、不碰目录外任何东西；目录不存在静默跳过。
备份（BACKUP_KEEP 滚动）与日志（轮转）各有自己的保留机制，不归这里管。

状态（上次清理日期）存引擎自有状态文件 ``<数据目录>/retention.json``（原子写），
是「当天已跑过就跳过」的防重键。巡检在后台线程跑文件扫描，应用启动后延迟
一轮、之后每小时看一眼是否跨天（到点每天至多清一次），不阻塞启动。
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from ...config import (
    ConfigError,
    load_config,
    set_retention_in_config,
    skysheep_home,
)
from ...instance import data_home
from ...obs import info as obs_info
from ...textio import write_text_atomic

STATE_FILE = "retention.json"

# 各类默认保留天数与上限（[retention] 的 schema 默认值在 config.RetentionConfig，
# 这里是配置读不出来时的兜底口径，两处保持一致）
DEFAULT_DAYS = {"screenshots": 30, "quarantine": 30, "reports": 30}
DAYS_MAX = 3650

SECONDS_PER_DAY = 86400.0


@dataclass(frozen=True)
class RetentionCategory:
    """一类的目录形状：只认引擎自己写入的文件形状，别的什么都不碰。"""

    key: str
    label: str
    patterns: tuple[str, ...]  # 文件名通配（fnmatch）
    depth: int = 1  # 1 = root 下的文件；2 = root/<一层子目录>/ 下的文件
    # depth=2 时子目录名白名单（隔离区只认 YYYY-MM-DD 日期目录），
    # 用户自己丢进来的其它子目录不碰
    subdir_re: re.Pattern | None = None


CATEGORIES = (
    RetentionCategory("screenshots", "截图副本", ("*.png",)),
    RetentionCategory(
        "quarantine", "隔离区", ("*.txt",), depth=2,
        subdir_re=re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    ),
    RetentionCategory("reports", "子代理报告", ("*.md",)),
)

_CATEGORY_BY_KEY = {c.key: c for c in CATEGORIES}


def category_roots(cat: RetentionCategory, projects: list) -> list[Path]:
    """一类的清理根目录。报告按项目落盘，逐个已知项目取 ``.skysheep/reports``。"""
    if cat.key == "screenshots":
        return [skysheep_home() / "screenshots"]
    if cat.key == "quarantine":
        return [data_home() / "quarantine"]
    return [Path(p.root_path) / ".skysheep" / "reports" for p in projects]


def _iter_target_files(root: Path, cat: RetentionCategory):
    """列出一类目录下目标形状的文件；目录不可读 / 不存在都静默返回空。

    只在已知目录内按形状挑文件（含一层日期子目录），不做 os.walk 式的整树递归
    ——目录外与不认识的子目录里的东西一概不看。
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    if cat.depth == 1:
        for f in entries:
            if f.is_file() and _name_match(f.name, cat.patterns):
                yield f
        return
    for sub in entries:
        try:
            if not sub.is_dir():
                continue
        except OSError:
            continue
        if cat.subdir_re is not None and not cat.subdir_re.match(sub.name):
            continue
        try:
            inner = list(sub.iterdir())
        except OSError:
            continue
        for f in inner:
            if f.is_file() and _name_match(f.name, cat.patterns):
                yield f


def _name_match(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def sweep_all(days: dict[str, int], projects: list, *, now: float | None = None) -> dict:
    """按各类保留天数清一轮，返回 ``{类别 key: 结果}``（只对启动了的类别动手）。"""
    n = time.time() if now is None else float(now)
    out: dict[str, dict] = {}
    for cat in CATEGORIES:
        limit = _clamp_days(days.get(cat.key, DEFAULT_DAYS[cat.key]))
        res = {"key": cat.key, "label": cat.label, "days": limit, "deleted": 0, "bytes": 0}
        if limit > 0:
            cutoff = n - limit * SECONDS_PER_DAY
            for root in category_roots(cat, projects):
                if not root.is_dir():
                    continue  # 目录不存在静默跳过（没产生过这类文件的常态）
                for f in _iter_target_files(root, cat):
                    try:
                        st = f.stat()
                    except OSError:
                        continue
                    if st.st_mtime >= cutoff:
                        continue
                    try:
                        f.unlink()
                    except OSError:
                        continue  # 被占用（杀软扫描中很常见）就留给下一轮
                    res["deleted"] += 1
                    res["bytes"] += st.st_size
        out[cat.key] = res
    return out


def usage_all(projects: list) -> dict[str, dict]:
    """各类目录当前占用（文件数 + 字节数，不删除）：设置页展示用。"""
    out: dict[str, dict] = {}
    for cat in CATEGORIES:
        files = size = 0
        for root in category_roots(cat, projects):
            if not root.is_dir():
                continue
            for f in _iter_target_files(root, cat):
                try:
                    files += 1
                    size += f.stat().st_size
                except OSError:
                    continue
        out[cat.key] = {"files": files, "bytes": size, "label": cat.label}
    return out


# ---- 保留天数解析：配置读不出来时兜底默认，绝不让巡检炸掉 ----


def _clamp_days(val) -> int:
    try:
        n = int(val)
    except (TypeError, ValueError):
        return -1  # 调用方按「非法回落该类默认」处理
    return min(DAYS_MAX, max(0, n))


def days_from(cfg) -> dict[str, int]:
    """从 SkySheepConfig 取各类保留天数；缺配置/缺字段回落默认。"""
    out = dict(DEFAULT_DAYS)
    section = getattr(cfg, "retention", None)
    if section is None:
        return out
    for key in out:
        val = getattr(section, f"{key}_days", None)
        if isinstance(val, bool) or not isinstance(val, int):
            continue  # pydantic 校验后不该发生，兜底保持默认
        out[key] = min(DAYS_MAX, max(0, val))
    return out


def resolve_days(cfg=None) -> dict[str, int]:
    """各类保留天数：显式配置优先；没给配置就现读 config.toml。

    配置损坏（手改坏的 TOML / 小节值类型写错 → ConfigError）时按默认天数兜底
    ——清理只针对已知目录里的旧文件，兜底口径是安全的，不该让巡检跟着配置炸。
    """
    if cfg is not None:
        return days_from(cfg)
    try:
        return days_from(load_config())
    except Exception:  # noqa: BLE001 - ConfigError 等：按默认清理，不终止巡检
        return dict(DEFAULT_DAYS)


# ---- 防重状态：当天已清过就跳过（引擎自有状态文件，原子写） ----


def state_path() -> Path:
    return skysheep_home() / STATE_FILE


def default_state() -> dict:
    return {"last_sweep_date": ""}


def load_state() -> dict:
    """读防重状态；缺文件 / 坏 JSON / 字段缺失都回落默认（当作还没清过）。"""
    try:
        raw = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default_state()
    if not isinstance(raw, dict):
        return default_state()
    return {"last_sweep_date": str(raw.get("last_sweep_date") or "")}


def save_state(state: dict) -> None:
    """原子写防重状态；未知字段不落盘（写出去的形状永远是自己读得回来的）。"""
    clean = default_state()
    clean["last_sweep_date"] = str(state.get("last_sweep_date") or "")
    write_text_atomic(state_path(), json.dumps(clean, ensure_ascii=False, indent=2) + "\n")


def today_str(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def _log_sweep(results: dict) -> None:
    """清理结果逐类记一条结构化日志（删了几个、多大了）：只记度量不记路径清单。"""
    for res in results.values():
        if res["days"] <= 0:
            continue  # 关闭了的类别没动手，不产生日志噪音
        obs_info(
            "retention_sweep", f"保留策略清理 · {res['label']}",
            key=res["key"], days=res["days"], deleted=res["deleted"], bytes=res["bytes"],
        )


async def run_retention_pass(store, cfg=None, *, now: float | None = None, force: bool = False) -> dict:
    """保留策略的一轮巡检：当天已清过就跳过；force=True 忽略防重（「立即清理」用）。

    返回 ``{"ran": bool, ...}``；ran=True 附 results（各类清理量）、deleted/bytes
    合计与 usage（清理后的最新占用）。防重日期**写前重读合并**：并发期间的
    状态改动不被覆盖（与日报同姿态）。文件扫描与删除在线程里跑，不卡事件循环。
    """
    n = time.time() if now is None else float(now)
    today = today_str(n)
    if not force and load_state().get("last_sweep_date") == today:
        return {"ran": False, "reason": "already_ran", "today": today}
    projects = await store.list_projects() if store is not None else []
    days = resolve_days(cfg)
    results = await asyncio.to_thread(sweep_all, days, projects, now=n)
    fresh = load_state()
    fresh["last_sweep_date"] = today
    save_state(fresh)
    _log_sweep(results)
    usage = await asyncio.to_thread(usage_all, projects)
    return {
        "ran": True, "today": today, "results": results,
        "deleted": sum(r["deleted"] for r in results.values()),
        "bytes": sum(r["bytes"] for r in results.values()),
        "usage": usage,
    }


class RetentionMixin:
    """数据保留策略：后台巡检循环与设置页读写（WS 方法由接线阶段挂 dispatch）。"""

    # 启动后延迟首扫：避开启动高峰（桌面壳/服务就绪都在抢时间），也不影响启动速度；
    # 之后每小时看一眼是否跨天——防重状态保证每天至多真清一次
    RETENTION_FIRST_DELAY_S = 45.0
    RETENTION_CHECK_INTERVAL_S = 3600.0

    def start_retention_loop(self) -> None:
        self._retention_task = asyncio.create_task(self._retention_loop())

    def stop_retention_loop(self) -> None:
        if getattr(self, "_retention_task", None):
            self._retention_task.cancel()
            self._retention_task = None

    async def _retention_loop(self) -> None:
        """延迟首扫 + 逐时跨天检查：单轮失败不终止循环（与日报/扫描循环同一姿态）。"""
        await asyncio.sleep(self.RETENTION_FIRST_DELAY_S)
        while True:
            try:
                await run_retention_pass(self.store, self.cfg)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(self.RETENTION_CHECK_INTERVAL_S)

    async def retention_status(self) -> dict:
        """保留策略回读（设置页渲染用）：各类天数 + 当前占用 + 上次清理日期。"""
        projects = await self.store.list_projects() if self.store is not None else []
        usage = await asyncio.to_thread(usage_all, projects)
        return {
            "days": days_from(self.cfg),
            "defaults": dict(DEFAULT_DAYS),
            "usage": usage,
            "last_sweep_date": load_state()["last_sweep_date"],
        }

    async def retention_save(self, params: dict) -> dict:
        """保存各类保留天数（config.toml 的 [retention]）；非法值在 config 层报错。"""
        converted: dict[str, int | None] = {}
        for key, name in (
            ("screenshots_days", "截图副本"),
            ("quarantine_days", "隔离区"),
            ("reports_days", "子代理报告"),
        ):
            raw = params.get(key)
            if raw is None or raw == "":
                converted[key] = None
                continue
            if isinstance(raw, bool):
                raise RuntimeError(f"{name}的保留天数需要一个整数")
            try:
                converted[key] = int(raw)
            except (TypeError, ValueError):
                raise RuntimeError(f"{name}的保留天数需要一个整数") from None
        try:
            set_retention_in_config(
                screenshots_days=converted["screenshots_days"],
                quarantine_days=converted["quarantine_days"],
                reports_days=converted["reports_days"],
            )
        except ConfigError as e:
            raise RuntimeError(str(e)) from e
        self.cfg = load_config()  # 热生效：下一轮巡检按新天数清理
        return await self.retention_status()

    async def retention_sweep_now(self) -> dict:
        """「立即清理」：忽略当天防重马上清一轮，回报清理量与最新占用。"""
        return await run_retention_pass(self.store, self.cfg, force=True)
