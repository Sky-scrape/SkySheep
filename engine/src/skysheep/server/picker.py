"""桌面窗口的原生文件选择桥：设置页里点「选择文件夹 / 选择文件」时弹系统对话框。

    pywebview 会把 ``js_api`` 对象的方法暴露成 ``window.pywebview.api.<name>()``，
    前端 await 它就能拿到用户选中的本机路径。五种 kind：

        "dir"        选任意文件夹（工作项目切换用）
        "skill_dir"  选技能文件夹
        "skill_zip"  选技能压缩包（.zip）
        "json"       选 MCP 配置文件（.json）
        "file"       选任意文件（可多选；拖拽进窗口拿不到真实路径，靠它兜底）

    只在原生窗口里可用。浏览器模式下前端会自动退回「手动粘贴路径」的输入框，
    所以这里遇到任何异常都返回空列表 + 错误说明，不往外抛。
"""

from __future__ import annotations

# 常见文档 / 文本 / 代码，第一组是默认筛选项；用户随时可以切到「所有文件」
FILE_FILTERS = (
    "文档与表格 (*.pdf;*.docx;*.xlsx;*.xls;*.csv;*.txt;*.md;*.log)",
    "代码与配置 (*.py;*.js;*.ts;*.json;*.toml;*.yaml;*.yml;*.html;*.css;*.sql;*.sh)",
    "图片 (*.png;*.jpg;*.jpeg;*.webp;*.gif;*.bmp)",
    "所有文件 (*.*)",
)



def _dialog_constants() -> tuple[int, int]:
    """pywebview 新旧两套常量都兼容（新版是 FileDialog 枚举）。"""
    import webview

    dialog = getattr(webview, "FileDialog", None)
    if dialog is not None:
        return int(dialog.FOLDER), int(dialog.OPEN)
    return int(webview.FOLDER_DIALOG), int(webview.OPEN_DIALOG)


class FilePicker:
    """挂在 pywebview 的 js_api 上，供前端选择本机文件/文件夹。

    窗口在 create_window() 之后才知道，所以先把这个对象当 js_api 传进去，
    拿到窗口后再 attach()——js_api 的方法是在页面加载时才读取的。
    """

    def __init__(self) -> None:
        self._window = None

    def attach(self, window) -> None:
        self._window = window

    def pick(self, kind: str = "skill_dir") -> dict:
        """弹系统选择框；返回 {"paths": [...]}，取消则为空列表。"""
        if self._window is None:
            return {"paths": [], "error": "窗口尚未就绪，请稍后再试"}
        try:
            folder, open_file = _dialog_constants()
            if kind in ("skill_dir", "dir"):
                picked = self._window.create_file_dialog(folder)
            elif kind == "file":
                picked = self._window.create_file_dialog(
                    open_file, allow_multiple=True, file_types=FILE_FILTERS
                )
            elif kind == "skill_zip":
                picked = self._window.create_file_dialog(open_file, file_types=("技能包 (*.zip)",))
            elif kind == "json":
                picked = self._window.create_file_dialog(open_file, file_types=("JSON 配置 (*.json)",))
            else:
                return {"paths": [], "error": "未知的选择类型：" + str(kind)}
        except Exception as e:  # 对话框不可用（缺后端等）→ 让前端退回手动输入
            return {"paths": [], "error": str(e)}
        if not picked:
            return {"paths": []}
        return {"paths": [str(p) for p in picked]}
