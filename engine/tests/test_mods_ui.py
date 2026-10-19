"""Mods 元素树校验（P3）测试。"""

from __future__ import annotations

from skysheep.core.mods_ui import normalize_ui_tree


def test_box_with_children():
    raw = {
        "type": "box", "direction": "row", "children": [
            {"type": "text", "text": "hello"},
            {"type": "button", "id": "btn1", "label": "Click"},
        ],
    }
    out = normalize_ui_tree(raw)
    assert out["type"] == "box"
    assert out["direction"] == "row"
    assert len(out["children"]) == 2
    assert out["children"][0] == {"type": "text", "text": "hello", "tone": "normal"}
    assert out["children"][1]["id"] == "btn1"


def test_unknown_type_dropped():
    assert normalize_ui_tree({"type": "script", "src": "x"}) is None


def test_depth_limit():
    node = {"type": "text", "text": "leaf"}
    for _ in range(12):
        node = {"type": "box", "children": [node]}
    out = normalize_ui_tree(node)
    assert out is not None
    depth = 0
    cur = out
    while cur.get("children"):
        depth += 1
        cur = cur["children"][0]
        if cur.get("type") != "box":
            break
    assert depth <= 9


def test_children_cap():
    raw = {"type": "box", "children": [{"type": "text", "text": str(i)} for i in range(100)]}
    out = normalize_ui_tree(raw)
    assert len(out["children"]) <= 50


def test_duplicate_id_dropped():
    raw = {"type": "box", "children": [
        {"type": "button", "id": "x", "label": "a"},
        {"type": "button", "id": "x", "label": "b"},
    ]}
    out = normalize_ui_tree(raw)
    assert len(out["children"]) == 1


def test_invalid_id_dropped():
    assert normalize_ui_tree({"type": "button", "id": "a b", "label": "x"}) is None
    assert normalize_ui_tree({"type": "button", "id": "", "label": "x"}) is None
    assert normalize_ui_tree({"type": "button", "id": "a/b", "label": "x"}) is None


def test_image_src_schemes():
    assert normalize_ui_tree({"type": "image", "src": "javascript:alert(1)"}) is None
    assert normalize_ui_tree({"type": "image", "src": "vbscript:x"}) is None
    assert normalize_ui_tree({"type": "image", "src": "file:///etc/passwd"}) is None
    ok = normalize_ui_tree({"type": "image", "src": "https://a.com/x.png", "alt": "x"})
    assert ok is not None and ok["src"].startswith("https://")
    data = normalize_ui_tree({"type": "image", "src": "data:image/png;base64,iVBOR"})
    assert data is not None


def test_text_whitespace_collapsed_and_truncated():
    out = normalize_ui_tree({"type": "text", "text": "a\n\n  b   c"})
    assert out["text"] == "a b c"
    long = normalize_ui_tree({"type": "text", "text": "x" * 1000})
    assert len(long["text"]) <= 501


def test_tone_whitelist():
    out = normalize_ui_tree({"type": "text", "text": "x", "tone": "rainbow"})
    assert out["tone"] == "normal"
    out = normalize_ui_tree({"type": "text", "text": "x", "tone": "danger"})
    assert out["tone"] == "danger"


def test_select_requires_options():
    assert normalize_ui_tree({"type": "select", "id": "s", "options": []}) is None
    ok = normalize_ui_tree({"type": "select", "id": "s", "options": [
        {"value": "a", "label": "A"},
    ]})
    assert ok is not None and ok["options"][0]["value"] == "a"


def test_button_style_whitelist():
    out = normalize_ui_tree({"type": "button", "id": "b", "label": "x", "style": "evil"})
    assert out["style"] == "normal"
    out = normalize_ui_tree({"type": "button", "id": "b", "label": "x", "style": "primary"})
    assert out["style"] == "primary"
