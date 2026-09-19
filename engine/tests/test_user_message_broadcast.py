"""跨客户端「用户消息」广播的回归测试。

轮次事件（text_delta / assistant_message / turn_finished …）只发给发起连接；
其他在线客户端（第二个窗口 / 手机遥控端）此前没有任何「发送方说了什么」的
事件来源——只会看到回复凭空出现，看不到用户消息。后端在轮次真正开始时把
user_message 广播给除发起连接之外的所有在线前端（发起连接本地已渲染过气泡，
按连接对象身份排除，避免重复）。
"""

from test_server import make_client, recv_until

from skysheep.messages import TextBlock


def test_user_message_reaches_other_clients_not_sender(home):
    script = [[TextBlock(text="回复一")]]
    with make_client(home, script) as client, \
         client.websocket_connect("/ws") as ws1, \
         client.websocket_connect("/ws") as ws2:
        # 连接 2 先于发送建立：必须在轮次开始时收到 user_message 广播
        ws2.send_json({"id": "b2", "method": "boot"})
        assert recv_until(ws2, "b2")["ok"]

        ws1.send_json({"id": "c1", "method": "chat.send", "params": {"text": "你好"}})
        ev1 = []
        frame = recv_until(ws1, "c1", ev1)
        assert frame["ok"] and frame["result"]["done"]

        # 发起连接绝不该收到自己的 user_message（本地已渲染过，重复会出两条气泡）
        assert "user_message" not in [e["event"] for e in ev1]

        # 其他连接必须收到：带文本与会话 id（用一次轻量请求把事件帧冲出来）
        ev2 = []
        ws2.send_json({"id": "s2", "method": "chat.status"})
        recv_until(ws2, "s2", ev2)
        kinds2 = [e["event"] for e in ev2]
        assert "user_message" in kinds2
        um = next(e for e in ev2 if e["event"] == "user_message")
        assert um["data"]["text"] == "你好"
        assert um["data"]["session_id"]


def test_regen_turn_does_not_broadcast_empty_user_message(home):
    """重新生成轮 text 为空串：不应给其他客户端广播空的 user_message。"""
    script = [[TextBlock(text="第一版")], [TextBlock(text="第二版")]]
    with make_client(home, script) as client, \
         client.websocket_connect("/ws") as ws1, \
         client.websocket_connect("/ws") as ws2:
        ws2.send_json({"id": "b2", "method": "boot"})
        assert recv_until(ws2, "b2")["ok"]

        ws1.send_json({"id": "c1", "method": "chat.send", "params": {"text": "你好"}})
        ev1 = []
        recv_until(ws1, "c1", ev1)
        assert "user_message" not in [e["event"] for e in ev1]

        # 排空 ws2 在第一轮收到的事件（含第一轮的 user_message 广播）
        drained = []
        ws2.send_json({"id": "d0", "method": "chat.status"})
        recv_until(ws2, "d0", drained)
        assert "user_message" in [e["event"] for e in drained]  # 第一轮广播正常

        # 拿到助手消息 seq 后重新生成（regenerate 轮 text 为空串）
        ws1.send_json({"id": "r1", "method": "session.truncate",
                       "params": {"mode": "regen"}})
        frame = recv_until(ws1, "r1")
        assert frame["ok"]
        ws1.send_json({"id": "c2", "method": "chat.send",
                       "params": {"text": "", "regenerate": True}})
        ev1 = []
        recv_until(ws1, "c2", ev1)
        assert "user_message" not in [e["event"] for e in ev1]

        ev2 = []
        ws2.send_json({"id": "s2", "method": "chat.status"})
        recv_until(ws2, "s2", ev2)
        assert "user_message" not in [e["event"] for e in ev2]
