"""凭据加密落盘（Windows DPAPI）：config.toml 里的密钥不再明文存放。

背景：config.toml 里的模型服务 API Key 与渠道凭据此前是明文（SECURITY.md 的
已知取舍「凭据明文落盘」）——本机任何能读用户目录的程序都能直接拿走。DPAPI
（Data Protection API，crypt32.dll）按当前 Windows 账户派生加密密钥，落盘的
是密文；文件被拷到别的机器或别的 Windows 账户下解不开。

保护级别与理由（CryptProtectData 的两个关键选择）：

- **按用户加密（默认），不用 CRYPTPROTECT_LOCAL_MACHINE**：SkySheep 是单用户
  桌面应用，凭据属于「这个 Windows 账户」而不是「这台机器」；按机器加密会让
  同机其他账户（乃至以 SYSTEM 运行的服务进程）也能解开，保护面反而更宽。
  代价是跨账户/跨机器迁移读不开——预期行为，解不开的字段按「未配置」处理
  并记日志，用户重填即可（见 decrypt_value）。
- **CRYPTPROTECT_UI_FORBIDDEN**：加解密发生在配置读写路径上（启动加载、保存
  设置、渠道后台落盘），全是无界面上下文；不带这个标志 DPAPI 可能弹系统级
  确认框，卡住 asyncio 事件循环。

值格式：``dpapi:<base64>`` 前缀标识已加密。前缀让「密文」与「真 Key」可区分：
旧版本 / 非 Windows 路径读到带前缀的值不会把 base64 当成 Key 静默用错，
而是走「解不开 → 未配置」的显式路径。

平台边界：非 Windows（Linux/macOS 的开发与兜底路径）没有 DPAPI，
encrypt_value 原样返回明文、config.toml 保持明文现状（跨平台取舍见
SECURITY.md）。crypt32 导入或调用失败一律不抛回配置层：加密失败保留明文
并记日志（与升级前行为一致），解密失败按「字段为空」处理——宁可少一个
可重填的 Key，绝不把密文本体当成 Key 静默用错。纯标准库 ctypes 直连，
零新依赖。

加密字段清单（以 config.py 实际 schema 为准，config.py 按此遍历读写）：

- providers.<名>.api_key —— 各模型服务的 API Key
- websearch / imagegen / speech 的 api_key —— 联网搜索 / AI 画图 / 语音转写
- server.token —— 局域网 / Tailscale 访问令牌
- channels.platforms.<平台> 内凭据类字段（app_secret / secret / bot_token 等，
  平台 dict 是开放集合，按键名子串识别）

对照 support.py 的 SECRET_KEYS（诊断包打码用，按「键名包含」宁可多打）：
这里只加密真正的凭据**值**——env_key 是环境变量**名**、proxy / base_url /
url 是地址（webhook 地址里可能内嵌 token，保留明文便于排障，与 SECRET_KEYS
不等同是刻意的）。
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import logging
import sys

logger = logging.getLogger("skysheep.secure_store")

# 已加密值的标识前缀；出现在 config.toml 里形如 dpapi:AQAAANCMnd8...
DPAPI_PREFIX = "dpapi:"

# providers 各实现的 api_key 是同一个字段名（ProviderConfig 唯一凭据字段）
PROVIDER_SECRET_FIELDS: tuple[str, ...] = ("api_key",)

# 顶层小节 → 该小节内的凭据字段（URL / 主机 / 模型名等保持明文，便于排障）
SECRET_SECTION_FIELDS: dict[str, tuple[str, ...]] = {
    "websearch": ("api_key",),
    "imagegen": ("api_key",),
    "speech": ("api_key",),
    "server": ("token",),  # 局域网 / Tailscale 访问令牌
}

# 渠道平台 dict（开放集合）内凭据字段的子串识别：
# 已知命中 app_secret / secret / bot_token / ilink_bot_token；
# app_id / url / base_url / cursor / allowed_ids / allowed_tools / cli_path
# 等标识与地址不命中，保持明文。
CHANNEL_SECRET_SUBSTRINGS: tuple[str, ...] = (
    "secret", "token", "password", "passwd", "credential", "private",
)

# 不弹系统级 UI（理由见模块 docstring）；也不按机器加密（默认按当前用户）
_CRYPTPROTECT_UI_FORBIDDEN = 0x1


def is_channel_secret_key(key: str) -> bool:
    """渠道平台 dict 里的这个键名是否为凭据字段。"""
    lowered = str(key or "").lower()
    return any(s in lowered for s in CHANNEL_SECRET_SUBSTRINGS)


def is_encrypted(value: str) -> bool:
    """该值是否带 dpapi: 前缀（已加密形态）。"""
    return isinstance(value, str) and value.startswith(DPAPI_PREFIX)


# ---- ctypes 绑定（懒加载；加载失败按「DPAPI 不可用」走明文路径） ----

class _DATA_BLOB(ctypes.Structure):
    """crypt32 的 CRYPTPROTECT_DATA BLOB（CRYPTOAPI_BLOB）。"""

    _fields_ = [
        ("cbData", ctypes.c_ulong),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


_crypt32 = None
_kernel32 = None


def _load_crypto_dlls() -> tuple[ctypes.CDLL, ctypes.CDLL]:
    global _crypt32, _kernel32
    if _crypt32 is None or _kernel32 is None:
        crypt32 = ctypes.WinDLL("crypt32")
        kernel32 = ctypes.WinDLL("kernel32")
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),      # pDataIn
            ctypes.c_wchar_p,                # szDataDescr
            ctypes.POINTER(_DATA_BLOB),      # pOptionalEntropy
            ctypes.c_void_p,                 # pvReserved
            ctypes.c_void_p,                 # pPromptStruct（UI_FORBIDDEN 下恒 None）
            ctypes.c_ulong,                  # dwFlags
            ctypes.POINTER(_DATA_BLOB),      # pDataOut
        ]
        crypt32.CryptProtectData.restype = ctypes.c_int  # BOOL
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),      # pDataIn
            ctypes.POINTER(ctypes.c_wchar_p),  # ppszDataDescr
            ctypes.POINTER(_DATA_BLOB),      # pOptionalEntropy
            ctypes.c_void_p,                 # pvReserved
            ctypes.c_void_p,                 # pPromptStruct
            ctypes.c_ulong,                  # dwFlags
            ctypes.POINTER(_DATA_BLOB),      # pDataOut
        ]
        crypt32.CryptUnprotectData.restype = ctypes.c_int  # BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        _crypt32, _kernel32 = crypt32, kernel32
    return _crypt32, _kernel32


def _blob_from_bytes(data: bytes) -> tuple[_DATA_BLOB, ctypes.Array]:
    """bytes → DATA_BLOB。返回的 buffer 必须在调用期间存活，一并带回防 GC。"""
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


def dpapi_available() -> bool:
    """当前平台能否 DPAPI 加解密（仅 Windows 且 crypt32/kernel32 可加载）。

    非 Windows 返回 False 是设计内状态（明文兜底），不记日志；
    Windows 上加载失败（极罕见）记日志以便排障。
    """
    if sys.platform != "win32":
        return False
    try:
        _load_crypto_dlls()
        return True
    except OSError as e:
        logger.warning("crypt32/kernel32 加载失败，凭据保持明文：%s", e)
        return False


def encrypt_value(plain: str) -> str:
    """明文凭据 → ``dpapi:<base64>`` 密文。

    非 Windows / DPAPI 不可用 / 加密失败时**原样返回明文**：写配置不能因
    加密失败而中断，宁可保持升级前的明文现状并记日志。空值与已带前缀的
    值原样返回（幂等）。
    """
    if not plain or is_encrypted(plain):
        return plain
    if not dpapi_available():
        return plain
    try:
        crypt32, kernel32 = _load_crypto_dlls()
        # keep 引用底层 buffer，与 in_blob 同生命周期，覆盖整个调用期间
        in_blob, keep = _blob_from_bytes(plain.encode("utf-8"))
        out = _DATA_BLOB()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob), "SkySheep config credential",
            None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            raw = ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
        return DPAPI_PREFIX + base64.b64encode(raw).decode("ascii")
    except Exception as e:  # noqa: BLE001 - 加密失败不能拖垮配置写入
        logger.warning("DPAPI 加密失败，凭据按明文保留：%s", e)
        return plain


def decrypt_value(value: str) -> str | None:
    """``dpapi:<base64>`` → 明文。

    - 不带前缀的值原样返回（明文直通，幂等）；
    - 解不开（跨机器 / 跨账户迁移、密文损坏、DPAPI 不可用）返回 **None**——
      调用方必须按「字段为空」处理并记日志，绝不把密文本体当值用。
    """
    if not is_encrypted(value):
        return value
    try:
        raw = base64.b64decode(value[len(DPAPI_PREFIX):], validate=True)
    except (binascii.Error, ValueError) as e:
        logger.warning("dpapi 值的 base64 解不开，按未配置处理：%s", e)
        return None
    if not raw:
        return ""
    if not dpapi_available():
        logger.warning("当前平台没有 DPAPI，加密凭据按未配置处理")
        return None
    try:
        crypt32, kernel32 = _load_crypto_dlls()
        in_blob, keep = _blob_from_bytes(raw)
        out = _DATA_BLOB()
        descr = ctypes.c_wchar_p()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob), ctypes.byref(descr),
            None, None, None, _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out),
        )
        if not ok:
            raise ctypes.WinError()
        try:
            data = ctypes.string_at(out.pbData, out.cbData)
        finally:
            kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
            # ppszDataDescr 也是系统 LocalAlloc 的输出串（调用方负责释放）：
            # 漏掉就是每次解密泄漏一小块堆内存，decrypt_value 挂在配置读取
            # 热路径上，桌面长驻进程会缓慢累积。descr 为 NULL 时 LocalFree
            # 忽略入参原样返回，无需分支。
            kernel32.LocalFree(ctypes.cast(descr, ctypes.c_void_p))
        return data.decode("utf-8")
    except Exception as e:  # noqa: BLE001 - 解不开按未配置处理，不炸配置读取
        logger.warning(
            "DPAPI 解密失败（跨机器/跨账户迁移或密文损坏），该凭据按未配置处理：%s", e
        )
        return None
