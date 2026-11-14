"""协作模板后端 API（本期）：模板册 / 预览 / chat.send 路由 / 团队授权。

覆盖范围：
- collab.template_list / save / remove / preview 的 WS 口径与中文错误；
- 跨册重名、内置名保留、注册表感知校验（read 角色不能列写工具等）；
- chat.send 的 template 参数（与三个布尔互斥、模板优先）与 grant 参数；
- 团队模板的 needs/tools/budget 可选字段落盘与旧调用兼容；
- 模板预算（max_calls）在圆桌链路上的如实收尾。

平台与数据隔离：全部走 home 夹具（SKYSHEEP_HOME 指向 tmp），provider 用
FakeProvider；不触真实 ~/.skysheep，不发任何网络请求。测试服务的 Key 从
环境变量读取（_TEST_PROVIDER_ENV），源码里不写任何可用凭据字面量。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from skysheep.config import ProviderConfig
from skysheep.core.collab_templates import CollabRole, CollabTemplate
from skysheep.core.team import TeamTemplate, TeamTemplateMember
from skysheep.messages import TextBlock
from skysheep.models.base import Provider
from skysheep.models.fake import FakeProvider
from skysheep.security.gate import GrantedGate
from skysheep.server import app as server_app
from skysheep.server import create_app
from skysheep.server.backend import ServerBackend, _CollabTeamTemplate

# 单测专用环境变量名（值由本机环境提供；缺省时 _configure_providers 跳过，
# 依赖已配置服务的用例会显式断言前置条件）。不写任何凭据字面量。
_TEST_PROVIDER_ENV = "SKYSHEEP_TEST_PROVIDER_KEY"


def recv_until(ws, wanted_id=None, events=None):
    while True:
        frame = ws.receive_json()
        if "event" in frame:
            if events is not None:
                events.append(frame)
            continue
        if wanted_id is None or frame.get("id") == wanted_id:
            return frame


def make_client(home, scripts):
    """scripts 顺序 = provider_factory 调用顺序（第 1 个是主模型）。"""
    pool = [p if isinstance(p, Provider) else FakeProvider(p) for p in scripts]
    app = create_app(
        working_dir=home / "proj",
        provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    return TestClient(app)


def _configure_providers(backend: ServerBackend, names: list[str]) -> None:
    """给隔离配置补上「已配置 Key」的服务（provider_factory 仍负责真实构建）。

    api_key 留空、env_key 指向测试环境变量：resolve_api_key 会从环境读，
    源码与测试文件里都不出现可用凭据字面量（安全约束）。
    """
    for name in names:
        backend.cfg.providers[name] = ProviderConfig(
            env_key=_TEST_PROVIDER_ENV, model=f"model-{name}"
        )


def _roundtable_template(name="我的圆桌", provider="fa", **overrides) -> dict:
    payload = {
        "name": name,
        "kind": "roundtable",
        "desc": "测试用圆桌模板",
        "roles": [
            {"key": "critic", "name": "批评者", "provider": provider,
             "prompt": "你负责挑错。"},
            {"key": "factcheck", "name": "事实核查员", "provider": provider,
             "prompt": "你负责核对事实。"},
            {"key": "practitioner", "name": "实干者", "provider": provider,
             "prompt": "你负责给出可落地步骤。"},
        ],
        "aggregator": {"provider": "agg", "model": "model-agg", "prompt": ""},
        "limits": {"debate_rounds": 1},
        "budget": {"max_calls": 8, "max_seconds": 900},
    }
    payload.update(overrides)
    return payload


def _team_template(name="交付小组", needs="write", tools=None) -> dict:
    return {
        "name": name,
        "kind": "team",
        "desc": "测试用团队模板",
        "roles": [
            {"key": "builder", "name": "实施者", "provider": "fa",
             "model": "model-fa", "prompt": "你负责实现。", "needs": needs,
             "tools_allow": tools if tools is not None else [
                 "read_file", "write_file", "run_command:git status",
             ]},
            {"key": "reviewer", "name": "验收员", "provider": "fb",
             "model": "model-fb", "prompt": "你负责验收。", "needs": needs,
             "tools_allow": tools if tools is not None else [
                 "read_file", "list_dir",
             ]},
        ],
        "aggregator": {"provider": "", "model": "", "prompt": ""},
        "budget": {"max_calls": 5, "max_seconds": 100},
    }


@pytest.fixture(autouse=True)
def _test_provider_key(monkeypatch):
    """给「已配置服务」的用例提供测试 Key（只经环境变量注入）。"""
    monkeypatch.setenv(_TEST_PROVIDER_ENV, "skysheep-test-only")


# ---------- 模板册 ----------


def test_collab_template_list_builtin_and_user(home):
    """内置三张齐备（kind 覆盖三种），用户模板带 source=collab。"""
    with make_client(home, []) as client:
        backend = client.app.state.backend
        backend.collab_templates.upsert(CollabTemplate(
            name="我的圆桌", kind="roundtable", builtin=False,
            roles=[CollabRole(key="a", name="甲", prompt="只读审查")],
        ))
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "l1", "method": "collab.template_list", "params": {}})
            frame = recv_until(ws, "l1")
    assert frame["ok"], frame
    templates = frame["result"]["templates"]
    names = [t["name"] for t in templates]
    assert names[:3] == ["圆桌·多模型评审", "团队·交付小组", "对抗·代码审查"]
    assert {t["kind"] for t in templates[:3]} == {"roundtable", "team", "adversarial"}
    assert all(t["builtin"] for t in templates[:3])
    assert templates[3]["name"] == "我的圆桌" and templates[3]["source"] == "collab"
    assert frame["result"]["team_templates"] == []
    assert frame["result"]["skipped"] == 0


def test_collab_template_save_roundtable_roundtrip(home):
    """保存圆桌模板 → 落 collab_templates.json → 重开加载一致。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
            "template": _roundtable_template(),
        }})
        s1 = recv_until(ws, "s1")
        assert s1["ok"], s1
        assert s1["result"]["source"] == "collab"
        assert s1["result"]["template"]["name"] == "我的圆桌"
        path = home / "home" / "collab_templates.json"
        assert path.exists()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert [t["name"] for t in on_disk["templates"]] == ["我的圆桌"]

        ws.send_json({"id": "l1", "method": "collab.template_list", "params": {}})
        l1 = recv_until(ws, "l1")
    assert [t["name"] for t in l1["result"]["templates"]][-1] == "我的圆桌"


def test_collab_template_save_team_goes_to_teams_json(home):
    """kind=team 落 teams.json（协作册不出现该条），并保留 needs/tools/budget。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
            "template": _team_template(),
        }})
        s1 = recv_until(ws, "s1")
        assert s1["ok"], s1
        assert s1["result"]["source"] == "team"
        teams_json = home / "home" / "teams.json"
        on_disk = json.loads(teams_json.read_text(encoding="utf-8"))["templates"]
        assert on_disk[0]["name"] == "交付小组"
        assert on_disk[0]["needs"] == "write"
        assert "run_command:git status" in on_disk[0]["tools_allow"]
        assert on_disk[0]["budget"]["max_calls"] == 5
        collab_json = home / "home" / "collab_templates.json"
        assert not collab_json.exists() or not json.loads(
            collab_json.read_text(encoding="utf-8")
        )["templates"]

        ws.send_json({"id": "l1", "method": "collab.template_list", "params": {}})
        l1 = recv_until(ws, "l1")
    team_names = [t["name"] for t in l1["result"]["team_templates"]]
    assert team_names == ["交付小组"]
    assert l1["result"]["team_templates"][0]["source"] == "team"
    assert l1["result"]["team_templates"][0]["roles"][0]["needs"] == "write"


def test_collab_template_save_team_keeps_desc(home):
    """kind=team 模板的说明（desc）随 teams.json 落盘并在回读时还原（collab-1）。

    此前 _collab_to_team_template 不带 desc、_team_template_to_collab 硬编码
    空串：用户写的说明保存后静默丢失，画廊卡变成占位文案。
    """
    payload = _team_template()
    payload["desc"] = "走查用：这条说明保存后不能丢"
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "collab.template_save",
                      "params": {"template": payload}})
        assert recv_until(ws, "s1")["ok"]
        # 落 teams.json 的条目带 desc
        teams_json = home / "home" / "teams.json"
        on_disk = json.loads(teams_json.read_text(encoding="utf-8"))["templates"]
        assert on_disk[0]["desc"] == "走查用：这条说明保存后不能丢"
        # 列表回读（画廊数据源）desc 是真值而非硬编码空串
        ws.send_json({"id": "l1", "method": "collab.template_list", "params": {}})
        l1 = recv_until(ws, "l1")
        team_rows = [t for t in l1["result"]["team_templates"]
                     if t.get("name") == "交付小组"]
        assert team_rows and team_rows[0]["desc"] == "走查用：这条说明保存后不能丢"


def test_collab_template_save_rejects_roundtable_write_needs(home):
    """圆桌/对抗本期是纯文本协作：needs=write 保存期拒绝（中文原文）。"""
    payload = _roundtable_template()
    payload["roles"][0]["needs"] = "write"
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
            "template": payload,
        }})
        frame = recv_until(ws, "s1")
    assert not frame["ok"]
    assert "只能是只读" in frame["error"]


def test_collab_template_save_registry_aware_checks(home):
    """read 角色列写工具 / write 角色列 DANGEROUS → 拒绝；未知工具只 warning。"""
    team_read = _team_template(name="只读写文件", needs="read", tools=["write_file"])
    team_write_exec = _team_template(
        name="写角色删除", needs="write", tools=["read_file", "delete_file"],
    )
    team_unknown = _team_template(
        name="未知工具", needs="read", tools=["read_file", "不存在的工具"],
    )
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "e1", "method": "collab.template_save", "params": {
            "template": team_read,
        }})
        e1 = recv_until(ws, "e1")
        ws.send_json({"id": "e2", "method": "collab.template_save", "params": {
            "template": team_write_exec,
        }})
        e2 = recv_until(ws, "e2")
        ws.send_json({"id": "s3", "method": "collab.template_save", "params": {
            "template": team_unknown,
        }})
        s3 = recv_until(ws, "s3")
    assert not e1["ok"] and "声明只读" in e1["error"]
    assert not e2["ok"] and "不能列执行类工具" in e2["error"]
    assert s3["ok"], s3
    assert any("当前工具表里不存在" in w for w in s3["result"]["warnings"])


def test_collab_template_save_rejects_cross_book_duplicate(home):
    """同名模板不能同时存在于两个册（先存团队，再存圆桌 → 拒绝）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
            "template": _team_template(name="同名"),
        }})
        assert recv_until(ws, "s1")["ok"]
        ws.send_json({"id": "s2", "method": "collab.template_save", "params": {
            "template": _roundtable_template(name="同名"),
        }})
        s2 = recv_until(ws, "s2")
    assert not s2["ok"]
    assert "已有同名的团队模板" in s2["error"]


def test_collab_template_remove_builtin_and_ambiguous(home):
    """内置不可删；两册同名未给 source 拒绝，给了 source 删对册。"""
    with make_client(home, []) as client:
        backend = client.app.state.backend
        backend.collab_templates.upsert(CollabTemplate(
            name="双册同名", kind="roundtable", builtin=False,
            roles=[CollabRole(key="a", name="甲")],
        ))
        backend.team_templates.upsert(TeamTemplate(
            name="双册同名", members=[TeamTemplateMember(provider="fa", model="ma")],
        ))
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "e1", "method": "collab.template_remove", "params": {
                "name": "圆桌·多模型评审",
            }})
            e1 = recv_until(ws, "e1")
            ws.send_json({"id": "e2", "method": "collab.template_remove", "params": {
                "name": "双册同名",
            }})
            e2 = recv_until(ws, "e2")
            ws.send_json({"id": "d1", "method": "collab.template_remove", "params": {
                "name": "双册同名", "source": "collab",
            }})
            d1 = recv_until(ws, "d1")
    assert not e1["ok"] and "内置模板不能删除" in e1["error"]
    assert not e2["ok"] and "请指明来源" in e2["error"]
    assert d1["ok"] and d1["result"]["source"] == "collab"


def test_collab_template_save_remote_is_rejected(home):
    """模板册写面仅本机（local_only）：远程连接拿不到写入口。"""
    entry = server_app._WS_METHODS["collab.template_save"]
    assert entry.local_only is True
    assert server_app._WS_METHODS["collab.template_remove"].local_only is True
    assert server_app._WS_METHODS["collab.template_list"].local_only is False
    assert server_app._WS_METHODS["collab.preview"].local_only is False


# ---------- 预览 ----------


def test_collab_preview_team_fields_and_tools(home):
    """团队模板预览：角色/工具/命令前缀/口径/预算与有效人数。"""
    with make_client(home, []) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": _team_template(needs="exec", tools=[
                    "read_file", "write_file", "delete_file",
                    "run_command:git status",
                ]),
            }})
            assert recv_until(ws, "s1")["ok"]
            ws.send_json({"id": "p1", "method": "collab.preview", "params": {
                "name": "交付小组",
            }})
            p1 = recv_until(ws, "p1")
    assert p1["ok"], p1
    result = p1["result"]
    assert result["kind"] == "team" and result["source"] == "team"
    assert result["needs"] == "exec"
    assert result["roles_declared"] == 2
    assert result["effective_member_count"] == 2
    assert result["max_calls"] == 5 and result["max_seconds"] == 100
    assert result["role_scoped"] is False and "不做逐成员隔离" in result["role_scope_note"]
    by_name = {(t["name"], t["command_prefix"]): t for t in result["tools"]}
    run_cmd = by_name[("run_command", "git status")]
    assert run_cmd["known"] is True and run_cmd["mode"] == "auto"
    assert run_cmd["grant_dependent"] is True
    assert "未勾选免确认" in run_cmd["note_ask"]
    assert result["command_prefixes"] == ["git status"]
    write_file = by_name[("write_file", "")]
    assert write_file["mode"] == "auto"
    assert write_file["grant_dependent"] is True
    assert "未勾选免确认" in write_file["note_ask"]
    # 只读工具的自动放行与跑前清单复选框无关：不参与勾选联动
    read_file = by_name[("read_file", "")]
    assert read_file["mode"] == "auto" and read_file["grant_dependent"] is False
    assert "note_ask" not in read_file
    assert by_name[("delete_file", "")]["mode"] == "confirm"
    assert "delete_file" in result["always_confirm"]


def test_collab_preview_read_needs_downgrades_write_tools(home):
    """团队 needs=read 时写工具在预览里标 confirm（运行时不挂写工具）。

    这种模板无法经保存期校验（read 角色列写工具会被拒），模拟手改 teams.json：
    直接往模板册塞一条，验证预览不误导用户。
    """
    with make_client(home, []) as client:
        backend = client.app.state.backend
        backend.team_templates.upsert(_CollabTeamTemplate(
            name="手改只读", needs="read",
            members=[TeamTemplateMember(provider="fa", model="ma", name="甲")],
            tools_allow=["read_file", "write_file"],
        ))
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "p1", "method": "collab.preview", "params": {
                "name": "手改只读",
            }})
            p1 = recv_until(ws, "p1")
    assert p1["ok"], p1
    by_name = {t["name"]: t for t in p1["result"]["tools"]}
    assert by_name["write_file"]["mode"] == "confirm"
    assert "needs=read" in by_name["write_file"]["note"]


def test_collab_preview_roundtable_calls_and_pricing(home):
    """圆桌预览：needs 全 read、tools 空、调用数=角色×(1+辩论)+1、费用按价目估算。"""
    with make_client(home, []) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "agg"])
        backend.cfg.usage_pricing.price_in = 1.0
        backend.cfg.usage_pricing.price_out = 2.0
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": _roundtable_template(),
            }})
            assert recv_until(ws, "s1")["ok"]
            ws.send_json({"id": "p1", "method": "collab.preview", "params": {
                "name": "我的圆桌",
            }})
            p1 = recv_until(ws, "p1")
    assert p1["ok"], p1
    result = p1["result"]
    assert result["needs"] == "read" and result["tools"] == []
    assert result["estimated_calls"] == 3 * (1 + 1) + 1
    assert result["priced"] is True
    assert result["cost_reason"] == "ok"
    assert result["estimated_cost"] > 0
    assert result["currency"] == "CNY"
    assert result["can_resident"] is False


def test_collab_preview_pricing_reasons(home):
    """无价目 → no_price；角色未声明服务 → unknown_provider（都不编 0 元）。"""
    with make_client(home, []) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "agg"])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": _roundtable_template(name="有服务无价目"),
            }})
            assert recv_until(ws, "s1")["ok"]
            no_provider = _roundtable_template(name="有角色未指定")
            no_provider["roles"][1]["provider"] = ""
            ws.send_json({"id": "s2", "method": "collab.template_save", "params": {
                "template": no_provider,
            }})
            assert recv_until(ws, "s2")["ok"]
            ws.send_json({"id": "p1", "method": "collab.preview", "params": {
                "name": "有服务无价目",
            }})
            p1 = recv_until(ws, "p1")
            ws.send_json({"id": "p2", "method": "collab.preview", "params": {
                "name": "有角色未指定",
            }})
            p2 = recv_until(ws, "p2")
    assert p1["result"]["cost_reason"] == "no_price"
    assert p1["result"]["estimated_cost"] is None and p1["result"]["priced"] is False
    assert p2["result"]["cost_reason"] == "unknown_provider"
    assert p2["result"]["estimated_cost"] is None


def test_collab_template_load_skips_bad_entries(home):
    """手改坏条目：load 跳过并计 skipped，好条目照常进列表（不进运行期）。"""
    path = home / "home" / "collab_templates.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"templates": [
        {"name": "坏类型", "kind": "x", "roles": [{"key": "a", "name": "甲"}]},
        {"name": "坏 needs", "kind": "team",
         "roles": [{"key": "a", "name": "甲", "needs": "root"}]},
        {"name": "好的圆桌", "kind": "roundtable",
         "roles": [{"key": "a", "name": "甲"}]},
    ]}, ensure_ascii=False), encoding="utf-8")
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "l1", "method": "collab.template_list", "params": {}})
        frame = recv_until(ws, "l1")
    assert frame["ok"], frame
    names = [t["name"] for t in frame["result"]["templates"]]
    assert "好的圆桌" in names
    assert "坏类型" not in names and "坏 needs" not in names
    assert frame["result"]["skipped"] == 2


def test_collab_preview_adversarial_with_one_role(home):
    """对抗模板角色不足三个：预览不越界，有效角色数=1、调用数=1+1。"""
    with make_client(home, []) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "judge-x"])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": {
                    "name": "单角色对抗", "kind": "adversarial",
                    "roles": [{"key": "finder", "name": "发现者",
                               "provider": "fa", "prompt": "找问题"}],
                    "aggregator": {"provider": "judge-x", "model": "m"},
                },
            }})
            assert recv_until(ws, "s1")["ok"]
            ws.send_json({"id": "p1", "method": "collab.preview", "params": {
                "name": "单角色对抗",
            }})
            p1 = recv_until(ws, "p1")
    assert p1["ok"], p1
    assert p1["result"]["effective_member_count"] == 1
    assert p1["result"]["estimated_calls"] == 2


def test_collab_preview_unknown_name(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "p1", "method": "collab.preview", "params": {
            "name": "没有这个模板",
        }})
        frame = recv_until(ws, "p1")
    assert not frame["ok"] and "找不到协作模板" in frame["error"]


# ---------- chat.send 路由 ----------


def test_chat_send_template_roundtable_overrides_and_meta(home):
    """模板圆桌：成员 provider/model/prompt 覆盖生效、事件带 name、meta 带模板名。"""
    main = FakeProvider([[TextBlock(text="主模型不参与")]])
    agg = FakeProvider([[TextBlock(text="模板收口融合答案")]])
    # 模板 limits.debate_rounds=1：每位成员两轮作答（独立 + 辩论修订），
    # 脚本各给两份，否则第二轮回落到空脚本、草稿被清空
    members = [
        FakeProvider([[TextBlock(text="批评者草稿")], [TextBlock(text="批评者修订")]]),
        FakeProvider([[TextBlock(text="核查员草稿")], [TextBlock(text="核查员修订")]]),
        FakeProvider([[TextBlock(text="实干者草稿")], [TextBlock(text="实干者修订")]]),
    ]
    pool = [main, agg, *members]
    app = create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    with TestClient(app) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "agg"])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": _roundtable_template(),
            }})
            assert recv_until(ws, "s1")["ok"]
            events: list = []
            ws.send_json({"id": "r1", "method": "chat.send", "params": {
                "text": "评审这段设计",
                "template": "我的圆桌",
                # 模板优先：同给团队布尔也不报互斥
                "team": True,
            }})
            frame = recv_until(ws, "r1", events)
        result = frame["result"]
        assert frame["ok"], frame
        assert result["roundtable"] and result["team"] is None
        rt = result["roundtable"]
        assert rt["template"] == "我的圆桌" and rt["template_kind"] == "roundtable"
        assert [m["name"] for m in rt["members"]] == ["批评者", "事实核查员", "实干者"]
        started = next(e for e in events if e["event"] == "roundtable_started")
        assert [m["name"] for m in started["data"]["members"]] == [
            "批评者", "事实核查员", "实干者",
        ]
        # 模板成员人设进了各自的 system prompt
        assert "你负责挑错" in members[0].calls[0][0].text
        assert "你负责核对事实" in members[1].calls[0][0].text
        # 收口由 aggregator 指定（主模型没被调用）
        assert agg.calls, "聚合主席应被调用"
        assert main.calls == []


def test_chat_send_template_adversarial_uses_aggregator_judge(home):
    """对抗模板：角色人设生效，裁判由 aggregator 指定（不是当前主模型）。"""
    main = FakeProvider([[TextBlock(text="主模型不参与")]])
    judge = FakeProvider([[TextBlock(text="不该走到裁判")]])
    finder = FakeProvider([[TextBlock(text="不是 JSON")], [TextBlock(text="仍不是 JSON")]])
    investigator = FakeProvider([[TextBlock(text="{}")]])
    advisor = FakeProvider([[TextBlock(text="{}")]])
    pool = [main, judge, finder, investigator, advisor]
    app = create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    template = {
        "name": "模板对抗", "kind": "adversarial",
        "roles": [
            {"key": "finder", "name": "发现者", "provider": "fa",
             "prompt": "你负责穷尽式找问题。"},
            {"key": "investigator", "name": "调查员", "provider": "fb",
             "prompt": "你负责推翻误报。"},
            {"key": "advisor", "name": "建议者", "provider": "fc",
             "prompt": "你负责给方案。"},
        ],
        "aggregator": {"provider": "judge-x", "model": "model-judge",
                       "prompt": "你负责终审。"},
        "budget": {"max_calls": 6},
    }
    with TestClient(app) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "fb", "fc", "judge-x"])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": template,
            }})
            assert recv_until(ws, "s1")["ok"]
            events: list = []
            ws.send_json({"id": "r1", "method": "chat.send", "params": {
                "text": "审查这段代码", "template": "模板对抗",
            }})
            frame = recv_until(ws, "r1", events)
        assert frame["ok"], frame
        started = next(e for e in events if e["event"] == "adversarial_started")
        roles = {r["role"]: r for r in started["data"]["roles"]}
        assert roles["judge"]["provider"] == "judge-x"
        assert roles["finder"]["provider"] == "fa"
        assert "你负责穷尽式找问题" in finder.calls[0][0].text
        assert judge.calls == []  # 发现阶段就失败，裁判没被调用
        adv = frame["result"]["adversarial"]
        assert adv["template"] == "模板对抗"
        assert {r["role"]: r for r in adv["roles"]}["finder"]["name"] == "发现者"


def test_chat_send_template_budget_stops_and_keeps_drafts(home):
    """模板预算 max_calls：超限如实收尾（status=error、预算原因、草稿保留）。"""
    main = FakeProvider([[TextBlock(text="主模型不参与")]])
    agg = FakeProvider([[TextBlock(text="不该走到融合")]])
    pool = [main, agg] + [FakeProvider([[TextBlock(text=f"草稿{i}")]])
                          for i in range(3)]
    app = create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    with TestClient(app) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "agg"])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": _roundtable_template(
                    name="预算圆桌", budget={"max_calls": 2, "max_seconds": 0},
                ),
            }})
            assert recv_until(ws, "s1")["ok"]
            events: list = []
            ws.send_json({"id": "r1", "method": "chat.send", "params": {
                "text": "评审", "template": "预算圆桌",
            }})
            frame = recv_until(ws, "r1", events)
        assert frame["ok"], frame
        rt = frame["result"]["roundtable"]
        assert rt["status"] == "error"
        assert rt["budget_exceeded"] == "calls"
        assert "调用次数上限" in rt["error"]
        # 已有草稿被保留（降级为对比式消息）
        draft_msgs = [
            e for e in events if e["event"] == "assistant_message"
            and (e["data"].get("message") or {}).get("roundtable", {}).get("mode")
            == "compare"
        ]
        assert draft_msgs, "预算超限时应保留已产出的成员草稿"
        assert agg.calls == [], "预算耗尽后不应再跑融合"


def test_chat_send_unknown_template_errors(home):
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "r1", "method": "chat.send", "params": {
            "text": "你好", "template": "根本没有",
        }})
        frame = recv_until(ws, "r1")
    assert not frame["ok"] and "找不到协作模板" in frame["error"]


def test_chat_send_team_template_roster_registry_and_grant(home):
    """团队模板：名册/人设来自模板；needs 裁剪工具表；grant=run 包 GrantedGate。"""
    main = FakeProvider([[TextBlock(text="主模型")]])
    member = FakeProvider([[TextBlock(text="实施完成")]])
    pool = [main, member]
    app = create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    with TestClient(app) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "fb"])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": _team_template(tools=["read_file", "write_file"]),
            }})
            assert recv_until(ws, "s1")["ok"]
            events: list = []
            ws.send_json({"id": "r1", "method": "chat.send", "params": {
                "text": "@实施者 开工", "template": "交付小组",
                "grant": {"scope": "run"},
            }})
            frame = recv_until(ws, "r1", events)
        assert frame["ok"], frame
        started = next(e for e in events if e["event"] == "team_started")
        assert [m["name"] for m in started["data"]["roster"]] == ["实施者", "验收员"]
        sid = frame["result"]["session_id"]
        orch = backend._teams[sid]
        assert isinstance(orch._gate, GrantedGate)
        assert orch._gate.allowed_tools == frozenset({"read_file", "write_file"})
        names = {t.name for t in orch._registry.all()}
        assert names == {"read_file", "write_file"}  # needs=write 滤掉 DANGEROUS
        assert frame["result"]["team"]["template"] == "交付小组"
        assert frame["result"]["team"]["template_kind"] == "team"


def test_chat_send_team_template_read_needs_registry_readonly(home):
    """needs=read 的团队模板（手改册）：执行型工具表只留 READONLY。"""
    main = FakeProvider([[TextBlock(text="主模型")]])
    pool = [main, FakeProvider([[TextBlock(text="成员")]])]
    app = create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    with TestClient(app) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa"])
        backend.team_templates.upsert(_CollabTeamTemplate(
            name="只读小组", needs="read",
            members=[TeamTemplateMember(provider="fa", model="model-fa", name="甲")],
            tools_allow=["read_file", "write_file", "delete_file"],
        ))
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "r1", "method": "chat.send", "params": {
                "text": "先看看现状", "template": "只读小组",
            }})
            frame = recv_until(ws, "r1")
        assert frame["ok"], frame
        orch = backend._teams[frame["result"]["session_id"]]
        names = {t.name for t in orch._registry.all()}
        assert names == {"read_file"}


def test_team_template_dedupes_roles_by_configured_services(home):
    """provider 留空的三角色 + 只配 1 个服务：预览/建队都如实回落到 1 人并注记。"""
    main = FakeProvider([[TextBlock(text="主模型")]])
    pool = [main, FakeProvider([[TextBlock(text="成员")]])]
    app = create_app(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    template = {
        "name": "三人小队", "kind": "team",
        "roles": [
            {"key": "a", "name": "甲", "needs": "read",
             "tools_allow": ["read_file"]},
            {"key": "b", "name": "乙", "needs": "read",
             "tools_allow": ["read_file"]},
            {"key": "c", "name": "丙", "needs": "read",
             "tools_allow": ["read_file"]},
        ],
        "budget": {"max_calls": 10},
    }
    with TestClient(app) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["only-one"])
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": template,
            }})
            assert recv_until(ws, "s1")["ok"]
            ws.send_json({"id": "p1", "method": "collab.preview", "params": {
                "name": "三人小队",
            }})
            preview = recv_until(ws, "p1")
            events: list = []
            ws.send_json({"id": "r1", "method": "chat.send", "params": {
                "text": "开工", "template": "三人小队",
            }})
            frame = recv_until(ws, "r1", events)
        assert frame["ok"], frame
        assert preview["result"]["effective_member_count"] == 1
        assert preview["result"]["dropped_roles"] == ["乙", "丙"]
        started = next(e for e in events if e["event"] == "team_started")
        assert [m["name"] for m in started["data"]["roster"]] == ["甲"]
        assert any(
            "去重后组成 1 名队员" in str((e.get("data") or {}).get("message") or "")
            for e in events if e["event"] == "notice"
        )


def test_chat_send_team_template_read_needs_rejects_write_tool(home):
    """needs=read 的团队模板列执行工具：保存期拒绝（不会建出超范围队伍）。"""
    with make_client(home, []) as client:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"id": "s1", "method": "collab.template_save", "params": {
                "template": _team_template(needs="read", tools=[
                    "read_file", "list_dir", "delete_file", "run_command:git status",
                ]),
            }})
            s1 = recv_until(ws, "s1")
        assert not s1["ok"] and "声明只读" in s1["error"]


@pytest.mark.asyncio
async def test_remote_grant_degrades_to_ask_each(home):
    """远程连接的 grant.scope=run 在服务端降级为 ask_each（不依赖前端自觉）。"""
    main = FakeProvider([[TextBlock(text="主模型")]])
    pool = [main, FakeProvider([[TextBlock(text="成员")]])]
    backend = ServerBackend(
        working_dir=home / "proj", provider_name="fake",
        provider_factory=lambda: pool.pop(0) if pool
        else FakeProvider([[TextBlock(text="备用")]]),
    )
    await backend.setup()
    try:
        _configure_providers(backend, ["fa", "fb"])
        await backend.collab_template_save({"template": _team_template()})
        emitted: list[dict] = []

        async def emit(ev: dict) -> None:
            emitted.append(ev)

        result = await backend.send(
            "@实施者 开工", emit, template="交付小组",
            grant={"scope": "run"}, local=False,
        )
        sid = result["session_id"]
        orch = backend._teams[sid]
        assert not isinstance(orch._gate, GrantedGate)
        assert any(
            "远程连接不能使用" in str(e.get("message") or "")
            for e in emitted if e.get("kind") == "notice"
        )
    finally:
        await backend.shutdown()


def test_team_template_save_keeps_optional_fields(home):
    """team.template_save 缺新字段时同名沿用旧值（旧前端保存不抹掉授权面）。"""
    with make_client(home, []) as client, client.websocket_connect("/ws") as ws:
        ws.send_json({"id": "s1", "method": "team.template_save", "params": {
            "name": "夜班小队", "members": [
                {"provider": "fa", "model": "ma", "name": "甲"},
            ], "needs": "write", "tools_allow": ["read_file", "write_file"],
            "budget": {"max_calls": 9, "max_seconds": 60},
        }})
        s1 = recv_until(ws, "s1")
        assert s1["ok"], s1
        assert s1["result"]["template"]["needs"] == "write"
        ws.send_json({"id": "s2", "method": "team.template_save", "params": {
            "name": "夜班小队", "members": [
                {"provider": "fa", "model": "ma", "name": "甲"},
            ],
        }})
        s2 = recv_until(ws, "s2")
        assert s2["ok"], s2
        assert s2["result"]["template"]["needs"] == "write"
        assert s2["result"]["template"]["tools_allow"] == ["read_file", "write_file"]
        assert s2["result"]["template"]["budget"]["max_calls"] == 9


def test_team_resident_set_rejects_builtin_template(home):
    """内置团队模板未落盘：登记常驻给可执行的中文错误（复制为我的模板）。"""
    with make_client(home, []) as client:
        backend = client.app.state.backend
        backend._teams["sid-x"] = SimpleNamespace(
            team_id="t1", source_template="团队·交付小组", director_mode="user",
        )

        async def run():
            with pytest.raises(Exception) as excinfo:
                await backend.team_resident_set({"session_id": "sid-x"})
            return str(excinfo.value)

        message = client.portal.call(run)
    assert "内置团队模板" in message and "复制" in message


def test_auto_pool_demotes_auth_failed_services(home):
    """自动挑模型（圆桌/对抗/团队自动成员）把鉴权失败的服务排到池尾。

    配置里留着一个坏 Key 时，自动池仍包含它（全都坏时要有错误卡），但
    有可用服务时不让它先上场——否则对抗发现者一挑到就整场 fail-fast。
    """
    with make_client(home, []) as client:
        backend = client.app.state.backend
        _configure_providers(backend, ["fa", "fb"])
        names = [n for n, _m in backend._auto_pool_services()]
        assert names.index("fa") < names.index("fb")
        backend.provider_health.record_text_error("fa", "401 Unauthorized: invalid api key")
        names = [n for n, _m in backend._auto_pool_services()]
        assert names.index("fb") < names.index("fa")
        # 恢复健康后回到配置顺序（稳定排序，不做永久降权）
        backend.provider_health.record_ok("fa")
        names = [n for n, _m in backend._auto_pool_services()]
        assert names.index("fa") < names.index("fb")
