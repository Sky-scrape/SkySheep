"""ToolRegistry.subset 的单元测试。

协作模板的工具表裁剪靠它把「全量注册表」缩成「模板声明的可见工具集」。
这里钉住四条行为：保持注册顺序、未知名字忽略、返回新表不动原表、schema
缓存互不串台（缓存是注册表实例级字段，两表各算各的）。
"""

from __future__ import annotations

from pydantic import BaseModel

from skysheep.tools.base import Safety, Tool, ToolContext, ToolRegistry


class _EmptyArgs(BaseModel):
    pass


def _make_tool(name: str, safety: Safety = Safety.READONLY) -> Tool:
    """造一个只有名字与分级的空壳工具（不执行，只参与注册与 schema 导出）。"""

    class _Dummy(Tool):
        async def run(self, args: BaseModel, ctx: ToolContext) -> str:  # pragma: no cover
            return name

    tool = _Dummy()
    tool.name = name
    tool.description = f"测试工具 {name}"
    tool.safety = safety
    tool.args_model = _EmptyArgs
    return tool


def _registry(*names: str) -> ToolRegistry:
    return ToolRegistry([_make_tool(n) for n in names])


def test_subset_preserves_registration_order():
    """子集按原表注册顺序返回，与 names 的传入顺序无关。"""
    reg = _registry("a", "b", "c", "d")
    sub = reg.subset(["d", "b"])
    assert isinstance(sub, ToolRegistry)
    assert sub is not reg
    assert [t.name for t in sub.all()] == ["b", "d"]


def test_subset_ignores_unknown_names():
    """未知名字直接忽略（MCP / 技能工具可能临时不可用，不能整轮拒跑）。"""
    reg = _registry("read_file", "write_file")
    sub = reg.subset(["write_file", "not_installed_tool"])
    assert [t.name for t in sub.all()] == ["write_file"]
    assert len(sub) == 1


def test_subset_empty_or_none_yields_empty_registry():
    reg = _registry("a", "b")
    for names in ([], None):
        sub = reg.subset(names)  # type: ignore[arg-type]
        assert len(sub) == 0
        assert sub.all() == []
        assert sub.schemas() == []


def test_subset_does_not_mutate_source_registry():
    """新表的注册不影响原表（反向也一样）。"""
    reg = _registry("a", "b", "c")
    sub = reg.subset(["a"])
    sub.register(_make_tool("z"))
    assert [t.name for t in sub.all()] == ["a", "z"]
    assert [t.name for t in reg.all()] == ["a", "b", "c"]
    assert reg.get("z") is None


def test_subset_schema_caches_are_independent():
    reg = _registry("a", "b")
    sub = reg.subset(["b"])
    assert [s["name"] for s in sub.schemas()] == ["b"]
    assert [s["name"] for s in reg.schemas()] == ["a", "b"]
    # 子表后续注册只失效自己的缓存，原表快照不受影响
    sub.register(_make_tool("c"))
    assert [s["name"] for s in sub.schemas()] == ["b", "c"]
    assert [s["name"] for s in reg.schemas()] == ["a", "b"]


def test_subset_duplicate_names_in_argument_do_not_raise():
    """names 里重复写同一个名字不触发 register 的重名异常。"""
    reg = _registry("a", "b")
    sub = reg.subset(["a", "a", "a"])
    assert [t.name for t in sub.all()] == ["a"]


def test_subset_of_subset():
    reg = _registry("a", "b", "c")
    assert [t.name for t in reg.subset(["a", "b"]).subset(["b"]).all()] == ["b"]
