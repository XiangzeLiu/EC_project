# Trader_Server 通信梳理表

## 说明

- 范围：只整理当前 `Trader_Server` 代码里的真实通信链路。
- 结构：固定 4 张主表，分别看 `TS 发给 SM`、`TS 接收 SM`、`TS 发给 Client`、`TS 接收 Client`。
- 结论基于静态代码事实，未做三端联调。

## 1. TS -> SM 发送

| 协议 | 接口 | TS 入口 | SM 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /auth/verify-token` | `Trader_Server/network/ws_server.py:195-236` | `Server_manager/routers/auth_router.py:121-196` | Client `token` `server_id` + TS Bearer token | 收到 Client `CONNECT` 时 | 向 SM 校验该 Client 是否有权连接当前 TS | 若 TS 未被该用户占用，SM 会拒绝 |
| HTTP | `GET /ping` | `Trader_Server/services/registration.py:37-60` | `Server_manager/main.py:611-616` | 无 | 注册前测试连通性时 | 检查 SM 是否可达 | 也被本地 GUI 的注册页包装调用 |
| HTTP | `POST /nodes/register-request` | `Trader_Server/services/registration.py:65-138` | `Server_manager/main.py:619-655` | `node_name` `region` `host` `capabilities` `contact` `description` | 提交注册时 | 发起 TS 注册申请 | 成功后写本地 `.register_state.json` |
| SSE | `GET /nodes/await-approval` | `Trader_Server/services/registration.py:175-260` | `Server_manager/main.py:658-724` | `request_id` | 等待审批时 | 持续等待 SM 审批结果 | 批准后保存 `server_id` 和 `token` |
| HTTP | `POST /nodes/cancel-request` | `Trader_Server/services/registration.py:141-170` | `Server_manager/main.py:1024-1063` | `request_id` `reason` `force_discard_approved` | 取消注册等待时 | 取消或废弃注册申请 | 可处理 pending 和 approved 两种状态 |
| HTTP | `POST /nodes/heartbeat` | `Trader_Server/services/heartbeat.py:111-220` | `Server_manager/main.py:772-829` | Bearer token `ts` `ip` | 注册完成后后台循环 | 心跳保活，并拿回占用状态与配置版本 | SM 会返回动态 `next_interval` |
| HTTP | `GET /api/nodes/config` | `Trader_Server/services/config_sync.py:291-329` | `Server_manager/main.py:1229-1257` | `server_id` `token` | 初始化 broker、热重载时 | 拉取 broker 配置 | 支持 query token 和 Bearer header |

## 2. TS <- SM 接收

| 协议 | 接口 / 消息 | SM 发送入口 | TS 处理入口 | 关键返回 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /auth/verify-token` 响应 | `Server_manager/routers/auth_router.py:144-196` | `Trader_Server/network/ws_server.py:208-230` | `ok` `valid` `allowed` `username` `server_id` | TS 校验 Client token 时 | 判断是否允许该 Client 接入当前 TS | 这是 TS 放行 `CONNECT` 的前置条件 |
| HTTP | `GET /ping` 响应 | `Server_manager/main.py:613-616` | `Trader_Server/services/registration.py:44-60` | `status=pong` | 注册前探活时 | 确认 SM 在线 | 失败则注册流程不继续 |
| SSE | `/nodes/await-approval` | `Server_manager/main.py:677-688` `987-995` `1013-1019` | `Trader_Server/services/registration.py:225-260` `Trader_Server/main.py:257-348` | `approved` `server_id` `token` `reason` | TS 等待审批时 | 接收审批通过或拒绝结果 | 批准后还会启动心跳和 broker 初始化 |
| HTTP | `POST /nodes/heartbeat` 响应 | `Server_manager/main.py:810-829` | `Trader_Server/services/heartbeat.py:143-201` | `status` `next_interval` `occupied` `occupied_by` `config_version` | 每次心跳时 | 调整心跳间隔，并检查是否要重拉配置 | TS 用 `config_version` 触发热更新 |
| HTTP | `GET /api/nodes/config` 响应 | `Server_manager/main.py:1248-1257` | `Trader_Server/services/config_sync.py:311-317` | `broker_type` `credentials` `enabled` `config_version` | broker 初始化或 reload 时 | 更新 TS 本地 broker 配置 | 后续交给 `BrokerFactory` 创建实例 |
| SSE | `CONFIG_CHANGED` | `Server_manager/main.py:1640-1648` | `Trader_Server/services/config_sync.py:237-277` | `type=CONFIG_CHANGED` `config_version` | 管理员改配置或触发 reload 时 | 让 TS 立即重拉配置 | 这是比 heartbeat 更快的变更通道 |
| HTTP | `POST /api/admin/force-disconnect` | `Server_manager/main.py:1166-1212` | `Trader_Server/main.py:477-488` | `reason` | 管理员 force-release 时 | 让 TS 强制断开当前所有 Client | 这是 SM 主动打到 TS 的管理入口 |

## 3. TS -> Client 发送

| 协议 | 消息 | TS 发送入口 | Client 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| WebSocket | `CONNECT_ACK` | `Trader_Server/network/ws_server.py:131-149` | `Client/ui/main_window.py:2076-2085` | `session_id` `node_info` `broker_gate` | `CONNECT` 成功后 | 建立 Client 会话 | 首次连接成功的确认包 |
| WebSocket | `STATUS_RESPONSE` | `Trader_Server/network/ws_server.py:307-325` | `Client/ui/main_window.py:2087-2096` | `node_info` `broker_gate` | `STATUS_QUERY` 后 | 返回节点状态 | 给 Client 刷新状态栏 |
| WebSocket | `BROKER_LOGIN_RESPONSE` | `Trader_Server/network/ws_server.py:446-490` | `Client/ui/main_window.py:2098-2106` | `success` `code` `message` `gate` | 交易服务登录后 | 返回 gate 登录结果 | 成功后 Client 会刷新持仓和订单 |
| WebSocket | `BROKER_STATUS_RESPONSE` | `Trader_Server/network/ws_server.py:496-506` | `Client/ui/main_window.py:2098-2106` | `gate` | 状态查询后 | 返回 gate 当前状态 | 控制 Client 可操作性 |
| WebSocket | `BROKER_LOGOUT_RESPONSE` | `Trader_Server/network/ws_server.py:512-522` | `Client/ui/main_window.py:2098-2106` | `gate` | 退出交易服务后 | 返回 gate 清理结果 | 用于恢复灰态 |
| WebSocket | `POSITION_RESPONSE` | `Trader_Server/network/ws_server.py:390-395` | `Client/services/trading_session.py` | 持仓结果 | `POSITION_QUERY` 后 | 返回持仓 | 结果由 `trading_svc.get_positions()` 提供 |
| WebSocket | `ORDER_LIST_RESPONSE` | `Trader_Server/network/ws_server.py:412-417` | `Client/services/trading_session.py` | 订单结果 | `ORDER_QUERY` 后 | 返回订单列表 | 结果由 `trading_svc.get_orders()` 提供 |
| WebSocket | `ORDER_RESPONSE` | `Trader_Server/network/ws_server.py:351-356` | `Client/services/trading_session.py` | 下单结果 | `ORDER_SUBMIT` 后 | 返回下单结果 | 结果由 `trading_svc.place_order()` 提供 |
| WebSocket | `ORDER_CANCEL_RESPONSE` | `Trader_Server/network/ws_server.py:370-375` | `Client/services/trading_session.py` | 撤单结果 | `ORDER_CANCEL` 后 | 返回撤单结果 | 结果由 `trading_svc.cancel_order()` 提供 |
| WebSocket | `QUOTE_ACK` | `Trader_Server/network/ws_server.py:429-434` | `Client/ui/main_window.py:2129-2130` | 订阅结果 | 行情订阅/退订后 | 确认行情订阅状态 | Client 当前只被动接收 |
| WebSocket | `QUOTE_DATA` | `Trader_Server/services/config_sync.py:480-505` | `Client/ui/main_window.py:2107-2127` | `symbol` `bid` `ask` `last` `volume` `ts` | broker 行情回调时 | 推送实时行情 | 通过 `broadcast_message()` 广播 |
| WebSocket | `BROKER_STATUS_CHANGE` | `Trader_Server/services/config_sync.py:507-530` | `Client/ui/main_window.py:2164-2165` | `broker_type` `status` `config_version` | broker 连接状态变化时 | 广播 broker 状态变化 | Client 当前只记日志 |
| WebSocket | `FORCE_DISCONNECT` | `Trader_Server/network/ws_server.py:561-588` | `Client/ui/main_window.py:2132-2154` | `code` `reason` `message` | SM 强制释放时 | 强制断开所有 Client | 关闭前会触发 gate grace |
| WebSocket | `ERROR` `PONG` | `Trader_Server/network/ws_server.py:165-176` `239-257` | `Client/ui/main_window.py:2155-2162` | `code` `message` `trace_id` | 心跳或异常时 | 保活和统一错误回传 | 通用协议消息 |

## 4. TS <- Client 接收

| 协议 | 消息 | Client 发送入口 | TS 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| WebSocket | `CONNECT` | `Client/network/ts_websocket.py:355-368` | `Trader_Server/network/ws_server.py:73-150` | `token` `server_id` `trace_id` | Client 建立直连时 | 建立主连接并做鉴权 | 第一包必须是它 |
| WebSocket | `PING` | `Client/network/ts_websocket.py` 心跳循环 | `Trader_Server/network/ws_server.py:165-176` | `id` | 连接存活期间 | 保活 | TS 返回 `PONG` |
| WebSocket | `STATUS_QUERY` | `Client/services/trading_session.py` | `Trader_Server/network/ws_server.py:305-325` | `trace_id` | Client 刷新状态时 | 查询节点和 gate 状态 | 常规状态查询 |
| WebSocket | `BROKER_LOGIN` | `Client/services/trading_session.py:88-111` | `Trader_Server/network/ws_server.py:437-491` | `account_username` `account_password` | 用户登录交易服务时 | 打开 gate | 当前还是本地校验逻辑 |
| WebSocket | `BROKER_STATUS_QUERY` | `Client/services/trading_session.py:113-125` | `Trader_Server/network/ws_server.py:494-507` | `trace_id` | 刷新 gate 时 | 查询 gate 状态 | 常规 gate 查询 |
| WebSocket | `BROKER_LOGOUT` | `Client/services/trading_session.py:127-139` | `Trader_Server/network/ws_server.py:510-523` | `trace_id` | 用户退出交易服务时 | 清掉 gate | 仅影响当前用户 gate |
| WebSocket | `POSITION_QUERY` | `Client/services/trading_session.py:271-320` | `Trader_Server/network/ws_server.py:378-395` | `symbols` `trace_id` | 刷新持仓时 | 查询持仓 | 真实走 broker API |
| WebSocket | `ORDER_QUERY` | `Client/services/trading_session.py:360-409` | `Trader_Server/network/ws_server.py:398-417` | `mode` `trace_id` | 刷新订单时 | 查询订单 | 真实走 broker API |
| WebSocket | `ORDER_SUBMIT` | `Client/services/trading_session.py:411-459` | `Trader_Server/network/ws_server.py:340-357` | 下单参数 `trace_id` | 下单时 | 提交交易 | 真实走 broker API |
| WebSocket | `ORDER_CANCEL` | `Client/services/trading_session.py` | `Trader_Server/network/ws_server.py:359-375` | `order_id` `trace_id` | 撤单时 | 撤销订单 | 真实走 broker API |
| WebSocket | `QUOTE_SUBSCRIBE` | `Client/services/trading_session.py:162-211` | `Trader_Server/network/ws_server.py:420-434` | `action` `symbols` | 行情订阅时 | 管理行情订阅关系 | 由 `quote_provider` 落地 |

## 5. TS 本地 GUI 使用链路

| 协议 | 接口 | 本地调用入口 | TS 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /api/register/ping` | `Trader_Server/ui_qt/api_client.py:76` | `Trader_Server/main.py:100-126` | `manager_url` | GUI 检测 SM 地址时 | 本地包装 SM ping | 当前仍有实际调用 |
| HTTP | `POST /api/register/submit` | `Trader_Server/ui_qt/api_client.py:80` | `Trader_Server/main.py:130-173` | 注册字段 | GUI 提交注册时 | 本地包装注册申请 | 当前仍有实际调用 |
| HTTP | `POST /api/register/cancel` | `Trader_Server/ui_qt/api_client.py:83` | `Trader_Server/main.py:176-197` | `request_id` `reason` | GUI 取消等待审批时 | 本地包装取消申请 | 当前仍有实际调用 |
| HTTP | `GET /api/register/pre-approve-check` | 未检到当前代码内明确调用入口 | `Trader_Server/main.py:200-234` | `request_id` | 注册等待期间 | 审批前校验 request 是否仍有效 | 当前未检到明确调用方，但删除风险未证伪，不归类为废弃 |
| SSE | `GET /api/register/await-approval` | `Trader_Server/ui_qt/api_client.py:107` | `Trader_Server/main.py:237-348` | `request_id` | GUI 等待审批时 | 本地代理审批 SSE 流 | 当前仍有实际调用 |
| HTTP | `POST /api/register/clear` | `Trader_Server/ui_qt/api_client.py:92` | `Trader_Server/main.py:351-375` | 无 | 清理注册凭证时 | 删除本地注册状态与 config | 当前仍有实际调用 |
| HTTP | `GET /health` | `Trader_Server/ui_qt/api_client.py:67` | `Trader_Server/main.py:389-400` | 无 | GUI 本地健康检查时 | 检查 TS 是否存活 | 当前仍有实际调用 |
| HTTP | `GET /api/status` | `Trader_Server/ui_qt/api_client.py:56` | `Trader_Server/main.py:403-436` | 无 | GUI 状态轮询时 | 查看 TS 运行状态 | 当前仍有实际调用 |
| HTTP | `GET /api/economic-data` | `Trader_Server/ui_qt/api_client.py:60` | `Trader_Server/main.py:439-448` | `indicator` | GUI 查看经济数据时 | 读取经济数据 | 当前仍有实际调用 |
| HTTP | `GET /api/logs` | `Trader_Server/ui_qt/api_client.py:63` | `Trader_Server/main.py:458-466` | `limit` | GUI 查看日志时 | 读取消息日志 | 当前仍有实际调用 |
| HTTP | `POST /api/logs/clear` | `Trader_Server/ui_qt/main_window.py` | `Trader_Server/main.py:469-474` | 无 | GUI 清空日志时 | 清空消息日志 | 当前仍有实际调用 |
| HTTP | `GET /api/summary` | 未检到当前 GUI 明确调用入口 | `Trader_Server/main.py:451-455` | 无 | 本地摘要查询时 | 返回经济数据摘要 | 目前未检到明确调用方，但删除风险未证伪，不归类为废弃 |

## 6. 废弃链路复核结论

- 本轮代码复核后，`Trader_Server` 当前未发现可安全删除的废弃业务 API。
- `TS` 本地 GUI 接口不是废弃链路，当前仍被 `Trader_Server/ui_qt/api_client.py` 或 `Trader_Server/ui_qt/main_window.py` 实际使用。
- `POST /api/admin/force-disconnect` 不是废弃接口，它被 `Server_manager` 的 force-release 流程主动调用。
- `POST /auth/verify-token`、`POST /nodes/heartbeat`、`GET /api/nodes/config`、`GET /nodes/config-events`、`GET /nodes/await-approval` 都属于 TS 当前主链路，不能按废弃处理。
- 券商相关主链路 `ORDER_SUBMIT`、`ORDER_CANCEL`、`POSITION_QUERY`、`ORDER_QUERY`、`QUOTE_SUBSCRIBE` 当前都仍在使用，不能误删。
- 旧命名残留已清理；后续若新增命名迁移，仍按“只改文本、不改业务链路”的原则处理。

## 2026-07-09 联调复核结论

| 链路 | 结果 | 说明 |
|---|---|---|
| TS 本地后台接口 | 已通过 | `/health`、`/api/status`、`/api/logs`、`/api/logs/clear`、`/api/economic-data`、`/api/summary` 均在隔离环境通过 |
| TS 注册代理链路 | 已通过 | `/api/register/ping`、`submit`、`pre-approve-check`、`await-approval`、`clear`、`cancel` 参数校验均通过 |
| SM 审批到 TS SSE 回传 | 已通过 | SM 审批后 TS 可通过 SSE 收到 `approved=true` 并进入 `running` |
| SM 占用 / 释放 TS | 已通过 | `test` 交易员可占用并释放指定 TS 节点 |
| Client token -> TS WS CONNECT | 已通过 | TS 通过 SM `/auth/verify-token` 校验 Client token 后返回 `CONNECT_ACK` |
| TS 交易服务 gate | 已通过 | 开发后门 `test/test` 可触发 `BROKER_LOGIN`，后续 `BROKER_STATUS_QUERY` 返回 `active=true` |
| TS force-disconnect 未授权保护 | 已通过 | 无节点 Bearer token 时返回 401 |

兼容说明：`se_address`、部分 `_se_*` 内部变量仍保留为兼容字段或旧 Tk 代码内部变量，本轮不做破坏性重命名。当前 TS 正式结构和文档已统一到 `Trader_Server` / `TS` 语义。