# Client 通信梳理表

更新时间：2026-07-27。

## 1. 当前主链路

```text
Client --HTTPS--> SM 登录并取得 Client Token、固定 TS 地址
Client --HTTPS--> SM 查询并占用固定 TS
Client --WSS----> TS CONNECT 鉴权
Client --WSS----> TS 行情、持仓、订单、下单、撤单
TS     --OAuth--> tastytrade（使用 SM 审批时保存的 Client Secret、Refresh Token、Account Number）
```

Client 不保存券商凭证，也不再执行券商用户名、密码或验证码登录。

## 2. Client 与 SM

| 方向 | 协议与接口 | 用途 | 关键数据 |
|---|---|---|---|
| Client -> SM | `POST /auth/login` | SM 交易员登录 | `username` `password` `force` |
| SM -> Client | 登录响应 | 返回 Client Token 和固定 TS 地址 | `token` `se_address` |
| Client -> SM | `GET /api/accounts/se-status` | 连接前检查账号绑定 TS | `address` + Bearer Token |
| Client -> SM | `POST /api/nodes/{server_id}/occupy` | 锁定单 Client 使用的 TS | `username` `connection_id` |
| Client -> SM | `POST /api/nodes/{server_id}/release` | 正常退出或连接失败时释放 TS | `server_id` `connection_id` |
| Client -> SM | `POST /auth/logout` | 注销 SM Client Token | Bearer Token |

## 3. Client 发给 TS 的 WebSocket 消息

| 消息 | 用途 | 运行条件 |
|---|---|---|
| `CONNECT` | WSS 首包鉴权并建立会话 | 已登录 SM，TS 已被当前用户占用 |
| `STATUS_QUERY` | 查询 TS 节点和真实券商状态 | TS 已连接 |
| `BROKER_STATUS_QUERY` | 查询 TS 管理的 OAuth Session、账户和能力 | TS 已连接 |
| `QUOTE_SUBSCRIBE` | 订阅或退订 Symbol 行情 | TS 与券商行情能力可用 |
| `POSITION_QUERY` | 查询所选券商账户持仓 | `positions=true` |
| `ORDER_QUERY` | 查询活动或当日订单 | `order_query=true` |
| `ORDER_SUBMIT` | 提交订单 | `orders=true`；read-only 为 false |
| `ORDER_CANCEL` | 撤销订单 | `cancel_order=true`；read-only 为 false |
| `PING` | WSS 保活 | 连接存续期间 |

`BROKER_LOGIN` 和 `BROKER_LOGOUT` 已从协议删除。Client 退出只释放 TS 占用和 SM Client Token，不销毁 TS 的券商 OAuth Session。

## 4. Client 接收 TS 的 WebSocket 消息

| 消息 | 关键数据 | Client 行为 |
|---|---|---|
| `CONNECT_ACK` | `session_id` `node_info` `broker_detail` | 建立会话并初始化券商/账户状态 |
| `STATUS_RESPONSE` | `node_info` `broker_detail` | 更新 TS 与券商状态 |
| `BROKER_STATUS_RESPONSE` | `broker_detail` | 更新账户和 capability |
| `BROKER_STATUS_CHANGE` | `status` `broker_detail` | 处理断线、重连、配置热更新并恢复行情订阅 |
| `QUOTE_DATA` | `symbol` `bid` `ask` `last` `volume` | 更新 Symbol 行情 |
| `QUOTE_ACK` | 订阅处理结果 | 记录订阅结果 |
| `POSITION_RESPONSE` | `positions` | 更新持仓表 |
| `ORDER_LIST_RESPONSE` | `orders` | 更新订单表 |
| `ORDER_RESPONSE` | 下单结果 | 显示结果并刷新订单/持仓 |
| `ORDER_CANCEL_RESPONSE` | 撤单结果 | 显示结果并刷新订单 |
| `FORCE_DISCONNECT` | `reason` | 停止连接并释放本地状态 |
| `ERROR` | `code` `message` `trace_id` | 显示错误，不弹出券商登录框 |
| `PONG` | 心跳响应 | 维持连接 |

## 5. Client 功能门控

Client 主界面不使用统一灰色遮罩，按 TS 返回的真实能力独立控制：

| 功能 | 条件 |
|---|---|
| Symbol 输入与行情 | TS WSS 在线且 `quotes=true` |
| 持仓刷新 | `connected=true` 且 `positions=true` |
| 订单刷新 | `connected=true` 且 `order_query=true` |
| Buy / Sell | `connected=true` 且 `orders=true` |
| 撤单 | `connected=true` 且 `cancel_order=true` |

read-only 账户允许行情、持仓和订单查询，Buy、Sell 和撤单保持禁用，并在顶部账户状态中显示 `READ ONLY`。

## 6. 残留旧链路

`Client/network/ws_client.py` 与 SM 旧 `/quotes` WebSocket 仍属于旧行情实现，不在当前正式入口 `Client/main.py -> Client/ui_qt/main_window.py` 的主链路内。物理删除继续按生产待办中的旧代码清理项处理。
