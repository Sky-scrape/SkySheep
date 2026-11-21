"""设置 · 高级「安全与后台」三项：定时调度总开关 / 命令沙箱遏制 / 沙箱级别。

锁三层：① advanced.save 把 [cron] system_schedule、[shell] job_containment、
[shell] sandbox_level 写进 config.toml，advanced.get 读回一致（热生效的读取
点都在各自执行路径上按配置取值，设置页只负责落盘）；
② 入口校验：非法 sandbox_level 拒绝、部分提交不动其它键；
③ 前端接线：控件、渲染读值与保存 payload 都在（与既有高级设置开关同一写法）。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from conftest import read_app_bundle
from test_server import make_client, recv_until

from skysheep.config import config_path, load_config

ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "src" / "skysheep" / "server" / "static"


def call(home, method, params=None):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "m1", "method": method, "params": params or {}})
        return recv_until(ws, "m1")


def read_toml(home):
    return tomllib.loads(config_path().read_text(encoding="utf-8"))


# ---------- ① 默认值与写读回环 ----------

def test_advanced_get_exposes_three_defaults(home):
    """advanced.get 回传三项；未配置时是产品默认（调度关 / 遏制开 / job 档）。"""
    frame = call(home, "advanced.get")
    assert frame["ok"]
    r = frame["result"]
    assert r["system_schedule"] is False
    assert r["job_containment"] is True
    assert r["sandbox_level"] == "job"


def test_advanced_save_roundtrip_persists_to_config_toml(home):
    """三项经 advanced.save 落 config.toml（[cron] / [shell] 段），读回一致。"""
    frame = call(home, "advanced.save", {
        "system_schedule": True,
        "job_containment": True,
        "sandbox_level": "restricted",
    })
    assert frame["ok"]
    r = frame["result"]
    assert r["system_schedule"] is True
    assert r["job_containment"] is True
    assert r["sandbox_level"] == "restricted"

    # 真的写进了 config.toml 的对应段
    raw = read_toml(home)
    assert raw["cron"]["system_schedule"] is True
    assert raw["shell"]["job_containment"] is True
    assert raw["shell"]["sandbox_level"] == "restricted"

    # load_config（执行路径的读入口）解析出同样的值；新连接 advanced.get 读回一致
    cfg = load_config()
    assert cfg.cron.system_schedule is True
    assert cfg.shell.job_containment is True
    assert cfg.shell.sandbox_level == "restricted"
    frame = call(home, "advanced.get")
    assert frame["result"]["system_schedule"] is True
    assert frame["result"]["sandbox_level"] == "restricted"

    # 关回去 / 退回 job 档：覆写生效，段里旧值被替换而不是残留
    frame = call(home, "advanced.save", {
        "system_schedule": False,
        "job_containment": False,
        "sandbox_level": "job",
    })
    assert frame["ok"]
    raw = read_toml(home)
    assert raw["cron"]["system_schedule"] is False
    assert raw["shell"]["job_containment"] is False
    assert raw["shell"]["sandbox_level"] == "job"


# ---------- ② 入口校验与部分提交 ----------

def test_advanced_save_rejects_bad_sandbox_level(home):
    """沙箱级别只认 job / restricted：非法值整次拒绝、配置不动（入口侧校验）。"""
    frame = call(home, "advanced.save", {"sandbox_level": "appcontainer"})
    assert not frame["ok"] and "沙箱级别" in frame["error"]
    assert load_config().shell.sandbox_level == "job"  # 未被写脏

    frame = call(home, "advanced.save", {"sandbox_level": True})
    assert not frame["ok"]  # 布尔也不行——档位是白名单字符串


def test_advanced_save_partial_keeps_other_keys(home):
    """只提交一个键时其余键不动（None = 不动）：电脑控制那类按键提交不受影响。"""
    call(home, "advanced.save", {"system_schedule": True, "sandbox_level": "restricted"})
    # 只动 job_containment：调度与档位保持上一次的值
    frame = call(home, "advanced.save", {"job_containment": False})
    assert frame["ok"]
    r = frame["result"]
    assert r["job_containment"] is False
    assert r["system_schedule"] is True
    assert r["sandbox_level"] == "restricted"


# ---------- ③ 前端接线（零构建，跟既有高级设置开关同一写法） ----------

def test_sandbox_settings_frontend_wired(home):
    """「安全与后台」卡三控件就位；渲染读值、保存 payload、级别随遏制置灰都在。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for dom_id, label in (
        ("adv-system-schedule", "系统级定时调度"),
        ("adv-job-containment", "命令沙箱遏制"),
        ("adv-sandbox-level", "沙箱级别"),
    ):
        assert f'id="{dom_id}"' in html and label in html
    # 档位下拉的两个取值；restricted 文案如实说明特权命令会失败
    assert '<option value="job">' in html and '<option value="restricted">' in html
    assert "受限令牌" in html and "部分需要特权的命令会失败" in html

    js = read_app_bundle()
    # 渲染回填三处 + 保存 payload 三处
    assert 'q("adv-system-schedule").checked = !!d.system_schedule' in js
    assert 'q("adv-job-containment").checked = d.job_containment !== false' in js
    assert 'q("adv-sandbox-level").value = d.sandbox_level === "restricted"' in js
    assert "system_schedule: q(\"adv-system-schedule\").checked" in js
    assert "job_containment: q(\"adv-job-containment\").checked" in js
    assert "sandbox_level: q(\"adv-sandbox-level\").value" in js
    # 遏制关闭 → 级别置灰（总开关关 = 不沙箱，档位不该显得还在起作用）
    assert "syncSandboxLevel" in js

    # 设置内搜索按卡片文字命中：三份文案都在 .settings-card 文本里
    # （settingsCardIndex 直接读 textContent，无需额外登记）
    assert html.index('id="settings-page-advanced"') < html.index("系统级定时调度")


# ---------- ④ 凭据加密降级提示（进程内标记 → advanced.get 载荷 → 前端） ----------

def test_advanced_get_credential_crypto_default_clean(home, monkeypatch):
    """advanced.get 回传 credential_crypto；进程内未发生过降级时是干净状态。"""
    import skysheep.secure_store as secure_store

    monkeypatch.setattr(secure_store, "_degraded_reason", None)
    frame = call(home, "advanced.get")
    assert frame["ok"]
    assert frame["result"]["credential_crypto"] == {"degraded": False, "reason": None}


def test_advanced_get_credential_crypto_degraded(home, monkeypatch):
    """进程内发生过一次加密降级 → 载荷点亮 degraded 并带原因，
    设置页据此亮「凭据未能加密」提示（不能只留在日志里）。"""
    import skysheep.secure_store as secure_store

    monkeypatch.setattr(
        secure_store, "_degraded_reason", "DPAPI 加密失败，凭据按明文保存：测试注入"
    )
    frame = call(home, "advanced.get")
    assert frame["ok"]
    cc = frame["result"]["credential_crypto"]
    assert cc["degraded"] is True
    assert "明文" in cc["reason"]


def test_credential_crypto_notice_wired():
    """降级提示的前端接线就位：index.html 挂点 + app-advanced.js 渲染与
    「知道了」关闭。锚稳定 id 与载荷字段名（credential_crypto），不锚文案。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="adv-credential-crypto-warn"' in html
    js = read_app_bundle()
    assert "credential_crypto" in js, "渲染必须读 advanced.get 的降级载荷字段"
    assert 'q("adv-credential-crypto-warn")' in js
    assert "btn-crypto-warn-dismiss" in js


# ---------- ⑤ 全局热键保存失败必须回错（与开机自启同一 RuntimeError 口径） ----------

def test_advanced_save_hotkey_failure_reports_error(home, monkeypatch):
    """热键偏好写盘失败要回可读错误，不能静默吞掉让前端拿到「保存成功」：
    此前是 except pass，用户重启后才发现热键没生效且无任何报错。
    模拟磁盘故障锚在 preferences 模块实际落盘调用（write_text_atomic）；
    只对含热键值的写入失败——启动期 _bind_project 的 ui.json 写入照常放行。"""
    import skysheep.server.backend_parts.preferences as prefs_mod

    real = prefs_mod.write_text_atomic

    def boom(path, text, **kwargs):
        if "Ctrl+Shift+F9" in text:
            raise OSError("模拟磁盘故障")
        return real(path, text, **kwargs)

    monkeypatch.setattr(prefs_mod, "write_text_atomic", boom)
    frame = call(home, "advanced.save", {"hotkey": "Ctrl+Shift+F9"})
    assert not frame["ok"]
    assert "全局热键保存失败" in frame["error"]
    assert "模拟磁盘故障" in frame["error"], "底层原因要透传给前端"


def test_advanced_save_hotkey_success_still_roundtrips(home):
    """正常路径不受影响：热键保存成功且落 ui.json（守住改动没有误伤主路径；
    与 test_settings_extras.test_hotkey_pref_and_parser 的回环断言同口径）。"""
    frame = call(home, "advanced.save", {"hotkey": "Ctrl+Shift+F9"})
    assert frame["ok"]
    ui = json.loads((home / "home" / "ui.json").read_text(encoding="utf-8"))
    assert ui.get("hotkey") == "Ctrl+Shift+F9"
