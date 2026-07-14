# Client 通信梳理表

## 说明

- 范围：只整理当前 `Client` 代码里真实存在的通信链路。
- 结构：固定 4 张主表，分别看 `Client 发给 SM`、`Client 接收 SM`、`Client 发给 TS`、`Client 接收 TS`。
- 结论基于静态代码事实，未做三端联调。

## 1. Client -> SM 发送

| 协议 | 接口 | Client 入口 | SM 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /auth/login` | `Client/services/trading_session.py:217-243` | `Server_manager/routers/auth_router.py:56-118` | `username` `password` `force` | 用户登录时 | 获取 Client 登录 token 和目标 TS 地址 | 登录成功后把 `se_address` 写入本地会话 |
| HTTP | `POST /auth/logout` | `Client/services/trading_session.py:245-253` | `Server_manager/routers/auth_router.py:199-213` | Bearer token | 用户登出时 | 注销 Client token | 只清 Client 会话 |
| HTTP | `GET /api/accounts/se-status?address=...` | `Client/ui/main_window.py:339-387` `552-592` | `Server_manager/main.py:1581-1626` | `address` + Bearer token | 连接 TS 前；TS 重连前 | 检查目标 TS 是否在线、是否被占用 | 路由名仍是 `se-status`，但实际查的是 TS 节点 |
| HTTP | `POST /api/nodes/{server_id}/occupy` | `Client/ui/main_window.py:697-780` | `Server_manager/main.py:1105-1139` | Bearer token `username` `server_id` | TS 在线且可连接后 | 在 SM 侧登记“当前 Client 占用该 TS” | 若已被别人占用会失败 |
| HTTP | `POST /api/nodes/{server_id}/release` | `Client/ui/main_window.py:782-808` | `Server_manager/main.py:1143-1163` | Bearer token `server_id` | 断开 TS、取消初始化、失败回滚时 | 释放 TS 占用 | 只允许占用者本人释放 |

## 2. Client <- SM 接收

| 协议 | 接口 | SM 返回入口 | Client 使用入口 | 返回字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /auth/login` | `Server_manager/routers/auth_router.py:71-77` `86-92` `103-112` | `Client/services/trading_session.py:223-243` | `token` `broker_list` `se_address` | 登录成功时 | 给 Client 建立会话并告知目标 TS 地址 | `Client` 后续用 `se_address` 去找 TS |
| HTTP | `POST /auth/logout` | `Server_manager/routers/auth_router.py:213` | `Client/services/trading_session.py:247-253` | `success` | 登出时 | 结束服务端会话 | Client 本地随后清 token |
| HTTP | `GET /api/accounts/se-status` | `Server_manager/main.py:1607-1626` | `Client/ui/main_window.py:345-358` `557-570` | `online` `node_name` `server_id` `occupied_by` `occupied_at` | 校验 TS 时 | 告知目标 TS 是否可连 | 直接决定 Client 是否继续建 WS |
| HTTP | `POST /api/nodes/{server_id}/occupy` | `Server_manager/main.py:1136-1139` | `Client/ui/main_window.py:729-733` | `ok` `message` | 占用 TS 时 | 确认占用是否成功 | 成功后 Client 才继续主流程 |
| HTTP | `POST /api/nodes/{server_id}/release` | `Server_manager/main.py:1162-1163` | `Client/ui/main_window.py:788-808` | `ok` `message` | 释放占用时 | 确认释放是否成功 | 失败仅记日志，不继续阻塞 UI |

## 3. Client -> TS 发送

| 协议 | 消息 / 接口 | Client 入口 | TS 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| WebSocket | `CONNECT` | `Client/network/ts_websocket.py:339-399` | `Trader_Server/main.py:491-494` `Trader_Server/network/ws_server.py:65-150` | `token` `server_id` `trace_id` | SM 校验通过后 | 建立 Client 到 TS 的主连接 | 首包必须是 `CONNECT` |
| WebSocket | `STATUS_QUERY` | `Client/services/trading_session.py` 内部 `_request_se()` | `Trader_Server/network/ws_server.py:305-325` | `trace_id` | 主界面状态刷新时 | 查询 TS 状态和 gate 状态 | UI 有对应消费逻辑 |
| WebSocket | `BROKER_LOGIN` | `Client/services/trading_session.py:88-111` | `Trader_Server/network/ws_server.py:437-491` | `account_username` `account_password` | 用户点击交易服务登录时 | 打开 TS 侧交易服务 gate | 当前还不是真实券商二段登录 |
| WebSocket | `BROKER_STATUS_QUERY` | `Client/services/trading_session.py:113-125` | `Trader_Server/network/ws_server.py:494-507` | `trace_id` | 进入主界面后；错误恢复时 | 刷新 gate 状态 | UI 灰态依赖它 |
| WebSocket | `BROKER_LOGOUT` | `Client/services/trading_session.py:127-139` | `Trader_Server/network/ws_server.py:510-523` | `trace_id` | 用户主动退出交易服务时 | 清理 TS 侧 gate | 与断线 grace 机制并存 |
| WebSocket | `POSITION_QUERY` | `Client/services/trading_session.py:271-320` | `Trader_Server/network/ws_server.py:378-395` | `symbols` `trace_id` | 刷新持仓时 | 查询持仓 | 前提是 `can_trade()` 为真 |
| WebSocket | `ORDER_QUERY` | `Client/services/trading_session.py:360-409` | `Trader_Server/network/ws_server.py:398-417` | `mode` `trace_id` | 刷新订单时 | 查询订单 | `mode` 仅接受 `live` / `all` |
| WebSocket | `ORDER_SUBMIT` | `Client/services/trading_session.py:411-459` | `Trader_Server/network/ws_server.py:340-357` | 下单参数 `trace_id` | 下单时 | 提交交易请求 | 实际落到 TS `trading_svc.place_order()` |
| WebSocket | `QUOTE_SUBSCRIBE` | `Client/services/trading_session.py:162-211` | `Trader_Server/network/ws_server.py:420-434` | `action` `symbols` `trace_id` | 订阅/退订行情时 | 管理行情订阅 | TS 按 `subscribe` / `unsubscribe` 处理 |

## 4. Client <- TS 接收

| 协议 | 消息 | TS 发送入口 | Client 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| WebSocket | `CONNECT_ACK` | `Trader_Server/network/ws_server.py:131-149` | `Client/ui/main_window.py:2076-2085` | `session_id` `node_info` `broker_gate` | `CONNECT` 成功后 | 建立会话并同步 gate 状态 | 初始化阶段和运行阶段都会消费 |
| WebSocket | `STATUS_RESPONSE` | `Trader_Server/network/ws_server.py:307-325` | `Client/ui/main_window.py:2087-2096` | `node_info` `broker_gate` | `STATUS_QUERY` 返回时 | 更新节点状态显示 | 有专门 UI 分支 |
| WebSocket | `BROKER_LOGIN_RESPONSE` `BROKER_STATUS_RESPONSE` `BROKER_LOGOUT_RESPONSE` | `Trader_Server/network/ws_server.py:446-490` `496-506` `512-522` | `Client/ui/main_window.py:2098-2106` | `success` `code` `message` `gate` | 交易服务登录/查状态/退出时 | 刷新 gate，必要时刷新持仓和订单 | 登录成功后会延迟刷新持仓/订单 |
| WebSocket | `QUOTE_DATA` | `Trader_Server/services/config_sync.py:480-505` | `Client/ui/main_window.py:2107-2127` | `symbol` `bid` `ask` `last` `volume` `ts` | broker 行情回调时 | 推送行情到界面 | 缺少 `last` 时会用 `bid/ask` 算中间价 |
| WebSocket | `QUOTE_ACK` | `Trader_Server/network/ws_server.py:429-434` | `Client/ui/main_window.py:2129-2130` | 订阅处理结果 | 行情订阅/退订后 | 订阅确认 | Client 当前收到后不做 UI 动作 |
| WebSocket | `FORCE_DISCONNECT` | `Trader_Server/network/ws_server.py:561-588` | `Client/ui/main_window.py:2132-2154` | `code` `reason` `message` | SM 强制释放占用时 | 强制断开 Client 与 TS 的连接 | 会关闭重连并弹警告 |
| WebSocket | `ERROR` | `Trader_Server/network/ws_server.py:239-257` `252-257` | `Client/ui/main_window.py:2155-2159` | `code` `message` `trace_id` | 鉴权失败、格式错误、未知消息、内部错误时 | 统一错误回传 | 部分错误码会触发 gate 刷新 |
| WebSocket | `PONG` | `Trader_Server/network/ws_server.py:165-176` | `Client/ui/main_window.py:2161-2162` | 空 | Client 心跳时 | 保活 | 收到后不做 UI 动作 |
| WebSocket | `BROKER_STATUS_CHANGE` | `Trader_Server/services/config_sync.py:507-530` | `Client/ui/main_window.py:2164-2165` | `broker_type` `status` `config_version` | TS broker 状态变化时 | 状态广播 | Client 当前只走通用日志分支 |

## 残留 / 旧链路

| 方向 | 发送方 | 接收方 | 协议 | 接口或消息名 | 请求入口 | 处理入口 | 核心字段 | 返回字段 | 触发时机 | 当前用途 | 状态 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Client -> SM | Client | Server_manager | WebSocket | `/quotes` | `Client/network/ws_client.py:84-145` | `Server_manager/main.py` 旧 `/quotes` WebSocket | `token` `subscribe/unsubscribe` | 行情流 | 旧版行情流客户端启动时 | 直接从 SM 收行情 | 残留旧链路 | 当前主流程已改为 `Client -> TS` 收行情，此文件未见被主界面主链路调用 |
