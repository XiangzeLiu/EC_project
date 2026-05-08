"""
Server_economic — 经济数据子服务端

业务执行侧组件，负责：
  1. 向 Server_manager 完成注册审核（HTTP REST + SSE）
  2. 持续心跳保活（Bearer Token 认证）
  3. 提供 WebSocket 服务端供客户端连接（Client ↔ SE）
  4. 经济数据采集与业务处理

启动方式:
    python -m Server_economic.main
    python Server_economic/main.py
    uvicorn Server_economic.main:app --host 0.0.0.0 --port 8900

架构分层:
    通信层  — RegistrationClient(注册) / HeartbeatSender(心跳) / WSServer(WS服务端)
    指令层  — MessageRouter / MessageParser / CommandHandler
    业务层  — EconomicDataService(经济数据采集)
    基础设施— ConfigLoader / Logger / StateManager
"""

__version__ = "1.0.0"
