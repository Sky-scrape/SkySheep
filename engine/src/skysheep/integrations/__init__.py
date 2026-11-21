"""外部服务集成（第三方 HTTP API 的薄客户端）。

与 ``core/``（Agent 循环、权限门、检查点）平级：放只依赖引擎基础设施
（``network``、``config``）出网、不参与 Agent 循环的服务适配器。
当前成员：``weather``（Open-Meteo 现实天气，见 [factory] weather）。
"""
