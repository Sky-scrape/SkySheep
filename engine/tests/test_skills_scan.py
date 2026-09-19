"""「扫描本机技能」测试：只读探测别家 agent 目录里的技能候选。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from skysheep.models.fake import FakeProvider
from skysheep.server import create_app
from skysheep.skills.installer import LOCAL_SKILL_SOURCES, scan_computer_skills


def make_client(home, script):
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: FakeProvider(script),
    )
    return TestClient(app)


def recv_until(ws, wanted_id=None, events=None):
    """读帧直到收到指定 id 的回复；事件帧收进 events。"""
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


def make_skill_dir(root, name, description="测试技能"):
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n正文步骤\n", encoding="utf-8"
    )
    return d


def _skill(root: Path, folder: str, name: str, desc: str = "d") -> Path:
    d = root / folder
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n正文\n", encoding="utf-8"
    )
    return d


def test_scan_finds_nested_skills_with_origin(tmp_path):
    claude = tmp_path / "claude"
    _skill(claude, "pdf", "pdf-tools", "处理 PDF")
    # 常见嵌套：repo-main/skills/xxx（限深 3 层内能找到）
    agents = tmp_path / "agents"
    _skill(agents, "repo-main/skills/aaa", "aaa")
    cands = scan_computer_skills(
        [("Claude Code", claude), ("agents", agents)], existing=set()
    )
    by_name = {c["name"]: c for c in cands}
    assert set(by_name) == {"pdf-tools", "aaa"}
    assert by_name["pdf-tools"]["origin"] == "Claude Code"
    assert by_name["pdf-tools"]["description"] == "处理 PDF"
    assert not by_name["pdf-tools"]["installed"]
    assert (Path(by_name["pdf-tools"]["path"]) / "SKILL.md").is_file()


def test_scan_dedupes_same_path_and_marks_installed(tmp_path):
    claude = tmp_path / "claude"
    _skill(claude, "pdf", "pdf-tools")
    # 同一来源传两次 = 同一路径，只出一条；与已装技能同名则标 installed
    cands = scan_computer_skills(
        [("Claude Code", claude), ("Claude Code", claude)], existing={"pdf-tools"}
    )
    assert len(cands) == 1
    assert cands[0]["installed"] is True


def test_scan_tolerates_missing_and_empty_roots(tmp_path):
    missing = tmp_path / "nope"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan_computer_skills(
        [("Codex", missing), ("Codex", empty)], existing=set()
    ) == []


def test_scan_falls_back_to_dir_name_when_no_frontmatter(tmp_path):
    # 与技能发现同一套宽容解析：SKILL.md 没有 name 字段时用目录名，正文首行截作描述
    claude = tmp_path / "claude"
    plain = claude / "plain"
    plain.mkdir(parents=True)
    (plain / "SKILL.md").write_text("没有 frontmatter 的文件\n", encoding="utf-8")
    cands = scan_computer_skills([("Claude Code", claude)], existing=set())
    assert [c["name"] for c in cands] == ["plain"]
    assert cands[0]["description"] == "没有 frontmatter 的文件"


def test_local_sources_are_home_relative():
    # backend 按这个约定 expanduser；写死绝对路径或空路径都是错的
    assert LOCAL_SKILL_SOURCES, "至少要有一个本机扫描来源"
    for label, path in LOCAL_SKILL_SOURCES:
        assert label
        assert path.startswith("~/")
        assert path.endswith("/skills")


def test_scan_local_ws_roundtrip(home, monkeypatch):
    # 真实 Path.home() 因机器而异（本机可能真装着别家技能），扫过的目录必须指向临时区
    fake_claude = home / "claude-skills"
    make_skill_dir(fake_claude, "borrowed-skill", "从别家扫来的")
    monkeypatch.setattr(
        "skysheep.server.backend.LOCAL_SKILL_SOURCES", [("Claude Code", str(fake_claude))]
    )
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s", "method": "skills.scan_local", "params": {}})
        r = recv_until(ws, "s")["result"]
        assert len(r["candidates"]) == 1
        cand = r["candidates"][0]
        assert cand["name"] == "borrowed-skill"
        assert cand["origin"] == "Claude Code"
        assert not cand["installed"]

        # 扫描只读：候选原地未动；导入走既有 skills.install，再扫就标 installed
        assert (fake_claude / "borrowed-skill" / "SKILL.md").is_file()
        ws.send_json({"id": "i", "method": "skills.install",
                      "params": {"source": cand["path"], "scope": "global"}})
        assert recv_until(ws, "i")["result"]["installed"] == ["borrowed-skill"]
        ws.send_json({"id": "s2", "method": "skills.scan_local", "params": {}})
        r2 = recv_until(ws, "s2")["result"]
        assert r2["candidates"][0]["installed"] is True
