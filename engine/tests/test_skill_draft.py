"""会话存为技能（/save-skill）：草稿生成、落点校验与原子写、WS 与前端接线。

覆盖四层：
1. skills/draft.build_skill_draft —— 从种子会话消息确定性生成 SKILL.md 草稿
   （四小节齐全、只读与写入/执行分开、项目路径泛化、正文 20000 上限）；
2. skills/draft.save_skill_draft —— 与技能安装同一套落点防线
   （路径穿越 / 非法名 / 重名 / frontmatter 不一致一律拒）+ textio 原子写；
3. WS 协议 —— skills.save_from_session（读会话生成草稿）与
   skills.save_draft（落盘后热生效进 boot 快照）；
4. 前端接线 —— SLASH_COMMANDS 的 /save-skill、编辑框回调里的字段拼装。
"""

from __future__ import annotations

import pytest
from test_server import make_client, recv_until

from skysheep.messages import Message, TextBlock, ToolUseBlock
from skysheep.skills.draft import (
    MAX_DRAFT_BODY_CHARS,
    build_skill_draft,
    save_skill_draft,
    suggest_name,
)
from skysheep.skills.installer import SkillInstallError
from skysheep.skills.loader import SkillLoader


def seed_messages() -> list[Message]:
    """一次典型的两轮会话：先读后写，再收尾。"""
    return [
        Message.user("帮我把 data.csv 按月份拆分成多个文件，放到 out/ 目录"),
        Message.assistant([
            TextBlock(text="先看一下目录结构。"),
            ToolUseBlock(id="t1", name="list_dir", input={"path": "."}),
        ]),
        Message.tool_result("t1", "data.csv\nout/"),
        Message.assistant([
            TextBlock(text="开始拆分并写出结果。"),
            ToolUseBlock(
                id="t2", name="write_file",
                input={"path": "out/jan.csv", "content": "月份,金额"},
            ),
        ]),
        Message.tool_result("t2", "ok"),
        Message.assistant([TextBlock(text="拆分完成。")]),
    ]


# ---- 1. 草稿生成 ----


def test_draft_has_four_sections_and_separates_tool_classes():
    draft = build_skill_draft(seed_messages(), safety_of=lambda n: "readonly" if n == "list_dir" else "write")
    body = draft["body"]
    # 四小节齐全
    for section in ("## 目标", "## 步骤", "## 注意事项", "## 适用边界"):
        assert section in body, f"草稿缺小节：{section}"
    # frontmatter 与完整文本
    assert draft["content"].startswith("---\n")
    assert f"name: {draft['name']}" in draft["content"]
    # 目标 = 首条用户消息
    assert "按月份拆分" in body.split("## 步骤")[0]
    # 只读与写入/执行分开列：list_dir 在只读桶，write_file 在写入桶
    steps = body.split("## 步骤")[1].split("## 注意事项")[0]
    readonly_part = steps.split("写入/执行")[0]
    write_part = steps.split("写入/执行")[1]
    assert "`list_dir`" in readonly_part and "write_file" not in readonly_part
    assert "`write_file`" in write_part
    # 按轮次分节：一次 chat.send = 一轮（先读后写在同一轮里）
    assert "### 第 1 轮" in steps
    assert "第 2 轮" not in steps
    # 注意事项列出写入操作并带确认提示
    notes = body.split("## 注意事项")[1].split("## 适用边界")[0]
    assert "`write_file`" in notes
    assert "允许一次" in notes and "确认" in notes
    # 适用边界不泄露项目真实路径
    assert "out/jan.csv" in steps or "jan.csv" in steps


def test_draft_generalizes_project_paths():
    msgs = [
        Message.user("整理 D:\\work\\demo 下的报表"),
        Message.assistant([
            ToolUseBlock(id="t1", name="read_file", input={"path": "D:\\work\\demo\\报表.xlsx"}),
        ]),
    ]
    draft = build_skill_draft(msgs, project_root="D:\\work\\demo")
    assert "D:\\work\\demo" not in draft["body"]
    assert "<项目>" in draft["body"]
    # 建议的描述同样不携带项目路径
    assert "D:\\work\\demo" not in draft["description"]


def test_draft_description_generalizes_before_truncation():
    """描述与 goal 同序：先泛化后截断。

    首行超过 100 字符且项目根绝对路径横跨截断点时，若先按上限截断再泛化，
    根路径会被腰斩、泛化匹配不到完整根路径——半截绝对路径（如 D:\\very\\se）
    泄进 frontmatter 的 description，随 SKILL.md 落盘进技能清单与系统提示词。
    """
    root = "D:\\very\\secret\\project_root_dir"
    filler = "请帮我整理这个项目的输出文件并按月份汇总"
    # 项目根从第 90 字符起：旧的「先截断」会把根腰斩成 D:\very\se（前 10 字符）
    first = (filler * 5)[:90] + root + " 下的所有文件"
    assert len(first) > 100 and first[90:100] == root[:10]
    msgs = [Message.user(first), Message.assistant([TextBlock(text="好的")])]
    draft = build_skill_draft(msgs, project_root=root)
    assert "D:" not in draft["description"], f"描述泄漏了绝对路径片段：{draft['description']}"
    assert "<项目>" in draft["description"], "项目根必须先泛化成占位符，截断才不会把它腰斩"
    assert draft["description"].endswith("…"), "泛化后仍超长才截断，截断要带省略号"
    # 正文 goal 的既有口径不受影响：完整泛化
    assert root not in draft["body"] and "<项目>" in draft["body"]


def test_draft_without_tool_calls_is_honest():
    msgs = [Message.user("解释一下什么是闭包"), Message.assistant([TextBlock(text="闭包是…")])]
    draft = build_skill_draft(msgs)
    assert draft["turn_count"] == 0 and draft["tool_calls"] == 0
    assert "没有实际工具调用" in draft["body"]
    assert draft["name"]  # 仍有名字建议


def test_draft_body_capped_at_load_skill_limit():
    filler = "p" * 80
    blocks = [
        # 序号放前缀里：参数要点按 60 字符截断，区分段被截掉会被当作重复调用折叠
        ToolUseBlock(id=f"t{i}", name="write_file", input={"path": f"{i}-{filler}"})
        for i in range(50)
    ]
    msgs = []
    for _ in range(6):  # 6 轮 × 50 条调用要点，把正文顶过 20000 上限
        msgs.append(Message.user("做点事"))
        msgs.append(Message.assistant(list(blocks)))
    draft = build_skill_draft(msgs)
    assert draft["tool_calls"] == 300
    assert len(draft["body"]) <= MAX_DRAFT_BODY_CHARS + 200  # 截断说明那一行
    assert "已截断" in draft["body"]


def test_suggest_name_strips_unsafe_chars_and_prefixes():
    assert suggest_name("# 把 `日志.log` 按周归档: 步骤\n第二行不算") == "把 `日志.log` 按周归档 步骤"
    assert suggest_name("../etc/passwd") == "etcpasswd"  # 分隔符与目录引用记号被剔
    assert suggest_name("a/b\\c:d?e*f\"g<h>i|j") == "abcdefghij"
    assert suggest_name("   ") == "未命名技能"


# ---- 2. 落点校验与原子写 ----


def _draft_content(name="my-skill", description="测试草稿", body="## 目标\n\n做一件事。\n"):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}"


def test_save_skill_draft_writes_and_loader_discovers(tmp_path):
    root = tmp_path / "skills"
    existing: set[str] = set()
    result = save_skill_draft(root, "my-skill", _draft_content(), existing=existing)
    md = root / "my-skill" / "SKILL.md"
    assert result == {"name": "my-skill", "path": str(md)}
    assert md.is_file()
    # 原子写产物能被既有发现逻辑认出来（frontmatter 完整、名字一致）
    loader = SkillLoader(global_dir=root)
    skills = loader.discover()
    assert [s.name for s in skills] == ["my-skill"]
    assert skills[0].description == "测试草稿"


@pytest.mark.parametrize("evil", [
    "../escape", "..\\escape", "a/b", "a\\b", "C:evil", "name:stream", "..", "  ",
])
def test_save_skill_draft_rejects_illegal_names(tmp_path, evil):
    with pytest.raises(SkillInstallError):
        save_skill_draft(tmp_path / "skills", evil, _draft_content(name=evil), existing=set())


def test_save_skill_draft_rejects_duplicate(tmp_path):
    root = tmp_path / "skills"
    save_skill_draft(root, "my-skill", _draft_content(), existing=set())
    with pytest.raises(SkillInstallError) as ei:
        save_skill_draft(root, "my-skill", _draft_content(), existing={"my-skill"})
    assert "同名" in str(ei.value)


def test_save_skill_draft_rejects_bad_content(tmp_path):
    root = tmp_path / "skills"
    with pytest.raises(SkillInstallError):
        save_skill_draft(root, "my-skill", "", existing=set())  # 空内容
    with pytest.raises(SkillInstallError):
        save_skill_draft(root, "my-skill", "没有 frontmatter 的正文", existing=set())
    # frontmatter name 与保存名不一致：清单名会和编辑框填的对不上
    with pytest.raises(SkillInstallError) as ei:
        save_skill_draft(root, "my-skill", _draft_content(name="other"), existing=set())
    assert "不一致" in str(ei.value)
    # 缺 description：技能清单里没有描述很难辨认
    with pytest.raises(SkillInstallError):
        save_skill_draft(root, "my-skill", "---\nname: my-skill\n---\n\n正文", existing=set())


def test_save_skill_draft_rejects_oversized_body(tmp_path):
    body = "很长的步骤" * (MAX_DRAFT_BODY_CHARS // 4 + 100)
    with pytest.raises(SkillInstallError) as ei:
        save_skill_draft(
            tmp_path / "skills", "my-skill",
            _draft_content(body=body), existing=set(),
        )
    assert "20000" in str(ei.value)


# ---- 3. WS 接线 ----


def test_ws_save_from_session_and_save_draft(home):
    script = [
        [ToolUseBlock(id="t1", name="list_dir", input={"path": "."})],
        [ToolUseBlock(id="t2", name="write_file", input={"path": "out.txt", "content": "hi"})],
        [TextBlock(text="完成。")],
    ]
    with make_client(home, script) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        recv_until(ws, "b")
        ws.send_json({"id": "c1", "method": "chat.send", "params": {"text": "写个配置文件"}})
        got_request = False
        while True:
            frame = ws.receive_json()
            if "event" not in frame:
                if frame.get("id") == "c1":
                    break
                continue
            if frame["event"] == "permission_request":
                got_request = True
                ws.send_json({
                    "id": "p1", "method": "permission.respond",
                    "params": {"request_id": frame["data"]["request_id"],
                               "decision": "allow_once"},
                })
        assert got_request

        # 生成草稿：只读，不落任何文件
        ws.send_json({"id": "g", "method": "skills.save_from_session", "params": {}})
        draft = recv_until(ws, "g")["result"]
        for section in ("## 目标", "## 步骤", "## 注意事项", "## 适用边界"):
            assert section in draft["body"]
        assert "写个配置文件" in draft["body"]  # 目标取首条用户消息
        assert "list_dir" in draft["body"] and "write_file" in draft["body"]

        # 用前端同样的拼法保存（改个名字模拟用户编辑）
        name = "config-writer"
        draft["name"] = name
        draft["content"] = (
            f"---\nname: {name}\ndescription: {draft['description']}\n---\n\n{draft['body']}"
        )
        ws.send_json({"id": "s", "method": "skills.save_draft",
                      "params": {"name": name, "content": draft["content"], "scope": "global"}})
        saved = recv_until(ws, "s")["result"]
        assert saved["name"] == name and saved["scope"] == "global"
        md = home / "home" / "skills" / name / "SKILL.md"
        assert md.is_file()
        assert f"name: {name}" in md.read_text(encoding="utf-8")

        # 热生效：boot 快照里立即出现，无需重启
        ws.send_json({"id": "b2", "method": "boot"})
        snap = recv_until(ws, "b2")["result"]
        assert name in [s["name"] for s in snap["skills"]]

        # 非法名：后端报错原文透传给前端
        ws.send_json({"id": "s2", "method": "skills.save_draft",
                      "params": {"name": "../evil", "content": draft["content"], "scope": "global"}})
        frame = recv_until(ws, "s2")
        assert not frame["ok"] and "路径分隔符" in frame["error"]

        # 重名：与安装同一口径，不覆盖
        ws.send_json({"id": "s3", "method": "skills.save_draft",
                      "params": {"name": name, "content": draft["content"], "scope": "global"}})
        frame = recv_until(ws, "s3")
        assert not frame["ok"] and "同名" in frame["error"]


def test_ws_save_from_session_without_session(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "b", "method": "boot"})
        recv_until(ws, "b")
        ws.send_json({"id": "g", "method": "skills.save_from_session", "params": {}})
        frame = recv_until(ws, "g")
        assert not frame["ok"] and "会话" in frame["error"]


# ---- 4. 前端接线 ----


def test_frontend_save_skill_wiring():
    from conftest import read_app_bundle

    js = read_app_bundle()
    # 命令注册 + 执行分支
    assert '{ cmd: "/save-skill"' in js
    assert 'case "/save-skill":' in js and "saveSkillDraftModal()" in js
    # 编辑框：生成请求、三个可编辑字段、frontmatter 由 name/description 拼装
    modal = js[js.index("async function saveSkillDraftModal()"):]
    modal = modal[:modal.index("// ---------- MCP：导入")]
    assert 'request("skills.save_from_session"' in modal
    assert 'data-f="name"' in modal and 'data-f="description"' in modal and 'data-f="body"' in modal
    assert "name: ${name}" in modal and "description: ${description}" in modal
    assert 'request("skills.save_draft"' in modal
    # 成功后引导去 MCP / Skills 页（用术语表里的唯一叫法）
    assert "MCP / Skills" in modal
