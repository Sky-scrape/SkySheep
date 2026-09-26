# 原生文件选择桥（server/picker.py）回归：窗口未就绪 / 未知 kind / 各 kind 的
# 对话框参数派发。webview 只 import 不建窗，无需 GUI。
from skysheep.server.picker import FILE_FILTERS, FilePicker


class FakeWindow:
    """记录 create_file_dialog 调用参数的假窗口。"""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def create_file_dialog(self, dialog_type, **kw):
        self.calls.append({"dialog_type": dialog_type, **kw})
        return self.result


def test_pick_without_window_returns_friendly_error():
    """窗口还没 create_window 时 pick 不能崩，回空列表 + 中文说明让前端退回手动输入。"""
    r = FilePicker().pick("dir")
    assert r == {"paths": [], "error": "窗口尚未就绪，请稍后再试"}


def test_pick_unknown_kind_reports_error():
    p = FilePicker()
    p.attach(FakeWindow([]))
    r = p.pick("no-such-kind")
    assert r["paths"] == []
    assert "未知的选择类型" in r["error"]


def test_pick_dir_uses_folder_dialog():
    w = FakeWindow([r"C:\some\proj"])
    p = FilePicker()
    p.attach(w)
    r = p.pick("dir")
    assert r == {"paths": [r"C:\some\proj"]}
    assert len(w.calls) == 1
    assert "file_types" not in w.calls[0]  # 选文件夹不设文件过滤器


def test_pick_file_multi_select_and_filters():
    from pathlib import Path

    w = FakeWindow([Path(r"C:\a.txt")])
    p = FilePicker()
    p.attach(w)
    r = p.pick("file")
    assert r["paths"] == [str(w.result[0])]
    call = w.calls[0]
    assert call["allow_multiple"] is True
    assert call["file_types"] == FILE_FILTERS


def test_pick_skill_zip_uses_zip_filter():
    w = FakeWindow([])
    p = FilePicker()
    p.attach(w)
    r = p.pick("skill_zip")
    assert r == {"paths": []}  # 取消选择 → 空列表、无 error
    assert "zip" in w.calls[0]["file_types"][0]


def test_pick_json_uses_json_filter():
    w = FakeWindow([])
    p = FilePicker()
    p.attach(w)
    p.pick("json")
    assert "json" in w.calls[0]["file_types"][0].lower()


def test_dialog_failure_degrades_gracefully():
    """对话框后端不可用（如缺 GUI 环境）→ 回空列表 + 错误说明，不外抛。"""
    class BrokenWindow:
        def create_file_dialog(self, *a, **kw):
            raise RuntimeError("no backend")

    p = FilePicker()
    p.attach(BrokenWindow())
    r = p.pick("dir")
    assert r["paths"] == []
    assert "no backend" in r["error"]


def test_pick_converts_paths_to_str():
    """pywebview 返回的可能是 Path/其他对象，统一转 str 再回传前端。"""
    from pathlib import Path

    w = FakeWindow([Path(r"C:\x\y.md")])
    p = FilePicker()
    p.attach(w)
    assert p.pick("file")["paths"] == [r"C:\x\y.md"]
