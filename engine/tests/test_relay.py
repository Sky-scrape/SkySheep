"""接力配置模型：固定槽位、可配置顺序、严格能力边界。"""
import pytest
from pydantic import ValidationError

from skysheep.core.relay import RelayConfig, RelayWorkflow


def test_default_is_serial_draft_not_execution():
    cfg = RelayConfig()
    assert set(cfg.slots) == {"A", "B"}
    assert all(s.execution_mode == "propose" and not s.capabilities for s in cfg.slots.values())
    assert RelayConfig.model_validate(cfg.model_dump()) == cfg


@pytest.mark.parametrize("slots", [["A", "B"], ["B", "A"], ["A", "B", "A"]])
def test_order_is_configurable(slots):
    nodes = [{"id": f"n{i}", "slot": slot, "prompt": "处理资料", "terminal": i == len(slots)-1}
             for i, slot in enumerate(slots)]
    edges = [{"source": f"n{i}", "target": f"n{i+1}"} for i in range(len(slots)-1)]
    graph = RelayWorkflow.model_validate({"start_node": "n0", "nodes": nodes, "edges": edges})
    assert [n.slot for n in graph.nodes] == slots


@pytest.mark.parametrize("change", [
    lambda c: c["slots"].pop("B"),
    lambda c: c["slots"].update(C=c["slots"]["A"]),
    lambda c: c["slots"]["A"].update(role=" "),
    lambda c: c["slots"]["A"].update(provider="fake", model=""),
    lambda c: c["slots"]["B"].update(execution_mode="execute"),
    lambda c: c["slots"]["B"].update(capabilities=["run_command"]),
    lambda c: c["slots"]["B"].update(context_scope="files"),
    lambda c: c.update(handoff_ratio=0.99),
    lambda c: c.update(recent_tokens=9000),
    lambda c: c.update(phase_timeout_seconds=500, handoff_timeout_seconds=100),
    lambda c: c.update(revision=True),
    lambda c: c.update(unknown_option=True),
    lambda c: c["workflow"].update(max_steps=1),
    lambda c: c["workflow"].update(max_retries=0),
    lambda c: c["workflow"].update(termination_condition="forever"),
    lambda c: c["workflow"].update(start_node="missing"),
    lambda c: c["workflow"]["nodes"][1].update(terminal=False),
    lambda c: c["workflow"]["nodes"][1].update(id="research"),
    lambda c: c["workflow"]["edges"][0].update(target="missing"),
    lambda c: c["workflow"]["edges"][0].update(require_confirmation=False),
    lambda c: c["workflow"]["edges"].append({"source": "research", "target": "research"}),
    lambda c: c["workflow"]["nodes"].append({"id": "orphan", "slot": "A", "prompt": "资料"}),
])
def test_invalid_or_unsupported_config_rejected(change):
    data = RelayConfig().model_dump()
    change(data)
    with pytest.raises(ValidationError):
        RelayConfig.model_validate(data)


def test_cycle_rejected_even_with_budget():
    with pytest.raises(ValidationError, match="循环"):
        RelayWorkflow.model_validate({
            "start_node": "a",
            "nodes": [{"id": "a", "slot": "A", "prompt": "a"},
                      {"id": "b", "slot": "B", "prompt": "b"}],
            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
        })
