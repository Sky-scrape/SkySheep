"""生成 Scoop bucket 的 manifest JSON 模板（docs/分发上架指南.md §2 的配套工具）。

以仓库实文为基准读三处，不手填：

- 版本：``engine/pyproject.toml`` 的 ``version``（发布基准）；
- 哈希：``engine/installer/SkySheep-<版本>-setup.exe.sha256``（发布清单 §4 的校验附件，
  兼容 ``<hex>`` 与 ``<hex>  <文件名>`` 两种写法——与 ``server/backend_parts/remote.py``
  的 ``_fetch_setup_sha256`` 同一宽容度）；附件缺失时退回对安装包本体现算 SHA-256，
  两者都没有则报错退出（先按发布清单 §3 打包）；
- 仓库地址：``engine/tools/installer.iss`` 的 ``MyAppURL``（拆 org/repo 拼下载 URL）。

用法（在 engine/ 目录）::

    uv run python tools/make_scoop_manifest.py                # 模板打到 stdout
    uv run python tools/make_scoop_manifest.py --out <路径>    # 另存为 UTF-8 文件

输出是「模板」：当版哈希已填好；``autoupdate`` 借 Release 的 .sha256 附件自动取新哈希。
启用签名后（分发上架指南 §3）重新生成，确保哈希取自签名后的产物。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

# 脚本位于 <repo>/engine/tools/：parents[0]=tools、parents[1]=engine、parents[2]=仓库根
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENGINE = _REPO_ROOT / "engine"

_DESCRIPTION = "开源桌面 AI Agent 工作台：Python 引擎 + 桌面壳 + 零构建前端"
_HEX64_RE = re.compile(r"^[a-fA-F0-9]{64}$")


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"读不了 {path}：{exc}") from exc
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{path} 不是合法 UTF-8：{exc}") from exc


def read_version() -> str:
    m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', _read_text(_ENGINE / "pyproject.toml"))
    if not m:
        raise SystemExit('engine/pyproject.toml 里解析不出 version（预期行首 version = "x.y.z"）')
    return m.group(1)


def read_repo_slug() -> str:
    text = _read_text(_ENGINE / "tools" / "installer.iss")
    m = re.search(r'(?m)^#define MyAppURL\s+"https://github\.com/([^/"]+)/([^/"]+)"', text)
    if not m:
        raise SystemExit("installer.iss 里解析不出 MyAppURL（预期 https://github.com/<org>/<repo>）")
    return f"{m.group(1)}/{m.group(2)}"


def read_sha256(version: str) -> str:
    name = f"SkySheep-{version}-setup.exe"
    attachment = _ENGINE / "installer" / f"{name}.sha256"
    if attachment.is_file():
        head = _read_text(attachment).split()[0]
        if not _HEX64_RE.match(head):
            raise SystemExit(
                f"{attachment} 首段不像 SHA-256（预期 64 位十六进制，可带 `  <文件名>` 尾巴）：{head}"
            )
        return head.lower()
    setup = _ENGINE / "installer" / name
    if setup.is_file():
        return hashlib.sha256(setup.read_bytes()).hexdigest()
    raise SystemExit(f"既没有 {attachment} 也没有 {setup}：先按发布清单 §3 打包出安装包")


def build_manifest(version: str, slug: str, sha256: str) -> str:
    repo = f"https://github.com/{slug}"
    setup_name = f"SkySheep-{version}-setup.exe"
    manifest = {
        "version": version,
        "description": _DESCRIPTION,
        "homepage": repo,
        "license": "MIT",
        "architecture": {
            "64bit": {
                "url": f"{repo}/releases/download/v{version}/{setup_name}",
                "hash": f"sha256:{sha256}",
            }
        },
        "innosetup": True,
        "shortcuts": [["SkySheep.exe", "SkySheep"]],
        "checkver": "github",
        "autoupdate": {
            "architecture": {
                "64bit": {
                    "url": f"{repo}/releases/download/v$version/SkySheep-$version-setup.exe",
                    "hash": {
                        "url": f"{repo}/releases/download/v$version/SkySheep-$version-setup.exe.sha256",
                        "find": "^([a-fA-F0-9]{64})",
                    },
                }
            }
        },
    }
    return json.dumps(manifest, ensure_ascii=False, indent=4) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成 Scoop bucket 的 manifest JSON 模板",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--out", type=Path, default=None, help="写入文件（缺省只打印到 stdout）")
    args = parser.parse_args()

    version = read_version()
    text = build_manifest(version, read_repo_slug(), read_sha256(version))
    if args.out is None:
        sys.stdout.write(text)
    else:
        args.out.write_text(text, encoding="utf-8", newline="\n")
        print(f"已写出 {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
