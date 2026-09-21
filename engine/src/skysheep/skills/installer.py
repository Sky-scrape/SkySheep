"""技能导入：把外部技能包（文件夹 / .zip）装进 ~/.skysheep/skills 或项目目录。

用户在设置页点「导入技能」时：
- 桌面窗口里有原生选择框，选完路径交给这里；
- 只用浏览器（或打包后原生框不可用）时，也可以直接粘贴一个本机路径。

安装结果必须是「一个技能一个文件夹，里面带 SKILL.md」——所以这里做三件事：
1. 找到包里的技能根（用户可能选中了外层目录，或 zip 里套了一层）；
2. 校验 SKILL.md 存在且能解析出名称，且不与已有技能重名（重名不覆盖，报错让用户先改名）；
3. 复制/解压进去。zip 解压逐条目检查路径，挡住 ../ 穿越与绝对路径。
"""

from __future__ import annotations

import os
import shutil
import stat
import time
import zipfile
from pathlib import Path

from .loader import _load_skill_from_dir  # 与发现逻辑共用同一套 SKILL.md 解析

SKILL_FILE = "SKILL.md"
MAX_SKILL_FILES = 2_000          # 单个技能包的文件数上限（防误选整个盘）
MAX_UNPACKED_BYTES = 120 * 1024 * 1024


class SkillInstallError(Exception):
    pass


def _scan_skills_in_dir(base: Path, max_depth: int = 3) -> list[Path]:
    """找出 base 下方（含 base 自己）带 SKILL.md 的技能目录。

    最多往下钻 max_depth 层——GitHub 仓库包常见三层套法 repo-main/skills/xxx，
    再深的基本不是用户想要的东西。找到技能目录后不再往它内部下钻，
    避免技能自带的 examples 又被当成独立技能重复导入。
    """
    if (base / SKILL_FILE).is_file():
        return [base]
    found: list[Path] = []
    base_depth = len(base.parts)
    for dirpath, dirnames, _filenames in os.walk(base):
        if len(Path(dirpath).parts) - base_depth >= max_depth:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for name in list(dirnames):  # 快照迭代：命中后要从 dirnames 里移除防下钻
            candidate = Path(dirpath) / name
            if (candidate / SKILL_FILE).is_file():
                found.append(candidate)
                dirnames.remove(name)  # 技能目录内部不下钻
    return sorted(found)


def _skill_name_of(path: Path) -> str:
    skill = _load_skill_from_dir(path, "global")
    if skill is None:
        raise SkillInstallError(f"「{path.name}」里没有可用的 {SKILL_FILE}")
    return skill.name


def _ensure_free(names: list[str], existing: set[str]) -> None:
    clash = [n for n in names if n in existing]
    if clash:
        raise SkillInstallError(
            "已存在同名技能：" + "、".join(clash) + "；请先删除或重命名后再导入"
        )


def install_from_dir(src: str | Path, dest_root: Path, *, existing: set[str]) -> dict:
    """把本机文件夹里的技能包复制到 dest_root。"""
    src_path = Path(src).expanduser()
    if not src_path.exists():
        raise SkillInstallError("路径不存在：" + str(src_path))
    if not src_path.is_dir():
        raise SkillInstallError("请选择技能文件夹（或 .zip 技能包）：" + str(src_path))

    roots = _scan_skills_in_dir(src_path)
    if not roots:
        raise SkillInstallError(
            f"在「{src_path}」里没找到技能：技能包必须是含 {SKILL_FILE} 的文件夹"
        )
    names = [_skill_name_of(r) for r in roots]
    _ensure_free(names, existing)

    dest_root.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    for root in roots:
        name = _skill_name_of(root)
        target = dest_root / name
        if target.exists():
            raise SkillInstallError(f"目标已存在：{target}")
        shutil.copytree(root, target)
        installed.append(name)
    return {"installed": installed, "count": len(installed), "dest": str(dest_root)}


# 别家 agent 工具的技能也装在 home 下的固定位置：「扫描本机技能」逐个探测，
# 找到即可勾选导入 SkySheep 复用（路径在调用时 expanduser，兼容 Windows/macOS/Linux）
LOCAL_SKILL_SOURCES: list[tuple[str, str]] = [
    ("Claude Code", "~/.claude/skills"),
    ("agents", "~/.agents/skills"),
    ("Codex", "~/.codex/skills"),
]


def scan_computer_skills(roots: list[tuple[str, Path]], existing: set[str]) -> list[dict]:
    """只读扫描 (来源, 目录) 清单，返回可导入的技能候选（不动磁盘上的任何文件）。

    同一技能只出一条：按名称去重——同名技能常同时装在多家 agent 的目录里，
    首个来源作为导入路径，其余来源并进 origin（如「Claude Code、Codex」）。
    解析不出名称的 SKILL.md 直接跳过。候选带 installed 标记（与已装技能同名）
    供前端默认排除——真正的重名把关仍由 install_from_dir 做，这里只负责预览。
    """
    out: list[dict] = []
    by_name: dict[str, dict] = {}
    seen_paths: set[str] = set()
    for label, base in roots:
        for p in _scan_skills_in_dir(Path(base)):
            key = str(p.resolve())
            if key in seen_paths:
                continue
            seen_paths.add(key)
            skill = _load_skill_from_dir(p, "global")
            if skill is None:
                continue
            hit = by_name.get(skill.name)
            if hit is not None:
                # 同名技能已在更早来源出现过：并进去处即可，不重复出条
                if label not in hit["origin"]:
                    hit["origin"] = f'{hit["origin"]}、{label}'
                continue
            entry = {
                "name": skill.name,
                "description": skill.description,
                "path": str(p),
                "origin": label,
                "installed": skill.name in existing,
            }
            by_name[skill.name] = entry
            out.append(entry)
    return out


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """过滤 zip 条目：挡绝对路径 / 上级目录穿越 / 符号链接 / 超量。"""
    members: list[zipfile.ZipInfo] = []
    total = 0
    for info in zf.infolist():
        raw = info.filename.replace("\\", "/")
        if not raw.strip():
            continue
        if raw.startswith("/") or ":" in raw.split("/")[0]:
            raise SkillInstallError("压缩包里含绝对路径条目：" + raw)
        if Path(raw).is_absolute() or os.pardir in Path(raw).parts:
            raise SkillInstallError("压缩包里含越界路径条目：" + raw)
        # 外部属性高位为符号链接（0xA000）时拒绝：解压出链接会绕过上面的检查
        if (info.external_attr >> 16) & 0xF000 == 0xA000:
            raise SkillInstallError("压缩包里含符号链接条目：" + raw)
        total += info.file_size
        if total > MAX_UNPACKED_BYTES:
            raise SkillInstallError("技能包解压后过大，已中止")
        members.append(info)
    if len(members) > MAX_SKILL_FILES:
        raise SkillInstallError("技能包文件数过多，已中止")
    return members


def _unpack_zip(zf: zipfile.ZipFile, members: list[zipfile.ZipInfo], staging: Path) -> None:
    """逐条目解压到 staging。

    条目名已在 _safe_members 里逐个筛过（无绝对路径、无上级目录穿越、无符号链接），
    这里再让 zipfile 自己清洗一次文件名，双重保险。
    """
    staging.mkdir(parents=True, exist_ok=True)
    for info in members:
        zf.extract(info, path=staging)


def _contains_seq(parts: tuple[str, ...], sub: tuple[str, ...]) -> bool:
    """sub 是否作为连续的一段出现在 parts 里（用来做「只装仓库里某个子目录」的过滤）。"""
    n = len(sub)
    if n == 0:
        return True
    return any(parts[i : i + n] == sub for i in range(len(parts) - n + 1))


def _no_match_hint(all_roots: list[Path], staging: Path, only_under: str) -> str:
    """链接里的子目录没匹配到技能时，给一份能照着改的提示。

    上游仓库改目录结构是常事（anthropics/skills 就把技能从 document-skills/ 移到了
    skills/），只说「没找到」用户无从下手。这里把包里实际发现的技能目录列出来，
    并剥掉压缩包外层那层仓库包装目录（GitHub 归档会套一层 repo-main/），
    让路径正好是用户在链接里该写的那一段。
    """
    rels = [r.relative_to(staging).parts for r in all_roots]
    if not rels:
        return f"链接指向的目录里没有找到技能（没有 {SKILL_FILE}）：{only_under}"
    # 所有技能共享同一层前缀时，那就是归档的包装目录，从展示路径里去掉
    firsts = {p[0] for p in rels}
    if len(firsts) == 1 and all(len(p) > 1 for p in rels):
        rels = [p[1:] for p in rels]
    found = ["/".join(p) for p in rels]
    shown = "、".join(found[:12]) + ("…" if len(found) > 12 else "")

    lines = [
        f"链接指向的目录里没有技能（没有 {SKILL_FILE}）：{only_under}",
        f"这个仓库里实际找到的技能目录是：{shown}",
    ]
    # 末段同名（如链接写的 docx、包里也有个 docx）→ 直接把该改成什么写出来
    wanted = only_under.replace("\\", "/").rstrip("/").split("/")[-1]
    same = [f for f in found if f.split("/")[-1] == wanted]
    if same:
        lines.append(f"链接里的目录多半过期了，把地址中的目录换成：{same[0]}")
    else:
        lines.append("多半是上游改了目录结构：把链接里的目录段换成上面其中之一，或直接粘贴仓库主页链接安装全部")
    return "\n".join(lines)


def install_from_zip(
    src: str | Path,
    dest_root: Path,
    *,
    existing: set[str],
    only_under: str | None = None,
) -> dict:
    """把 .zip 技能包解压到 dest_root（先解到暂存目录再改名，失败不留半个技能）。

    only_under：只安装压缩包里这个子目录下的技能（给「从 GitHub 链接装仓库里
    某个 skills/xxx 子目录」用）。压缩包外层通常套着一层 repo-main 之类的目录，
    所以这里按「路径里包含这段连续目录」来匹配，不锚定开头。
    """
    src_path = Path(src).expanduser()
    if not src_path.is_file():
        raise SkillInstallError("文件不存在：" + str(src_path))
    if not zipfile.is_zipfile(src_path):
        raise SkillInstallError("不是有效的 .zip 文件：" + str(src_path))

    dest_root.mkdir(parents=True, exist_ok=True)
    staging = dest_root / (".importing-" + src_path.stem)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    installed: list[str] = []
    try:
        with zipfile.ZipFile(src_path) as zf:
            _unpack_zip(zf, _safe_members(zf), staging)
        roots = _scan_skills_in_dir(staging)
        if only_under:
            sub = tuple(p for p in only_under.replace("\\", "/").split("/") if p)
            all_roots = list(roots)
            roots = [r for r in all_roots if _contains_seq(r.relative_to(staging).parts, sub)]
            if not roots:
                raise SkillInstallError(_no_match_hint(all_roots, staging, only_under))
        if not roots:
            raise SkillInstallError(f"压缩包里没找到技能：需要含 {SKILL_FILE} 的文件夹")
        names = [_skill_name_of(r) for r in roots]
        _ensure_free(names, existing)
        for root in roots:
            name = _skill_name_of(root)
            target = dest_root / name
            if target.exists():
                raise SkillInstallError(f"目标已存在：{target}")
            root.replace(target)
            installed.append(name)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"installed": installed, "count": len(installed), "dest": str(dest_root)}


def install(
    src: str | Path,
    dest_root: Path,
    *,
    existing: set[str],
) -> dict:
    """按来源类型分派：.zip 解压，其余按文件夹处理。"""
    src_path = Path(src).expanduser()
    if src_path.is_file() and src_path.suffix.lower() == ".zip":
        return install_from_zip(src_path, dest_root, existing=existing)
    return install_from_dir(src_path, dest_root, existing=existing)


def rmtree_force(path: Path) -> None:
    """整树删除，兜住 Windows 的两类删除失败：.git 的 pack/idx 文件带只读位
    （git 克隆安装的技能全中招，报 WinError 5 拒绝访问），杀软短暂锁住文件。
    先清只读位再删，权限错误时稍等重试一次。"""
    def clear_ro(p: Path) -> None:
        try:
            os.chmod(p, stat.S_IWRITE)
        except OSError:
            pass

    for attempt in (1, 2):
        try:
            for p in path.rglob("*"):
                clear_ro(p)
            clear_ro(path)
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 1:
                time.sleep(0.5)
            else:
                raise


def remove_skill(name: str, roots: list[Path]) -> dict:
    """删除一个技能目录（在给定的候选根目录里找）。"""
    for root in roots:
        target = root / name
        if target.is_dir() and (target / SKILL_FILE).is_file():
            rmtree_force(target)
            return {"removed": name, "from": str(root)}
    raise SkillInstallError("找不到技能目录：" + name)


# ---- 从网址安装（GitHub / Gitee 仓库页 / .zip 直链） ----
#
# 下载只允许落在已知代码托管域上（含 releases 直链与重定向后的对象存储域），
# 不对任意主机发请求；同时校验 DNS 解析结果不是内网/环回地址，重定向目标逐跳复核。

TRUSTED_ZIP_HOSTS = frozenset({
    "github.com",
    "www.github.com",
    "codeload.github.com",
    "objects.githubusercontent.com",
    "raw.githubusercontent.com",
    "gitee.com",
    "www.gitee.com",
})
USER_AGENT = "SkySheep/0.1 (skill import)"
DOWNLOAD_TIMEOUT_S = 30.0       # 单次 socket 读写的超时
GUESS_BRANCHES = ("main", "master")  # 链接没写分支时依次猜


class DownloadError(SkillInstallError):
    """下载失败；status 是 HTTP 状态码（分支猜测时 404 就换下一个候选地址）。"""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _host_allowed(url: str) -> str:
    """校验协议与主机白名单，返回主机名；不通过直接报错。"""
    scheme = url.split(":", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise SkillInstallError("网址要以 http:// 或 https:// 开头：" + url)
    after_scheme = url.split("://", 1)[1]
    host = after_scheme.split("/", 1)[0].split(":", 1)[0].lower()
    if host not in TRUSTED_ZIP_HOSTS:
        raise SkillInstallError(
            f"只支持 GitHub / Gitee 的下载链接（不支持主机 {host or '空'}）：" + url
        )
    return host


def _assert_public_dns(host: str) -> None:
    """防 DNS rebinding：托管域解析出内网/环回地址说明被劫持，中止下载。"""
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        raise SkillInstallError(f"解析不到 {host}：{e}") from e
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise SkillInstallError(f"{host} 解析到了内网地址（{ip}），已中止下载")


def _strict_opener():
    """带重定向校验的 opener：每一跳都要落在白名单主机上。"""
    import urllib.request

    class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            _host_allowed(newurl)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    return urllib.request.build_opener(_CheckedRedirect)


def resolve_url(url: str) -> tuple[list[str], str | None]:
    """把用户粘贴的链接解析成（候选 zip 直链列表, 只装某个子目录）。

    候选按优先级排列：链接写明了分支就只有一个；仓库主页链接给出 main/master
    两个猜测，下载时 404 换下一个。支持的形态：
        https://github.com/<用户>/<仓库>[/tree/<分支>[/<子目录>]]
        https://gitee.com/<用户>/<仓库>[/tree/<分支>[/<子目录>]]
        GitHub / Gitee 上的 .zip 直链（含 releases 下载地址）
    """
    url = (url or "").strip()
    if not url:
        raise SkillInstallError("请先粘贴要安装的网址")
    _host_allowed(url)  # 只放行托管域；协议/主机不对在这里就报错
    if url.split("?", 1)[0].lower().endswith(".zip"):
        return [url], None  # 已经是直链

    after_scheme = url.split("://", 1)[1]
    authority = after_scheme.split("/", 1)[0].lower()
    rest_path = after_scheme.split("/", 1)
    segments = [seg for seg in (rest_path[1] if len(rest_path) > 1 else "").split("/") if seg]

    is_github = authority in ("github.com", "www.github.com")
    if len(segments) < 2:
        site = "GitHub" if is_github else "Gitee"
        raise SkillInstallError(f"{site} 链接要像 https://{authority}/用户名/仓库：" + url)
    user, repo = segments[0], segments[1].removesuffix(".git")
    rest = segments[2:]
    if rest and rest[0] != "tree":
        site = "GitHub" if is_github else "Gitee"
        raise SkillInstallError(
            f"这种 {site} 页面装不了：请粘贴仓库主页、仓库里的 tree 页面，或 .zip 下载链接"
        )

    if is_github:
        def archive(branch: str) -> str:
            return f"https://github.com/{user}/{repo}/archive/refs/heads/{branch}.zip"
    else:
        # Gitee 归档直链格式：/repository/archive/<分支>.zip
        def archive(branch: str) -> str:
            return f"https://gitee.com/{user}/{repo}/repository/archive/{branch}.zip"

    if rest:  # tree/<分支>[/<子目录>]
        branch = rest[1]
        sub = "/".join(rest[2:]) or None
        return [archive(branch)], sub
    return [archive(b) for b in GUESS_BRANCHES], None


def download_to(url: str) -> Path:
    """下载到本机临时文件并返回路径（分块写、设上限、重定向逐跳校验）。

    临时文件由 tempfile 创建（名字不可预测、从不手工拼路径），调用方用完负责删除；
    任何失败都会把半截文件清掉。
    """
    import tempfile
    import urllib.error
    import urllib.request

    host = _host_allowed(url)
    if host not in ("127.0.0.1", "localhost"):  # 字面本机仅供测试桩使用
        _assert_public_dns(host)

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp: Path | None = None
    try:
        with _strict_opener().open(request, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            if getattr(resp, "status", 200) != 200:
                raise DownloadError(f"下载失败（HTTP {resp.status}）：{url}", resp.status)
            total = 0
            with tempfile.NamedTemporaryFile(
                prefix="skysheep-skill-", suffix=".zip", delete=False
            ) as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UNPACKED_BYTES:
                        raise SkillInstallError(
                            f"文件超过 {MAX_UNPACKED_BYTES // (1024 * 1024)} MB，已中止下载"
                            "（如确需安装请手动下载后从本地导入）"
                        )
                    fh.write(chunk)
                tmp = Path(fh.name)
    except Exception as e:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
        if isinstance(e, urllib.error.HTTPError):
            raise DownloadError(f"下载失败（HTTP {e.code}）：{url}", e.code) from e
        if isinstance(e, urllib.error.URLError):
            raise SkillInstallError(
                f"下载失败：{e.reason}。如果是 GitHub 链接，可能需要先开系统代理；"
                "也可以手动下载 .zip 后用「选择…」从本地导入"
            ) from e
        if isinstance(e, SkillInstallError):
            raise
        raise SkillInstallError("下载失败：" + str(e)) from e
    if tmp is None:
        raise SkillInstallError("下载失败：临时文件创建失败")
    return tmp


def install_from_url(url: str, dest_root: Path, *, existing: set[str]) -> dict:
    """下载并安装。分支没写明时依次试候选地址，404 换下一个。"""
    candidates, only_under = resolve_url(url)
    last: DownloadError | None = None
    for candidate in candidates:
        try:
            tmp = download_to(candidate)
        except DownloadError as e:
            last = e
            if e.status == 404:
                continue  # 分支猜测：这个分支不存在，试下一个
            raise SkillInstallError(str(e)) from e
        try:
            return install_from_zip(tmp, dest_root, existing=existing, only_under=only_under)
        finally:
            tmp.unlink(missing_ok=True)
    tried = "、".join(candidates)
    detail = f"最后错误：{last}" if last else ""
    raise SkillInstallError(
        f"下载失败：这些地址都不存在（{tried}）。{detail} 请确认仓库存在、不是私有仓库"
    )
