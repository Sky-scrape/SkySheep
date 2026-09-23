"""飞书渠道：驱动官方 lark-cli 子进程（`larksuite/cli`，MIT）。

## 为什么是子进程而不是自己实现协议

飞书收事件必须走 WebSocket 长连接 + protobuf 帧（pbbp2），且每帧要在 3 秒内 ACK。
自己实现要长期跟进官方协议演进；官方 CLI 已经把这一层封装成两条稳定命令：

    收：lark-cli event consume im.message.receive_v1 --as bot   → stdout 逐行 NDJSON
    发：lark-cli im +messages-send --chat-id oc_x --text ... --as bot

所以本适配器只做三件事：拉起/守护子进程、把 NDJSON 归一化成 ChannelMessage、
把回复交给 send 命令。协议细节（握手、心跳、分片、ACK、重连、去重键语义）
都由官方 CLI 维护。

## 与另外两个适配器的结构性差异

* 微信用 getupdates 游标轮询、飞书则是一条常驻子进程；本适配器管的是**进程生命周期**
  （拉起 → 等 ready → 读流 → 退出后重连），不是网络连接。
* 凭据不写在 config.toml 里能被 CLI 读到的地方，而是由我们用
  ``config init --app-secret-stdin`` 落进 CLI 自己的配置目录（App Secret 走 stdin，
  不出现在进程命令行里，避免被同机其它进程从进程列表读到）。

## CLI 输出的形状（实测 + 官方 Go 源码核对）

`im.message.receive_v1` 被 CLI 的 convertlib 预渲染过，**不是**原始 OAPI 形状：

* 事件体是**扁平**的（字段直接在顶层，不是 `.event.xxx`）。
* ``content`` 对 text/post 等类型已是**人类可读文本**，且 @提及已被替换成显示名；
  只有 interactive（卡片）仍是原始 JSON 字符串。所以本适配器直接取 ``content``，
  不再自己去解析 content JSON、也不再清 ``@_user_N`` 占位符（CLI 已处理）。
* ``mentions`` 是精简后的 ``[{key, id, name}]``，``id`` 是 open_id。

## 群聊 @ 机器人的处理

飞书平台默认只把「@了机器人」的群消息推给机器人（`im:message.group_at_msg:readonly`），
但若应用另授了「接收群聊中所有消息」的权限，群里每句话都会被推过来。为免机器人在群里
乱插嘴，本适配器对群聊要求消息里**至少有一个提及**，否则跳过（可用
``require_mention_in_group`` 关掉）。真正的授权边界仍是 allowed_ids。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import threading
import time

from .base import Channel, ChannelMessage, split_text

logger = logging.getLogger("skysheep.channels.feishu")

# 要订阅的事件：接收消息
EVENT_KEY = "im.message.receive_v1"

# 子进程可执行名（按 PATH 查找；也接受配置里显式给的绝对路径）
CLI_NAMES = ("lark-cli", "lark-cli.exe")

# 单条消息长度上限。飞书文本上限 150 KB，这里取一个远低于上限、手机上也便于阅读的值。
MAX_TEXT = 4000

# 拉起的子进程把 stdout 一行一行吐出来；单行上限给足，避免大卡片事件被截断。
_MAX_LINE = 1024 * 1024

# 等 ready 标记的最长时间。CLI 建连要先向飞书换地址再握长连接，给它留足时间。
READY_TIMEOUT = 60.0

# 子进程异常退出的重连退避（指数，带上限）
RECONNECT_DELAY = 3.0
RECONNECT_MAX_DELAY = 60.0

# 事件去重窗口：重连后服务端可能重推同一条消息（按 message_id 判重）。
DEDUP_TTL = 1800.0
_DEDUP_SWEEP = 200

# 无这些变量时子进程可能弹控制台窗口（GUI 版会显得像闪黑框）
_CREATE_NO_WINDOW = 0x08000000


def _resolve_cli(config: dict) -> str | None:
    """定位 lark-cli 可执行文件：配置里显式指定优先，其次按 PATH 找。"""
    explicit = str(config.get("cli_path", "") or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for name in CLI_NAMES:
        found = shutil.which(name)
        if found:
            return found
    return None


class FeishuChannel(Channel):
    name = "feishu"

    def __init__(self, config: dict, on_message) -> None:
        super().__init__(config, on_message)
        self.app_id = str(self.config.get("app_id", "") or "").strip()
        self.app_secret = str(self.config.get("app_secret", "") or "").strip()
        # 群聊是否要求有提及（默认要求，避免机器人对群里所有话插嘴）
        self.require_mention_in_group = bool(
            self.config.get("require_mention_in_group", True)
        )
        self.cli = _resolve_cli(self.config)
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = False
        self.connected = False          # 子进程是否已 ready（真的在收事件）
        self._seen_ids: dict[str, float] = {}
        self._dedup_sweep = 0
        self._cli_version = ""

    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret)

    # ---- CLI 环境 ----

    def config_dir(self) -> str:
        """CLI 的配置目录。

        必须与 SkySheep 的数据目录同源：CLI 默认写 ``~/.lark-cli``，那既违反本项目
        「用户数据一律在 ~/.skysheep/」的约定，也会让 SKYSHEEP_HOME / 多实例隔离失效
        （dev 身份会与安装版抢同一份凭据）。用官方支持的 LARKSUITE_CLI_CONFIG_DIR
        把它指到 home 下即可。
        """
        from ..config import skysheep_home

        return str(skysheep_home() / "feishu-cli")

    def _env(self) -> dict:
        env = dict(os.environ)
        env["LARKSUITE_CLI_CONFIG_DIR"] = self.config_dir()
        # 关掉升级通知与技能更新提示：无人值守的渠道进程不该自行拉新二进制，
        # 也不该在 stdout/stderr 里混入 _notice（会干扰事件流的 NDJSON 解析）。
        # 名字取自二进制里的常量（实测 *_NOTIFIER 后缀才是真正被读的变量名）。
        env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        return env

    def _run_cli(self, args: list[str], *, stdin_text: str = "",
                 timeout: float = 60.0) -> tuple[bool, dict]:
        """同步跑一条 lark-cli 命令，返回 (成功, JSON 信封)。

        失败时信封里带 error.message / error.hint，调用方据此给可读原因。
        """
        if not self.cli:
            return False, {"error": {"message": "没找到 lark-cli，请先安装官方 CLI"}}
        kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "env": self._env(),
            "cwd": self.config_dir() if os.path.isdir(self.config_dir()) else None,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            proc = subprocess.run(  # noqa: S603 - 参数由本模块构造，不经 shell
                [self.cli, *args],
                input=stdin_text,
                timeout=timeout,
                **kwargs,
            )
        except subprocess.TimeoutExpired:
            return False, {"error": {"message": f"lark-cli {' '.join(args[:2])} 超时"}}
        except OSError as e:
            return False, {"error": {"message": f"无法启动 lark-cli：{e}"}}

        out = (proc.stdout or "").strip()
        payload: dict = {}
        if out:
            # CLI 的 stdout 常是「人类可读提示行 + JSON」混在一起（实测）：
            # config init 成功时只有 "OK: Configuration saved to ..."（无 JSON）；
            # 失败时是 OK 行 + 错误信封，且信封可能是多行 pretty-printed。
            # 所以从每个以 { 开头的行起，把「到末尾的全文」试着当作 JSON 解析：
            # 整段成功形态、OK 行 + 单行 JSON、OK 行 + 多行 pretty JSON 都能盖住。
            lines = out.splitlines()
            for i, line in enumerate(lines):
                if not line.lstrip().startswith("{"):
                    continue
                try:
                    payload = json.loads("\n".join(lines[i:]).strip())
                    break
                except ValueError:
                    continue
        ok = bool(payload.get("ok")) if payload else proc.returncode == 0
        if not ok and not payload.get("error"):
            detail = (proc.stderr or "").strip()[:300] or out[:300]
            payload = {"error": {"message": detail or f"lark-cli 退出码 {proc.returncode}"}}
        return ok, payload

    @staticmethod
    def _err_text(payload: dict) -> str:
        """把 CLI 的错误信封拼成一句可读原因（含它给的排查提示）。"""
        err = payload.get("error") or {}
        msg = str(err.get("message") or payload.get("raw") or "未知错误").strip()
        hint = str(err.get("hint") or "").strip()
        subtype = str(err.get("subtype") or "").strip()
        bits = [msg]
        if subtype and subtype not in msg:
            bits.append(f"[{subtype}]")
        if hint:
            bits.append(f"→ {hint}")
        return " ".join(bits)

    def _sync_cli_credentials(self) -> bool:
        """把当前 App ID / App Secret 写进 CLI 的配置目录（有变化才写）。

        App Secret 走 stdin（``--app-secret-stdin``），不进进程命令行。
        """
        marker = os.path.join(self.config_dir(), "config.json")
        try:
            with open(marker, encoding="utf-8") as f:
                cur = json.load(f)
            # 兼容两种落盘形状：我们的假 CLI/早期版本是顶层 appId；
            # 真实 CLI 是 {"apps": [{"appId", ...}]}（可能多应用，取首个）。
            apps = cur.get("apps") if isinstance(cur.get("apps"), list) else None
            saved_id = str((apps[0] if apps else cur).get("appId") or "")
            if saved_id == self.app_id:
                return True  # 已是这份凭据，不必重写
        except (OSError, ValueError):
            pass

        os.makedirs(self.config_dir(), exist_ok=True)
        ok, payload = self._run_cli(
            ["config", "init", "--app-id", self.app_id, "--app-secret-stdin"],
            stdin_text=self.app_secret,
            timeout=90.0,
        )
        if not ok:
            self.error = f"飞书凭据写入失败：{self._err_text(payload)}"
            logger.warning("feishu 凭据写入失败：%s", self.error)
            return False
        return True

    # ---- 生命周期 ----

    async def start(self) -> None:
        if not self.configured():
            self.error = "未填写 App ID / App Secret，无法启动"
            return
        if not self.cli:
            self.error = (
                "没找到 lark-cli（飞书官方 CLI）。请先安装："
                "npm install -g @larksuite/cli；"
                "若装在其他位置，可在本卡片的高级设置里填 cli_path"
            )
            logger.warning("feishu 未找到 lark-cli，渠道无法启动")
            return
        self._stopping = False
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._stopping = True
        await super().stop()
        self._kill_reader()
        await asyncio.to_thread(self._terminate_process)
        await asyncio.to_thread(self._stop_daemon)
        self.connected = False

    def _kill_reader(self) -> None:
        self._reader = None

    def _terminate_process(self) -> None:
        """结束事件子进程。Windows 上连带其子进程一起收（避免残留 daemon 连）。"""
        proc = self._proc
        self._proc = None
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(  # noqa: S603,S607 - 固定命令，清进程树
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    creationflags=_CREATE_NO_WINDOW,
                )
            else:
                proc.terminate()
        except Exception:  # noqa: BLE001 - 退出路径尽力而为
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _stop_daemon(self) -> None:
        """停掉 CLI 的事件 daemon。

        CLI 会为一个应用起一条常驻 bus；不显式停掉的话，渠道关闭后它还占着长连接，
        下次启用可能撞上「已有连接」的限流。
        """
        if not self.cli:
            return
        self._run_cli(["event", "stop", "--force"], timeout=30.0)

    async def _run_loop(self) -> None:
        """常驻：拉起子进程 → 读流 → 退出后按退避重连。"""
        delay = RECONNECT_DELAY
        while not self._stopping:
            try:
                await self._consume_once()
                delay = RECONNECT_DELAY  # 正常结束（如被事件限额收尾）
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 单次失败不终止循环
                self.error = str(e) or e.__class__.__name__
                logger.warning("feishu 事件子进程中断：%s", e)
                delay = min(delay * 2, RECONNECT_MAX_DELAY)
            self.connected = False
            if self._stopping:
                return
            await asyncio.sleep(delay)

    async def _consume_once(self) -> None:
        """一次完整的「起子进程 → 读事件流 → 子进程退出」。"""
        if not await asyncio.to_thread(self._sync_cli_credentials):
            # 凭据不对就别反复重连了：报错停在这，等用户改配置
            raise RuntimeError(self.error or "飞书凭据不可用")
        if self._stopping:
            return

        args = [EVENT_KEY]
        kwargs: dict = {
            "stdin": subprocess.PIPE,       # 保持打开：stdin EOF 会让无界运行优雅退出
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "env": self._env(),
            "cwd": self.config_dir() if os.path.isdir(self.config_dir()) else None,
        }
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        proc = subprocess.Popen(  # noqa: S603 - 参数由本模块构造，不经 shell
            [self.cli, "event", "consume", *args], **kwargs
        )
        self._proc = proc
        logger.info("feishu 事件子进程已启动（pid=%s）", proc.pid)

        ready = asyncio.Event()
        loop = self._loop or asyncio.get_running_loop()
        # stderr 与 stdout 必须同时读：ready 标记在 stderr，事件在 stdout。
        # 只读一个会把另一个的管道缓冲写满、把子进程堵死。
        self._reader = threading.Thread(
            target=self._pump, args=(proc, ready, loop), daemon=True, name="feishu-cli"
        )
        self._reader.start()

        try:
            await asyncio.wait_for(ready.wait(), timeout=READY_TIMEOUT)
        except TimeoutError:
            await asyncio.to_thread(self._terminate_process)
            raise RuntimeError(
                f"lark-cli 在 {int(READY_TIMEOUT)} 秒内没有就绪。"
                "请确认该应用已在开发者后台开启「机器人」能力、事件订阅选了"
                "「使用长连接接收事件」并已发布版本"
            ) from None
        self.connected = True
        self.error = ""

        # 等子进程自己退出（被 stop 时由 _terminate_process 结束）
        while proc.poll() is None:
            await asyncio.sleep(0.5)
        code = proc.returncode
        self.connected = False
        if not self._stopping:
            detail = self.error or f"退出码 {code}"
            raise RuntimeError(f"lark-cli 事件流已结束（{detail}）")

    def _pump(self, proc, ready: asyncio.Event, loop) -> None:
        """在独立线程里同时读 stderr（ready 标记）与 stdout（事件 NDJSON）。"""
        stderr_lines: list[str] = []

        def read_stderr() -> None:
            try:
                assert proc.stderr is not None
                for line in proc.stderr:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    stderr_lines.append(line)
                    # CLI 契约：就绪时 stderr 输出固定一行 ready 标记
                    if "ready" in line and "event_key" in line:
                        loop.call_soon_threadsafe(ready.set)
                    elif "error" in line.lower() or "reason:" in line.lower():
                        # 出错原因留在 stderr，退出时用来解释
                        text = line.strip()
                        if text and not text.startswith("{"):
                            self.error = text[:300]
            except Exception:  # noqa: BLE001 - 读 stderr 失败不该影响主流程
                pass

        threading.Thread(target=read_stderr, daemon=True, name="feishu-err").start()

        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    logger.debug("feishu 跳过非 JSON 行：%s", line[:120])
                    continue
                fut = asyncio.run_coroutine_threadsafe(self._on_event(obj), loop)
                try:
                    fut.result(timeout=30)
                except TimeoutError:
                    # 30 秒没等到结果，多数是渠道串行队列在忙（一轮可能跑几分钟，
                    # 前面可能还有排队的消息）。run_coroutine_threadsafe 的超时只是
                    # 不再等待，协程不会被取消，事件仍会被按序处理——这里只降为
                    # info，不能当失败告警。py3.11 起 concurrent.futures 的超时
                    # 就是内建 TimeoutError。
                    logger.info("feishu 事件处理仍在后台排队，读流继续")
                except Exception as e:  # noqa: BLE001 - 单条事件失败不断流
                    logger.warning("feishu 处理事件失败：%s", e)
        except Exception as e:  # noqa: BLE001 - 流断掉由上层重连
            logger.debug("feishu 读 stdout 结束：%s", e)
        finally:
            # 子进程已退出但没等到 ready：把 stderr 里最后一句当原因
            if not ready.is_set() and stderr_lines:
                loop.call_soon_threadsafe(self._note_failure, stderr_lines[-1])

    def _note_failure(self, line: str) -> None:
        if not self.error and line:
            self.error = line.strip()[:300]

    # ---- 入站 ----

    async def _on_event(self, obj: dict) -> None:
        """一条 CLI 事件 → ChannelMessage。

        CLI 的 ``--format json`` 成功信封是 ``{"ok":true,"data":{...}}``；
        事件流则直接把事件对象逐行吐出，两种形状都兼容。
        """
        if obj.get("ok") is False:
            obj.get("error") or {}
            self.error = self._err_text(obj)
            logger.warning("feishu 事件流报告错误：%s", self.error)
            return
        data = obj.get("data") if isinstance(obj.get("data"), dict) else obj
        msg = parse_event(data, require_mention_in_group=self.require_mention_in_group)
        if msg is None:
            return
        if not self._mark_seen(msg["message_id"]):
            return
        await self.on_message(
            ChannelMessage(
                channel=self.name,
                actor=msg["actor"],
                chat_id=msg["chat_id"],
                text=msg["text"],
                approved=self.is_allowed(msg["actor"], msg["chat_id"]),
                raw=msg["raw"],
            )
        )

    def _mark_seen(self, message_id: str) -> bool:
        """按 message_id 去重（CLI 明确要求用它，而非 event_id）。"""
        if not message_id:
            return True
        now = time.time()
        if message_id in self._seen_ids:
            return False
        self._seen_ids[message_id] = now
        self._dedup_sweep += 1
        if self._dedup_sweep >= _DEDUP_SWEEP:
            self._dedup_sweep = 0
            for key in [k for k, ts in self._seen_ids.items() if now - ts > DEDUP_TTL]:
                self._seen_ids.pop(key, None)
        return True

    # ---- 出站 ----

    async def send_text(self, chat_id: str, text: str) -> bool:
        if not str(chat_id).strip():
            return False
        if not self.cli:
            self.error = "没找到 lark-cli，无法发送消息"
            return False
        ok = True
        for chunk in split_text(text or "", MAX_TEXT):
            sent, payload = await asyncio.to_thread(
                self._run_cli,
                ["im", "+messages-send", "--as", "bot",
                 "--chat-id", chat_id, "--text", chunk],
            )
            if not sent:
                self.error = f"发送失败：{self._err_text(payload)}"
                ok = False
        return ok

    def status(self):
        st = super().status()
        st.extra = {
            "connected": self.connected,
            "cli": bool(self.cli),
            "cli_path": self.cli or "",
            "config_dir": self.config_dir(),
        }
        return st


def parse_event(obj: dict, *, require_mention_in_group: bool = True) -> dict | None:
    """把 CLI 吐出的一条事件归一化成 {actor, chat_id, text, message_id, raw}。

    返回 None 表示这条事件不该交给 Agent：
    * 不是接收消息事件；
    * 机器人/应用自己发的（否则会自己回自己）；
    * 群聊里没有任何提及（避免对群里所有话插嘴）；
    * 非文本类型（图片/文件等，当前版本只处理文本）；
    * 文本为空。

    注意 ``content`` 已由 CLI 预渲染成人类可读文本（@提及已换成显示名），
    所以这里不需要再解析 content JSON，也不需要清 ``@_user_N`` 占位符。
    """
    if not isinstance(obj, dict):
        return None
    # 只认接收消息事件。CLI 会带 type 字段；缺失时按字段齐备程度判断。
    ev_type = str(obj.get("type") or "").strip()
    if ev_type and ev_type != EVENT_KEY:
        return None
    if str(obj.get("sender_type") or "").lower() in ("app", "bot"):
        return None
    if str(obj.get("message_type") or "") != "text":
        return None
    chat_id = str(obj.get("chat_id") or "").strip()
    if not chat_id:
        return None
    chat_type = str(obj.get("chat_type") or "").strip()
    mentions = obj.get("mentions") or []
    if (
        require_mention_in_group
        and chat_type
        and chat_type != "p2p"
        and not any(isinstance(m, dict) and m.get("id") for m in mentions)
    ):
        return None
    text = str(obj.get("content") or "").strip()
    if not text:
        return None
    actor = str(obj.get("sender_id") or "").strip()
    return {
        "actor": actor or chat_id,
        "chat_id": chat_id,
        "text": text,
        "message_id": str(obj.get("message_id") or obj.get("id") or "").strip(),
        "raw": obj,
    }


_split = split_text  # 与其它适配器一致的别名
