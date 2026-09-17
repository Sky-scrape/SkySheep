"""右侧文件面板编辑器：fs.read 的 mtime/editable 元数据 + fs.write 保存链路。

覆盖：正常保存、冲突检测（base_mtime 过期拒绝落盘、force 强制覆盖）、
新建文件（含子目录）、同名创建冲突、沙箱越界、Windows 非法文件名、
大小限制、目录目标、以及「提取文本不可写回」的 editable 标记。
"""

from __future__ import annotations

from test_server import make_client, recv_until


def _ws(home, wid):
    """起一个带空脚本的客户端并返回已连接的 ws（测试辅助）。"""
    ctx = make_client(home, [])
    client = ctx.__enter__()
    ws = client.websocket_connect("/ws").__enter__()
    return ctx, client, ws, wid


def _call(ws, wid, method, params):
    ws.send_json({"id": wid, "method": method, "params": params})
    return recv_until(ws, wid)


# ---------- fs.read 元数据：编辑器据此决定可否写回 ----------


def test_fs_read_returns_mtime_and_editable(home):
    (home / "proj" / "a.md").write_text("# hi", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "m1", "fs.read", {"path": "a.md"})
        assert r["ok"]
        res = r["result"]
        assert res["editable"] is True
        assert isinstance(res["mtime"], int)
        # 毫秒精度：ns 级超出 JS Number 安全整数，JSON 往返会丢精度
        expect = (home / "proj" / "a.md").stat().st_mtime_ns // 1_000_000
        assert res["mtime"] == expect


def test_fs_read_doc_extract_not_editable(home):
    """PDF/DOCX/XLSX 的提取文本改了也存不回去：必须标 editable=False。"""
    openpyxl = __import__("openpyxl")
    wb = openpyxl.Workbook()
    wb.active.append(["列"])
    (home / "proj" / "表.xlsx").write_bytes(b"")  # 占位，稍后覆盖
    wb.save(str(home / "proj" / "表.xlsx"))
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "m2", "fs.read", {"path": "表.xlsx"})
        assert r["ok"] and r["result"]["doc"] is True
        assert r["result"]["editable"] is False


# ---------- fs.write：保存 / 冲突 / 新建 ----------


def test_fs_write_saves_and_roundtrips(home):
    (home / "proj" / "a.md").write_text("旧内容", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        read = _call(ws, "w1", "fs.read", {"path": "a.md"})["result"]
        r = _call(ws, "w2", "fs.write", {
            "path": "a.md", "text": "新内容", "base_mtime": read["mtime"],
        })
        assert r["ok"] and r["result"]["saved"] is True
        assert r["result"]["size"] == len("新内容".encode())
        assert (home / "proj" / "a.md").read_text(encoding="utf-8") == "新内容"
        # 保存后 mtime 更新，可用于下一轮编辑的冲突基准
        assert r["result"]["mtime"] == (home / "proj" / "a.md").stat().st_mtime_ns // 1_000_000


def test_fs_write_conflict_rejected_until_forced(home):
    """编辑期间文件被外部（Agent/其他程序）改过：拒绝落盘，force 才覆盖。"""
    (home / "proj" / "a.txt").write_text("磁盘旧版", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        read = _call(ws, "c1", "fs.read", {"path": "a.txt"})["result"]
        # 模拟 Agent 在用户编辑期间改了磁盘
        (home / "proj" / "a.txt").write_text("Agent 改的", encoding="utf-8")
        r = _call(ws, "c2", "fs.write", {
            "path": "a.txt", "text": "我的修改", "base_mtime": read["mtime"],
        })
        assert r["ok"], "冲突不是协议错误，应正常返回"
        assert r["result"]["conflict"] is True and r["result"]["saved"] is False
        assert (home / "proj" / "a.txt").read_text(encoding="utf-8") == "Agent 改的", \
            "冲突时绝不能落盘"
        # force=True：用户明确选择覆盖
        r2 = _call(ws, "c3", "fs.write", {
            "path": "a.txt", "text": "我的修改", "base_mtime": read["mtime"], "force": True,
        })
        assert r2["result"]["saved"] is True
        assert (home / "proj" / "a.txt").read_text(encoding="utf-8") == "我的修改"


def test_fs_write_creates_new_file_with_subdir(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "n1", "fs.write", {
            "path": "docs/笔记.md", "text": "# 计划", "base_mtime": None,
        })
        assert r["ok"] and r["result"]["saved"] is True
        assert (home / "proj" / "docs" / "笔记.md").read_text(encoding="utf-8") == "# 计划"


def test_fs_write_create_refuses_existing_name(home):
    """新建流程传 base_mtime=0：同名文件已存在必须冲突，不能静默覆盖。"""
    (home / "proj" / "已有.md").write_text("别覆盖我", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "n2", "fs.write", {
            "path": "已有.md", "text": "", "base_mtime": 0,
        })
        assert r["ok"] and r["result"]["conflict"] is True
        assert (home / "proj" / "已有.md").read_text(encoding="utf-8") == "别覆盖我"


def test_fs_write_sandbox_and_bad_names(home):
    cases = [
        ("../outside.txt", ".. 段"),             # 目录穿越（段校验先拦）
        ("C:/Windows/evil.txt", None),           # 绝对路径落在工作区外（沙箱拦截）
        ("a:b.txt", "非法字符"),                  # Windows 保留冒号
        ("x<y.txt", "非法字符"),
        ("bad/.. /x.txt", None),                 # 含 .. 段（路径里带空格变体也被段校验拦）
        ("名字.", "空格或点结尾"),                # Windows 不允许文件名以点结尾
        ("子 /x.md", "空格或点结尾"),             # 目录段以空格结尾同理拒绝
    ]
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i, (path, expect_msg) in enumerate(cases):
            r = _call(ws, f"b{i}", "fs.write", {"path": path, "text": "x", "base_mtime": None})
            assert not r["ok"], f"{path} 应被拒绝"
            if expect_msg:
                assert expect_msg in r["error"], f"{path}: {r['error']}"
        # 中间带点/空格的普通名字合法
        r = _call(ws, "b-ok1", "fs.write", {"path": "名字 .md", "text": "x", "base_mtime": None})
        assert r["ok"] and r["result"]["saved"] is True
        r = _call(ws, "b-ok2", "fs.write", {"path": "名字. md", "text": "x", "base_mtime": None})
        assert r["ok"] and r["result"]["saved"] is True
        # 沙箱外的文件绝不能被创建出来
        assert not (home / "outside.txt").exists()


def test_fs_write_size_limit(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "s1", "fs.write", {
            "path": "big.txt", "text": "x" * 2_000_001, "base_mtime": None,
        })
        assert not r["ok"] and "2MB" in r["error"]
        assert not (home / "proj" / "big.txt").exists()


def test_fs_write_to_directory_rejected(home):
    (home / "proj" / "子目录").mkdir()
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "d1", "fs.write", {"path": "子目录", "text": "x", "base_mtime": None})
        assert not r["ok"] and "写入失败" in r["error"]


def test_fs_write_empty_path_rejected(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "e1", "fs.write", {"path": "  ", "text": "x"})
        assert not r["ok"]


# ---------- 边界加固：保留设备名 / 非法 base_mtime / 文件被删 / symlink ----------


def test_fs_write_windows_reserved_device_names(home):
    """CON / con.txt 这类保留设备名必须拒绝（旧系统上无法正常打开/删除）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        for i, path in enumerate(("CON", "con.txt", "NUL.md", "com1", "aux.log")):
            r = _call(ws, f"rn{i}", "fs.write", {"path": path, "text": "x", "base_mtime": None})
            assert not r["ok"], f"{path} 应被拒绝"
            assert "保留设备名" in r["error"], f"{path}: {r['error']}"
        # 含保留词但词根不同名的正常通过
        r = _call(ws, "rn-ok", "fs.write", {"path": "console.md", "text": "x", "base_mtime": None})
        assert r["ok"] and r["result"]["saved"] is True


def test_fs_write_bad_base_mtime_type(home):
    """base_mtime 非整数：给出可读中文错误，而不是漏出 int() 的英文异常。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "bm1", "fs.write", {"path": "a.txt", "text": "x", "base_mtime": "abc"})
        assert not r["ok"] and "base_mtime 必须是整数" in r["error"]


def test_fs_write_after_file_deleted_recovers(home):
    """编辑期间文件被删：保存走新建路径，内容恢复而非报错。"""
    (home / "proj" / "story.md").write_text("v1", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        read = _call(ws, "dl1", "fs.read", {"path": "story.md"})["result"]
        (home / "proj" / "story.md").unlink()
        r = _call(ws, "dl2", "fs.write", {
            "path": "story.md", "text": "v2", "base_mtime": read["mtime"],
        })
        assert r["ok"] and r["result"]["saved"] is True
        assert (home / "proj" / "story.md").read_text(encoding="utf-8") == "v2"


def test_fs_read_directory_rejected(home):
    (home / "proj" / "sub").mkdir()
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "dr1", "fs.read", {"path": "sub"})
        assert not r["ok"] and "不存在" in r["error"]


def test_fs_write_symlink_escape_rejected(home):
    """工作区内的符号链接指向外部：resolve 后越界必须拒绝（Windows 建链要特权，建不了就跳过）。"""
    target = home / "outside-symlink-target.txt"
    target.write_text("secret", encoding="utf-8")
    link = home / "proj" / "evil-link.txt"
    try:
        link.symlink_to(target)
    except OSError:
        import pytest

        pytest.skip("当前系统无权限创建符号链接")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r = _call(ws, "sl1", "fs.write", {"path": "evil-link.txt", "text": "hacked", "base_mtime": None})
        assert not r["ok"]
        assert target.read_text(encoding="utf-8") == "secret", "symlink 目标绝不能被写穿"


# ---------- 端到端闭环：read → 外部改动 → write 冲突 → force 读回 ----------


def test_editor_roundtrip_with_external_change(home):
    path = home / "proj" / "story.md"
    path.write_text("第一稿", encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        r1 = _call(ws, "t1", "fs.read", {"path": "story.md"})["result"]
        path.write_text("第二稿（Agent 改的）", encoding="utf-8")
        conflict = _call(ws, "t2", "fs.write", {
            "path": "story.md", "text": "第一稿续写", "base_mtime": r1["mtime"],
        })["result"]
        assert conflict["conflict"] is True
        # 冲突返回的 mtime 是磁盘当前值：用户选择「覆盖」后新基准正确
        r2 = _call(ws, "t3", "fs.write", {
            "path": "story.md", "text": "最终稿", "base_mtime": conflict["mtime"],
        })
        assert r2["result"]["saved"] is True
        assert path.read_text(encoding="utf-8") == "最终稿"
        # os.stat 的 mtime 与返回值同源（毫秒精度，JS 安全整数范围内）
        assert path.stat().st_mtime_ns // 1_000_000 == r2["result"]["mtime"]
