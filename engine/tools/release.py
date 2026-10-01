"""SkySheep 一键发版脚本——docs/发布清单.md §3/§4 的机械化。

用法（路径按本脚本自身定位、不依赖 cwd，仓库根或 engine/ 下均可直接跑）：

    python engine/tools/release.py                 # 本地全流程：依赖→打包→校验→冒烟→安装包→sha256
    python engine/tools/release.py --skip-pack     # 复用已有 dist，重做产物校验/冒烟/安装包
    python engine/tools/release.py --upload        # 全部阶段通过后 gh release create 上传（默认绝不传）

阶段与发布清单条目的对应（每步开头都会打印）：

    1  uv sync --locked                        §3-1
    2  PyInstaller 打包（不经 uv run）          §3-2
    3  产物校验（exe / 内置技能 / manifest）     §3-3
    4  冒烟（临时 SKYSHEEP_HOME 起 exe）        §3-3
    5  ISCC 编译安装包                          §3-4
    6  安装包 sha256 附件                       §4-5
    7  gh release create 上传                   §4-2/§4-3（仅 --upload）

安全取向：
- ``--upload`` 不给就绝不碰 gh；给了也在全部本地阶段通过后才执行，且
  ``gh release create`` 对已存在的 release 直接报错退出，不做任何覆盖重传
  （发布清单 §4「不 --clobber 重传」）。上传正文只是占位，提醒按清单把
  CHANGELOG 条目并回长句后再在 GitHub 上编辑。
- 冒烟细节：打包版是无窗口（console=False）程序，启动输出统一重定向到
  ``<SKYSHEEP_HOME>/logs/desktop.log``（desktop.py 的 _ensure_streams），
  端口从该日志的「服务已启动: http://127.0.0.1:<port>/」行解析；
  SKYSHEEP_HOME 指向一次性临时目录做隔离（不碰真实 ~/.skysheep），
  SKYSHEEP_INSTANCE=smoke 让单实例互斥体与 URL 协议注册都走独立身份，
  不与正在运行的真实实例抢锁。收割统一 ``taskkill /PID <pid> /T /F``。
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

# 脚本位于 <仓库根>/engine/tools/：parents[0]=tools、parents[1]=engine、parents[2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENGINE_DIR = _REPO_ROOT / "engine"
_DIST_DIR = _ENGINE_DIR / "dist" / "SkySheep"
_DIST_EXE = _DIST_DIR / "SkySheep.exe"
_INTERNAL_SKILLS = _DIST_DIR / "_internal" / "skysheep" / "skills"
_INSTALLER_DIR = _ENGINE_DIR / "installer"
# 本机 ISCC 安装位（发布清单 §3-4 的固定路径；没有它时用 --skip-installer 跳过安装包两步）
_ISCC = Path(r"D:\Inno Setup 7\ISCC.exe")

# skills-gallery/ 官方种子技能包数量（AGENTS.md：20 个中文场景技能）
_GALLERY_SKILL_COUNT = 20
# 版本号正则与 tools/check_versions.py 保持同款，避免两处各认一套写法
_PYPROJECT_VERSION_RE = re.compile(r'(?m)^version\s*=\s*"([^"]+)"')
_ISS_VERSION_RE = re.compile(r'(?m)^#define MyAppVersion\s+"([^"]+)"')
# 冒烟：desktop.log 里的服务地址行「服务已启动: http://127.0.0.1:<port>/」
_URL_IN_LOG_RE = re.compile(r"http://127\.0\.0\.1:(\d+)/")

# 冷启动预算：新 exe 首跑常有 Defender 全量扫描，宁多勿少；超时即判失败
SMOKE_READY_TIMEOUT_S = 180.0
SMOKE_LOG_POLL_S = 0.5


# ---------- 基础工具 ----------


def _force_utf8_stdio() -> None:
    """标准流重配为 UTF-8（与 tools/check_versions.py 同款）：中文 Windows 控制台
    默认按 GBK 编码 stdout，输出里的非 GBK 字符会让脚本半路崩掉。

    stdout 再开行缓冲：本脚本输出与 uv/pyinstaller/ISCC 子进程的透传输出交错，
    不开行缓冲的话（stdout 接管道时是块缓冲）脚本自己的进度行会迟到、与子
    进程输出错序。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (OSError, ValueError, TypeError):
            pass


def _read_text(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"读不了 {label}（{path}）：{exc}") from exc


def pyproject_version() -> str:
    """从 engine/pyproject.toml 读版本号（安装包文件名的最终依据）。"""
    text = _read_text(_ENGINE_DIR / "pyproject.toml", "engine/pyproject.toml")
    m = _PYPROJECT_VERSION_RE.search(text)
    if not m:
        raise RuntimeError('engine/pyproject.toml 里解析不出 version = "x.y.z"')
    return m.group(1)


def _installer_exe(version: str) -> Path:
    return _INSTALLER_DIR / f"SkySheep-{version}-setup.exe"


def _child_env() -> dict[str, str]:
    """子进程环境：去掉本进程可能继承到的 PYTHONHOME / PYTHONPATH。

    本脚本只依赖标准库，用哪个 python 跑都行（含 LibreOffice 等自带解释器——
    它们往往把 PYTHONHOME/PYTHONPATH 设进自己的进程环境，实测 LibreOffice 的
    python.exe 就是这样）；可这两个变量一旦被 .venv 里的子工具继承，venv 的
    stdlib 定位就被带偏（实测：pyinstaller.exe 直接 SRE module mismatch）。
    uv / pyinstaller / ISCC / taskkill / gh 都不需要这两样，剥掉最稳。
    """
    env = dict(os.environ)
    for var in ("PYTHONHOME", "PYTHONPATH"):
        env.pop(var, None)
    return env


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    """跑子命令并原样透传输出（失败带退出码抛错）。"""
    printable = " ".join(cmd)
    print(f"  $ {printable}")
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=_child_env())  # noqa: S603
    if proc.returncode != 0:
        raise RuntimeError(f"命令退出码 {proc.returncode}：{printable}")


# ---------- 各阶段实现 ----------


def step_sync() -> None:
    """清单 §3-1：uv sync --locked 对齐环境（pyproject 改了忘更 lock 在这里直接红）。"""
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("PATH 上找不到 uv（https://docs.astral.sh/uv/），无法同步依赖")
    _run([uv, "sync", "--locked"], cwd=_ENGINE_DIR)


def step_pack() -> None:
    """清单 §3-2：直接用 .venv 里的 pyinstaller 打包（不经 uv run，见发布清单 §3-2）。"""
    pyi = _ENGINE_DIR / ".venv" / "Scripts" / "pyinstaller.exe"
    if not pyi.is_file():
        raise RuntimeError(
            f"找不到 {pyi}（先完成依赖同步；pyinstaller 已入册 dev 组，uv sync 即装）"
        )
    _run([str(pyi), "--noconfirm", "--clean", "SkySheep.spec"], cwd=_ENGINE_DIR)


def step_verify_artifacts() -> None:
    """清单 §3-3（产物部分）：exe 在、20 个内置技能在、场景模板清单在。"""
    if not _DIST_EXE.is_file():
        raise RuntimeError(f"缺打包产物 {_DIST_EXE}")
    gallery = _INTERNAL_SKILLS / "gallery"
    if not gallery.is_dir():
        raise RuntimeError(f"缺内置技能目录 {gallery}（SkySheep.spec 的 gallery 收集没生效？）")
    count = sum(1 for p in gallery.rglob("SKILL.md") if p.is_file())
    if count != _GALLERY_SKILL_COUNT:
        raise RuntimeError(
            f"内置技能包数量不符：{gallery} 下 SKILL.md 共 {count} 个，"
            f"预期 {_GALLERY_SKILL_COUNT} 个（与仓库根 skills-gallery/ 对不上）"
        )
    manifest = _INTERNAL_SKILLS / "gallery_manifest.json"
    if not manifest.is_file():
        raise RuntimeError(f"缺场景模板清单 {manifest}")
    print(f"  exe、内置技能（{count} 个 SKILL.md）、gallery_manifest.json 均在位")


def _read_log(log_path: Path) -> str:
    try:
        return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _log_tail(log_path: Path, lines: int = 40) -> str:
    tail = "\n".join(_read_log(log_path).splitlines()[-lines:])
    return tail or "（日志还不存在或为空）"


def _wait_ready(proc: subprocess.Popen[bytes], log_path: Path) -> int:
    """轮询 desktop.log 直到出现服务地址行，返回端口；进程早退/超时即失败。"""
    deadline = time.monotonic() + SMOKE_READY_TIMEOUT_S
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            raise RuntimeError(
                f"SkySheep.exe 提前退出（退出码 {code}）；日志尾部：\n{_log_tail(log_path)}"
            )
        m = _URL_IN_LOG_RE.search(_read_log(log_path))
        if m:
            return int(m.group(1))
        time.sleep(SMOKE_LOG_POLL_S)
    raise RuntimeError(
        f"等待服务就绪超时（{SMOKE_READY_TIMEOUT_S:.0f}s）；日志尾部：\n{_log_tail(log_path)}"
    )


def _http_get_status(url: str) -> int:
    """GET 一发取状态码；禁用环境代理（回环地址不该被代理截走）。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=15) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:  # 非 2xx 也要把状态码带回给上层判断
        return int(exc.code)
    except OSError as exc:
        raise RuntimeError(f"GET {url} 失败：{exc}") from exc


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """taskkill /T 收割进程树（含 WebView2 子进程）；杀不掉只告警不掩盖原始错误。

    输出按字节捕获后丢弃：taskkill 打的是控制台 GBK 文本，按默认文本模式解码
    会在读管道线程里抛 UnicodeDecodeError（本地实测），字节模式无此问题。"""
    if proc.poll() is not None:
        return
    subprocess.run(  # noqa: S603
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        capture_output=True,
        check=False,
    )
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        print(
            f"  警告：taskkill /T /F 后 pid {proc.pid} 仍未退出，请到任务管理器确认",
            file=sys.stderr,
        )


def _rmtree_later(home: Path) -> None:
    """冒烟临时目录尽力清理：进程刚死文件可能还被占用，重试几次，删不掉留提示。"""
    for _ in range(3):
        if not home.exists():
            return
        shutil.rmtree(home, ignore_errors=True)
        if not home.exists():
            return
        time.sleep(1.0)
    print(f"  提示：冒烟临时目录未能完全删除（个别文件仍被占用，可手动删）：{home}", file=sys.stderr)


def step_smoke() -> None:
    """清单 §3-3（冒烟条）：临时 SKYSHEEP_HOME 起打包产物，GET / 应 200。"""
    home = Path(tempfile.mkdtemp(prefix="skysheep-release-smoke-"))
    env = _child_env()
    env["SKYSHEEP_HOME"] = str(home)  # 数据目录隔离，绝不指向真实 ~/.skysheep
    env["SKYSHEEP_INSTANCE"] = "smoke"  # 单实例互斥体/URL 协议走独立身份，不碰真实实例
    log_path = home / "logs" / "desktop.log"
    print(f"  SKYSHEEP_HOME={home}")
    print("  （无窗口版启动输出落在上述目录的 logs/desktop.log，等待服务就绪…）")
    proc = subprocess.Popen([str(_DIST_EXE)], cwd=str(_DIST_DIR), env=env)  # noqa: S603
    try:
        port = _wait_ready(proc, log_path)
        url = f"http://127.0.0.1:{port}/"
        status = _http_get_status(url)
        if status != 200:
            raise RuntimeError(f"GET {url} 返回 {status}，预期 200")
        print(f"  GET {url} → 200")
    finally:
        _kill_tree(proc)
        _rmtree_later(home)


def step_installer() -> None:
    """清单 §3-4：ISCC 编译安装包；开打前先校验 MyAppVersion 与 pyproject 一致（§1）。"""
    version = pyproject_version()
    iss_path = _ENGINE_DIR / "tools" / "installer.iss"
    m = _ISS_VERSION_RE.search(_read_text(iss_path, "engine/tools/installer.iss"))
    if not m:
        raise RuntimeError("engine/tools/installer.iss 里解析不出 #define MyAppVersion")
    if m.group(1) != version:
        raise RuntimeError(
            f"版本号漂移（发布清单 §1）：installer.iss 的 MyAppVersion={m.group(1)}，"
            f"pyproject 的 version={version}。先对齐版本号再打包，否则安装包文件名就是错的"
        )
    if not _ISCC.is_file():
        raise RuntimeError(f"找不到 ISCC：{_ISCC}（本机未装 Inno Setup？可用 --skip-installer 跳过）")
    _run([str(_ISCC), "tools/installer.iss"], cwd=_ENGINE_DIR)
    exe = _installer_exe(version)
    if not exe.is_file():
        raise RuntimeError(f"ISCC 已跑完但缺产物 {exe}")
    print(f"  产物：{exe}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def step_sha256() -> None:
    """清单 §4-5：生成 <安装包名>.sha256（应用内更新链路按它校验）。"""
    version = pyproject_version()
    exe = _installer_exe(version)
    if not exe.is_file():
        found = sorted(p.name for p in _INSTALLER_DIR.glob("SkySheep-*-setup.exe"))
        raise RuntimeError(
            f"缺安装包 {exe}（installer/ 现有：{', '.join(found) or '无'}）"
            "——多半是版本号不一致，见上方 ISCC 阶段的校验"
        )
    digest = _sha256(exe)
    sha_path = exe.with_name(exe.name + ".sha256")
    # 格式：<64位哈希>  <文件名>（两个空格；清单 §4-5 允许带文件名尾巴）
    sha_path.write_text(f"{digest}  {exe.name}\n", encoding="utf-8", newline="\n")
    print(f"  {sha_path.name}")
    print(f"  sha256 = {digest}")


def step_upload(allow: bool) -> None:
    """清单 §4-2/§4-3：gh release create 上传安装包 + sha256。默认（不给 --upload）绝不执行。"""
    version = pyproject_version()
    if not allow:
        print("  （未指定 --upload：不上传——本脚本默认绝不执行对外发布动作）")
        return
    exe = _installer_exe(version)
    sha = exe.with_name(exe.name + ".sha256")
    missing = [str(p) for p in (exe, sha) if not p.is_file()]
    if missing:
        raise RuntimeError("缺上传资产：" + ", ".join(missing))
    gh = shutil.which("gh")
    if not gh:
        raise RuntimeError("PATH 上找不到 gh（GitHub CLI）；不装也能完成其余全部本地阶段")
    print(f"  !! --upload 已指定：即将 gh release create v{version} 上传 2 个资产（发布清单 §4）")
    print("  !! 提醒：确认版本号四处一致已过、非同日重发；资产已存在时 gh 会报错，本脚本不做 --clobber 覆盖")
    _run(
        [
            gh,
            "release",
            "create",
            f"v{version}",
            str(exe),
            str(sha),
            "--title",
            f"SkySheep v{version}",
            "--notes",
            f"SkySheep v{version}，变更内容见仓库 CHANGELOG.md 对应条目。"
            "（发布清单 §4：Release 正文请把 CHANGELOG 条目的手动换行并回长句后编辑，"
            "并在末尾贴一份可读 sha256 哈希。）",
        ]
    )


# ---------- 阶段表与主流程 ----------


@dataclass(frozen=True)
class Step:
    ref: str  # 对应发布清单条目编号（每步开头打印）
    title: str
    fn: Callable[[], None]
    skip_flag: str | None = None  # 对应 args 属性名；None = 没有跳过开关


def build_steps(args: argparse.Namespace) -> list[Step]:
    return [
        Step("§3-1", "依赖同步 uv sync --locked", step_sync),
        Step("§3-2", "PyInstaller 打包（不经 uv run）", step_pack, "skip_pack"),
        Step("§3-3", "产物校验（exe / 内置技能 / manifest）", step_verify_artifacts),
        Step("§3-3", "冒烟（临时 SKYSHEEP_HOME 起 exe，GET / 应 200）", step_smoke, "skip_smoke"),
        Step("§3-4", "ISCC 编译安装包", step_installer, "skip_installer"),
        Step("§4-5", "安装包 sha256 附件", step_sha256, "skip_installer"),
        Step("§4-2/§4-3", "gh release create 上传（仅 --upload）", lambda: step_upload(args.upload)),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="release.py",
        description=(
            "SkySheep 一键发版脚本：发布清单 §3/§4 的机械化"
            "（依赖同步→打包→产物校验→冒烟→ISCC 安装包→sha256）"
        ),
        epilog=(
            "默认绝不执行任何对外发布动作；只有显式 --upload 才会在全部阶段通过后\n"
            "调用 gh release create v<版本> 上传安装包与 .sha256（发布清单 §4-2/§4-3）。\n"
            "阶段对应清单条目：1=§3-1 2=§3-2 3=§3-3 4=§3-3 5=§3-4 6=§4-5 7=上传（仅 --upload）。\n"
            "失败即非零退出，并打印已到达的步骤。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--skip-pack",
        action="store_true",
        help="跳过 PyInstaller 打包（§3-2）；产物校验与冒烟仍会执行，可复用已有 dist",
    )
    parser.add_argument(
        "--skip-smoke",
        action="store_true",
        help="跳过打包产物冒烟（§3-3 的冒烟条；会短暂弹出应用窗口并自动收割）",
    )
    parser.add_argument(
        "--skip-installer",
        action="store_true",
        help="跳过 ISCC 安装包与 sha256 附件两步（§3-4/§4-5）",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="全部阶段通过后 gh release create 上传安装包+sha256（§4）；不给则绝不发布",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = parse_args(argv)
    version = pyproject_version()
    print(f"SkySheep 发版脚本 · 版本 {version} · 仓库根 {_REPO_ROOT}")
    if args.upload:
        print("已指定 --upload：全部阶段通过后将执行 gh release create（发布清单 §4）。")
    else:
        print("未指定 --upload：本次只做本地构建与校验，绝不执行上传/发布动作。")

    steps = build_steps(args)
    done: list[str] = []
    for i, step in enumerate(steps, 1):
        head = f"[{i}/{len(steps)}] [清单{step.ref}] {step.title}"
        if step.skip_flag and getattr(args, step.skip_flag):
            print(f"{head} —— 跳过（--{step.skip_flag.replace('_', '-')}）")
            continue
        print(head)
        t0 = time.monotonic()
        try:
            step.fn()
        except Exception as exc:  # noqa: BLE001 —— 统一失败出口：非零退出并打印已到达的步骤
            print(f"\n失败于 {head}：{exc}", file=sys.stderr)
            print(f"\n已完成的步骤：{'、'.join(done) if done else '（无）'}", file=sys.stderr)
            print(f"已到达的步骤：[{i}/{len(steps)}] {step.title}（未通过）", file=sys.stderr)
            return 1
        done.append(f"[{i}] {step.title}")
        print(f"  OK（{time.monotonic() - t0:.1f}s）")

    skipped = [
        f"[{i}] {s.title}"
        for i, s in enumerate(steps, 1)
        if s.skip_flag and getattr(args, s.skip_flag)
    ]
    print(f"\n全部 {len(done)} 个已启用阶段通过。跳过：{'、'.join(skipped) if skipped else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
