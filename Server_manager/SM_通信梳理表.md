# Server_manager 通信梳理表

## 说明

- 范围：只整理当前 `Server_manager` 代码中的真实通信链路。
- 结构：固定 4 张主表，分别看 `SM 发给 TS`、`SM 接收 TS`、`SM 发给 Client`、`SM 接收 Client`。
- 结论基于静态代码事实，未做三端联调。

## 1. SM -> TS 发送

| 协议 | 接口 / 消息 | SM 发送入口 | TS 处理入口 | 关键字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SSE | `/nodes/await-approval` | `Server_manager/main.py:658-724` `987-995` `1013-1019` | `Trader_Server/services/registration.py:175-260` `Trader_Server/main.py:237-348` | `approved` `server_id` `token` `reason` | 审批或拒绝注册后 | 把注册结果推给 TS | 通过 `_push_sse_result()` 投递 |
| SSE | `/nodes/config-events` | `Server_manager/main.py:727-769` `1640-1648` | `Trader_Server/services/config_sync.py:237-277` | `type=CONFIG_CHANGED` `server_id` `config_version` | 管理员改配置或 reload 后 | 通知 TS 立即重拉配置 | 通过 `_push_config_change()` 投递 |
| HTTP 响应 | `/api/nodes/config` | `Server_manager/main.py:1229-1257` | `Trader_Server/services/config_sync.py:291-329` | `broker_type` `credentials` `enabled` `config_version` | TS 主动拉配置时 | 下发 broker 配置 | 当前 broker 配置仍由 SM 存储 |
| HTTP | `POST /api/admin/force-disconnect` | `Server_manager/main.py:1166-1212` | `Trader_Server/main.py:477-488` | TS Bearer token `reason` | force-release 时 | 让 TS 强制断开当前 Client | 这是 SM 主动打到 TS 的管理入口 |

## 2. SM <- TS 接收

| 协议 | 接口 | TS 发送入口 | SM 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /auth/verify-token` | `Trader_Server/network/ws_server.py:195-236` | `Server_manager/routers/auth_router.py:121-196` | TS Bearer token `token` `server_id` | TS 收到 Client `CONNECT` 时 | 校验 Client 是否有权接入当前 TS | 占用关系不对时会拒绝 |
| HTTP | `GET /ping` | `Trader_Server/services/registration.py:37-60` | `Server_manager/main.py:611-616` | 无 | TS 注册前探活时 | 给 TS 返回健康探测结果 | 最基础的连通性检查 |
| HTTP | `POST /nodes/register-request` | `Trader_Server/services/registration.py:65-138` | `Server_manager/main.py:619-655` | `node_name` `region` `host` `capabilities` `contact` `description` | TS 提交注册时 | 创建待审批节点请求 | 先写入待审批区 |
| SSE | `GET /nodes/await-approval` | `Trader_Server/services/registration.py:175-260` | `Server_manager/main.py:658-724` | `request_id` | TS 等待审批时 | 建立审批结果推送通道 | SM 内部维护 `_node_sse_queues` |
| HTTP | `POST /nodes/cancel-request` | `Trader_Server/services/registration.py:141-170` | `Server_manager/main.py:1024-1063` | `request_id` `reason` `force_discard_approved` | TS 取消注册时 | 取消或废弃注册请求 | 可处理 pending / approved |
| HTTP | `POST /nodes/heartbeat` | `Trader_Server/services/heartbeat.py:111-220` | `Server_manager/main.py:772-829` | Bearer token `ts` `ip` | TS 运行中周期发送 | 保活、同步占用感知、返回配置版本 | `next_interval` 会动态变化 |
| HTTP | `GET /api/nodes/config` | `Trader_Server/services/config_sync.py:291-329` | `Server_manager/main.py:1229-1257` | `server_id` `token` | TS 初始化或热更新 broker 时 | 请求最新 broker 配置 | 由 node token 鉴权 |

## 3. SM -> Client 发送

| 协议 | 接口 | SM 返回入口 | Client 使用入口 | 关键返回 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /auth/login` | `Server_manager/routers/auth_router.py:71-77` `86-92` `103-112` | `Client/services/trading_session.py:223-243` | `token` `broker_list` `se_address` | Client 登录成功时 | 给 Client 会话 token 和目标 TS 地址 | Client 后续依赖 `se_address` 找 TS |
| HTTP | `POST /auth/logout` | `Server_manager/routers/auth_router.py:213` | `Client/services/trading_session.py:247-253` | `success` | Client 登出时 | 结束服务端会话 | 只影响 Client token |
| HTTP | `GET /api/accounts/se-status` | `Server_manager/main.py:1607-1626` | `Client/ui/main_window.py:345-358` `557-570` | `online` `node_name` `server_id` `occupied_by` `occupied_at` | Client 校验目标 TS 时 | 告知目标 TS 是否在线可连 | 这里直接影响 Client 是否继续连接 TS |
| HTTP | `POST /api/nodes/{server_id}/occupy` | `Server_manager/main.py:1136-1139` | `Client/ui/main_window.py:729-733` | `ok` `message` | Client 占用 TS 时 | 告知占用是否成功 | 成功后 Client 才继续主流程 |
| HTTP | `POST /api/nodes/{server_id}/release` | `Server_manager/main.py:1162-1163` | `Client/ui/main_window.py:788-808` | `ok` `message` | Client 释放 TS 时 | 告知释放是否成功 | 失败时 Client 端主要记日志 |

## 4. SM <- Client 接收

| 协议 | 接口 | Client 发送入口 | SM 处理入口 | 核心字段 | 触发时机 | 用途 | 备注 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| HTTP | `POST /auth/login` | `Client/services/trading_session.py:217-243` | `Server_manager/routers/auth_router.py:56-118` | `username` `password` `force` | 用户登录时 | 建立 Client 会话 | DB 分支会返回 `se_address` |
| HTTP | `POST /auth/logout` | `Client/services/trading_session.py:245-253` | `Server_manager/routers/auth_router.py:199-213` | Bearer token | 用户登出时 | 失效 Client token | 不直接断开 TS broker |
| HTTP | `GET /api/accounts/se-status?address=...` | `Client/ui/main_window.py:341-343` `554-556` | `Server_manager/main.py:1581-1626` | Bearer token `address` | 登录后连接 TS 前；重连前 | 按地址查当前在线 TS | 读的是内存节点状态，不查库 |
| HTTP | `POST /api/nodes/{server_id}/occupy` | `Client/ui/main_window.py:723-726` | `Server_manager/main.py:1105-1139` | Bearer token `username` | Client 准备接入 TS 时 | 登记该 Client 占用该 TS | 后续 `verify-token` 会用到这个关系 |
| HTTP | `POST /api/nodes/{server_id}/release` | `Client/ui/main_window.py:790` | `Server_manager/main.py:1143-1163` | Bearer token | Client 断开或回滚时 | 释放 TS 占用 | 仅允许占用者本人释放 |

## Residual / Deprecated Chains

- Removed on 2026-06-29 after static verification with no live references: `/orders/*`, `GET /positions`.
- Only chains that still carry functional risk are kept in this section.

| Direction | Sender | Receiver | Protocol | Interface | Request Entry | Handler | Key Fields | Return | Trigger | Current Use | Status | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Client legacy chain -> SM | Client | Server_manager | WebSocket | `/quotes` | `Client/network/ws_client.py:84-145` | legacy WebSocket in `Server_manager/main.py` | `token` `subscribe` `unsubscribe` | quote stream | old quote client path | direct quotes from SM | retained legacy chain | main flow moved to `Client -> TS`, but SM quote background tasks still depend on it, so it was not deleted |
