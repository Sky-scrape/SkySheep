"""系统级定时调度：把定时任务（cron_tasks）注册为 Windows 计划任务。

让定时任务不依赖 SkySheep 在运行：应用内 cron.add / cron.update / cron.delete
成功后（[cron] system_schedule = true 时，挂钩在 server/backend_parts/
automation.py），按任务行把 `SkySheepCron-<任务id>` 注册进 Windows 任务计划
程序，到点由系统直接拉起独立进程执行一次：

    "<python>" -m skysheep.cli.app cron-run <任务id> --project "<项目目录>"

执行体在 cli/app.py 的 `cron-run` 子命令，复用 `skysheep run` 既有机制
（HeadlessGate 无人值守门控、结果回写任务行、notify_channel 推送）。

本模块保持薄：纯映射函数 build_schtasks_args 不碰系统；register / unregister
只是 subprocess 调 schtasks 并返回 (成功与否, 输出)，全部可注入可测试。

注意：与后端里用户手动「导出为系统计划任务」（backend_parts/automation.py 的
`SkySheep-<任务id>`）是并行的两条注册通道，任务名前缀不同互不覆盖。
"""

from __future__ import annotations

import re
import subprocess
import sys

# 计划任务名前缀：与手动导出的 SkySheep-<id>（automation.py）区分开
SCHTASK_NAME_PREFIX = "SkySheepCron-"
SCHTASK_TIMEOUT_S = 30

# 任务行 weekday 0=周一..6=周日（与 compute_next_run / 前端一致）→ schtasks /D 三字母
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")

# schtasks 参数边界（本机 `schtasks /Create /?` 帮助原文）：MINUTE 1-1439、
# HOURLY 1-23、DAILY /MO 1-365（每 n 天）。越界值 schtasks 直接拒绝——注册
# 静默失败会让「无人值守覆盖」形同虚设（任务行显示正常、系统里什么都没
# 注册），映射与入口两侧都必须按这里能表达的范围拦。
MAX_MINUTE_INTERVAL = 1439
MAX_INTERVAL_HOURS = 23
MAX_INTERVAL_DAYS = 365


def schtask_name(task_id: int) -> str:
    return SCHTASK_NAME_PREFIX + str(int(task_id))


def validate_interval_minutes(minutes: int) -> str | None:
    """interval 分钟数映射不成 schtasks 参数时返回可读原因，可表达返回 None。

    可表达范围：1-1439 分钟（/SC MINUTE）、60 的倍数且 ≤23 小时（/SC HOURLY）、
    1440 的整倍数且 ≤365 天（/SC DAILY /MO n，每 n 天）。
    """
    m = int(minutes)
    if m < 1:
        return "间隔分钟数必须 ≥ 1"
    if m % 60 == 0 and m // 60 <= MAX_INTERVAL_HOURS:
        return None
    if m <= MAX_MINUTE_INTERVAL:
        return None
    if m % 1440 == 0 and m // 1440 <= MAX_INTERVAL_DAYS:
        return None
    return (
        f"间隔 {m} 分钟映射不成 Windows 计划任务的可表达粒度"
        f"（分钟 1-{MAX_MINUTE_INTERVAL}、整小时 1-{MAX_INTERVAL_HOURS} 小时、"
        f"整天 1-{MAX_INTERVAL_DAYS} 天），注册必被 schtasks 拒绝：请调整间隔"
    )


def normalize_time_of_day(raw: str) -> str:
    """time_of_day 规整成 schtasks /ST 要求的 HH:MM（24 小时制）；非法抛 ValueError。

    垃圾值若放行会一路进 /ST 被 schtasks 拒——注册静默失败，任务行却显示正常。
    """
    tod = str(raw or "").strip()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", tod)
    if not m:
        raise ValueError(
            f"time_of_day 必须是 HH:MM（24 小时制），收到的是「{tod or '(空)'}」"
        )
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        raise ValueError(f"time_of_day 超出 24 小时制范围：「{tod}」")
    return f"{h:02d}:{mi:02d}"


def build_schtasks_args(
    task_id: int, cron_spec: dict, project_dir: str, engine_python: str
) -> list[str]:
    """cron 任务行 → schtasks /Create 参数（纯函数，不碰系统）。

    cron_spec 取任务行字段（见 session/store.py 的 cron_tasks 表）：
    schedule_type（interval / daily / weekly）、interval_minutes、
    time_of_day（"HH:MM"）、weekday（0=周一..6=周日）。

    - interval：60 的整数倍且 ≤23 小时 → /SC HOURLY /MO n；1440 的整倍数
      （≥1 天）→ /SC DAILY /MO n（每 n 天，schtasks 原生支持 1-365）；其余
      ≤1439 分钟 → /SC MINUTE /MO n；再往外映射不出可表达参数，抛 ValueError
      ——MINUTE /MO 上限 1439、HOURLY /MO 上限 23（schtasks 帮助明文），越界
      值注册必被拒绝，宁可在映射层明示也不留静默失败
    - daily：/SC DAILY /ST <time_of_day>（缺省 09:00，与手动导出同口径）
    - weekly：/SC WEEKLY /D <三字母> /ST <time_of_day>；weekday 无效时抛
      ValueError——无人值守下静默降级 DAILY 会把频次放大 7 倍，调用方应先拦
    - time_of_day 只在 daily / weekly 分支消费，非法值（非 HH:MM）抛
      ValueError，不让垃圾值一路进 /ST 再被 schtasks 拒
    - /TR 是 `"<python>" -m skysheep.cli.app cron-run <id> --project "<dir>"`：
      解释器与项目目录带空格都靠内引号整体保护，schtasks 才能把带参命令
      完整存进任务
    - /F：同名任务已存在时覆盖（更新任务后按同一条入口重建）
    """
    args = ["/Create", "/F", "/TN", schtask_name(task_id)]
    stype = str(cron_spec.get("schedule_type") or "interval")
    tod = str(cron_spec.get("time_of_day") or "09:00")
    if stype == "interval":
        minutes = max(1, int(cron_spec.get("interval_minutes") or 1))
        problem = validate_interval_minutes(minutes)
        if problem:
            raise ValueError(problem)
        if minutes % 60 == 0 and minutes // 60 <= MAX_INTERVAL_HOURS:
            args += ["/SC", "HOURLY", "/MO", str(minutes // 60)]
        elif minutes % 1440 == 0:
            args += ["/SC", "DAILY", "/MO", str(minutes // 1440)]
        else:
            args += ["/SC", "MINUTE", "/MO", str(minutes)]
    elif stype == "weekly":
        raw_wd = cron_spec.get("weekday")
        wd = int(raw_wd) if raw_wd is not None else -1
        if not 0 <= wd <= 6:
            raise ValueError(
                "每周任务没有有效的星期几（weekday 0=周一..6=周日），"
                "无法注册为系统计划任务：请先在任务设置里选好星期几"
            )
        args += ["/SC", "WEEKLY", "/D", WEEKDAYS[wd], "/ST", normalize_time_of_day(tod)]
    else:  # daily（未知类型按 daily 兜底，与应用内 daily 语义最接近）
        args += ["/SC", "DAILY", "/ST", normalize_time_of_day(tod)]
    tr = (f'"{engine_python}" -m skysheep.cli.app cron-run {int(task_id)}'
          f' --project "{project_dir}"')
    args += ["/TR", tr]
    return args


def register(
    task_id: int, cron_spec: dict, project_dir: str, engine_python: str
) -> tuple[bool, str]:
    """注册（/F 覆盖式重建）系统计划任务。返回 (成功与否, 合并输出)。

    参数映射不合法（如每周任务没有星期几）时不调 schtasks，直接返回失败原因。
    """
    try:
        args = build_schtasks_args(task_id, cron_spec, project_dir, engine_python)
    except (TypeError, ValueError) as e:
        return False, str(e)
    return _run_schtasks(args)


def unregister(task_id: int) -> tuple[bool, str]:
    """移除系统计划任务（schtasks /Delete /F）。

    任务本就不存在时 schtasks 也返回非零：自动同步挂钩按日志处理即可，
    不影响任务行本身的删除。
    """
    return _run_schtasks(["/Delete", "/TN", schtask_name(task_id), "/F"])


def list_scheduled_ids() -> list[int]:
    """枚举本机已注册的 SkySheepCron-* 计划任务 id（启动对账用，best-effort）。

    `schtasks /Query /FO CSV /NH` 每行首个带引号字段是任务名（不随系统语言
    变化）。schtasks 不可用 / 超时 / 输出解析不出一律返回空表——对账只做
    注销（fail-safe 方向），漏掉一枚残留注册的代价远小于把启动流程打瘫。
    """
    ok, out = _run_schtasks(["/Query", "/FO", "CSV", "/NH"])
    if not ok:
        return []
    ids: list[int] = []
    for line in out.splitlines():
        m = re.match(r'"([^"]+)"', line.strip())
        if not m or not m.group(1).startswith(SCHTASK_NAME_PREFIX):
            continue
        suffix = m.group(1)[len(SCHTASK_NAME_PREFIX):]
        if suffix.isdigit():
            ids.append(int(suffix))
    return ids


def default_engine_python() -> tuple[str | None, str]:
    """cron-run 执行体用的解释器。返回 (路径, 不可用原因)。

    源码 / 虚拟环境：当前解释器（skysheep 包随环境可导入，`-m` 形式成立）。
    打包环境（PyInstaller）：`-m` 形式不成立，返回 (None, 原因)，由调用方
    跳过注册——绝不注册一条到点必失败的计划任务；打包环境的无人值守入口
    用任务卡片上手动「导出为系统计划任务」（ex e 入口是 SkySheep.exe 本体）。
    """
    if getattr(sys, "frozen", False):
        return None, ("打包环境不支持以 python -m 方式自动注册系统计划任务；"
                      "请在任务卡片使用「导出为系统计划任务」")
    return sys.executable, ""


def _run_schtasks(args: list[str]) -> tuple[bool, str]:
    """调一次 schtasks，返回 (退出码是否为 0, 合并输出)。

    GUI 进程（无控制台）拉起控制台子进程时 Windows 会给它新开终端窗口
    （Win11 默认是 Windows Terminal）——启动对账/导出每次都闪窗；带
    CREATE_NO_WINDOW 抑制（POSIX 无此参数，不传）。"""
    try:
        proc = subprocess.run(
            ["schtasks", *args], capture_output=True, timeout=SCHTASK_TIMEOUT_S,
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),  # noqa: S603 - exe 固定 schtasks
        )
    except FileNotFoundError as e:
        return False, f"schtasks 不可用：{e}"
    except subprocess.TimeoutExpired:
        return False, "schtasks 执行超时"
    raw = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
    return proc.returncode == 0, _decode(raw).strip()


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # 中文 Windows 上 schtasks 按控制台代码页（GBK）输出（与 automation.py 同款探测序）
        return raw.decode("gb18030", errors="replace")
