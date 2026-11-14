"""消息导航条（右侧刻度）：主栏一条，每个分屏列各一条（静态源码锚定）。

分屏列是独立对话面，共用主栏那条会让列里的滚动高亮与点击目标全指向主栏；
用户要求「每一个屏幕的右侧都有，样式和只有一个屏幕时一样，只是小一些」。
"""

from conftest import read_app_bundle


def test_each_screen_has_its_own_minimap():
    js = read_app_bundle()
    # 一条导航条 = 一个实例，各自绑自己的聊天流
    assert "function createChatMinimap(" in js
    # 主栏保留原 id（单屏样式与既有测试的锚点）
    assert 'createChatMinimap(chatBox, { id: "chat-minimap" })' in js
    # 每个分屏列在创建时挂自己的导航条，关列时销毁（不留观察器/监听器）
    assert "createChatMinimap(col, { log: pane.logEl })" in js
    assert "pane.minimap.destroy()" in js
    # 旧的全局单实例绑定函数不该再存在
    assert "function miniBind(" not in js


def test_minimap_skips_non_conversation_pseudo_messages():
    """刻度只对应真正的对话：截图回填的伪 user 消息与系统补记不算一格。

    工具截图会在历史里留下「[screenshot] 上述工具…」的 user 消息，不排除的话
    接力一轮里截图多时，刻度条数会明显多于对话条数（2026-11-14 用户实例）。
    """
    js = read_app_bundle()
    assert 'el.classList.contains("screenshot-note")' in js
    assert 'el.classList.contains("system-note")' in js
    assert 'd.classList.add("screenshot-note")' in js
