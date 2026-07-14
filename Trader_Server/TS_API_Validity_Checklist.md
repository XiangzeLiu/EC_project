# TS API 有效性清单

本文件基于当前代码事实与 `2026-06-27` 的本地真实联调结果整理，目标是快速判断 `Trader_Server` 当前所有 TS 接口及其下钻券商 API 的有效性。

本次结论分为两层：

- 静态代码审查：判断接口是否存在、链路是否接上
- 真实联调测试：判断当前环境下是否真的跑通

## 标记说明

| 标记 | 含义 |
|---|---|
| `已通过` | 当前环境下已真实测试通过 |
| `未通过` | 当前环境下已真实测试失败 |
| `需复测` | 当前未具备完整测试条件，或失败由环境/凭证阻塞，后续需再次验证 |
| `条件有效` | 代码链路存在，但运行结果依赖外部条件 |
| `不支持` | 当前代码明确不支持 |

## 总体结论

| 维度 | 结论 |
|---|---|
| TS 协议层 | 大体有效 |
| SM -> TS 登录、占用、连接链路 | 已真实跑通 |
| TS 本地交易服务 gate | 已真实跑通 |
| TS -> broker 真实交易链路 | 当前未跑通 |
| tastytrade 交易链路 | 代码已实现，但当前环境未完成真实券商连接 |
| tastytrade 行情链路 | 代码已实现，但当前环境未完成真实券商连接 |
| IB 行情链路 | 代码已实现，当前未做真实运行验证 |
| IB 交易链路 | 当前代码不支持 |

当前最重要的事实只有一条：

- 现在已经确认 `Client/SM/TS` 之间的主链路可以真实工作
- 但 `TS -> 券商` 被环境条件拦住，当前不能证明“已经能真实操作券商侧功能”

## 一、TS 对外 API 静态清单

| API | 静态结论 | 说明 | 代码依据 |
|---|---|---|---|
| `CONNECT` | 条件有效 | 首包必须为 `CONNECT`，并通过 SM 的 `/auth/verify-token` 校验；TS 不能脱离 SM 单独完成认证。 | [network/ws_server.py](./network/ws_server.py) |
| `PING` / `PONG` | 条件有效 | 心跳逻辑存在。 | [network/ws_server.py](./network/ws_server.py) |
| `ECONOMIC_DATA_QUERY` | 条件有效 | 本地经济数据查询链路存在。 | [network/ws_server.py](./network/ws_server.py) |
| `STATUS_QUERY` | 条件有效 | 本地状态查询链路存在。 | [network/ws_server.py](./network/ws_server.py) |
| `SUMMARY_REPORT` | 条件有效 | 本地汇总报告链路存在。 | [network/ws_server.py](./network/ws_server.py) |
| `BROKER_LOGIN` | 条件有效 | 当前只是 TS 本地“交易服务登录”gate，不是券商真实登录。 | [network/ws_server.py](./network/ws_server.py), [config.py](./config.py) |
| `BROKER_STATUS_QUERY` | 条件有效 | 查询的是 gate 状态，不是券商真实会话状态。 | [network/ws_server.py](./network/ws_server.py) |
| `BROKER_LOGOUT` | 条件有效 | 只清理本地 gate。 | [network/ws_server.py](./network/ws_server.py) |
| `ORDER_SUBMIT` | 条件有效 | 调用链完整，结果取决于 broker 是否已连上。 | [network/ws_server.py](./network/ws_server.py), [services/trading_svc.py](./services/trading_svc.py) |
| `ORDER_CANCEL` | 条件有效 | 调用链完整，结果取决于 broker 是否已连上。 | [network/ws_server.py](./network/ws_server.py), [services/trading_svc.py](./services/trading_svc.py) |
| `POSITION_QUERY` | 条件有效 | 调用链完整，结果取决于 broker 是否已连上。 | [network/ws_server.py](./network/ws_server.py), [services/trading_svc.py](./services/trading_svc.py) |
| `ORDER_QUERY` | 条件有效 | 调用链完整，支持 `live` / `all`。 | [network/ws_server.py](./network/ws_server.py), [services/trading_svc.py](./services/trading_svc.py) |
| `QUOTE_SUBSCRIBE` | 条件有效 | TS 侧订阅/退订链路完整，结果取决于 broker 行情连接。 | [network/ws_server.py](./network/ws_server.py), [services/quote_provider.py](./services/quote_provider.py) |

## 二、按 broker 能力划分的静态结论

| Broker | 能力 | 静态结论 | 说明 | 代码依据 |
|---|---|---|---|---|
| `tastytrade` | 连接 | 条件有效 | 已实现 `Session(secret, token)` + `Account.get(session)`。 | [api/tastytrade.py](./api/tastytrade.py) |
| `tastytrade` | 下单 | 条件有效 | 已实现 `Equity.get()`、`build_leg()`、`place_order()`。 | [api/tastytrade.py](./api/tastytrade.py) |
| `tastytrade` | 撤单 | 条件有效 | 已实现 `delete_order()`。 | [api/tastytrade.py](./api/tastytrade.py) |
| `tastytrade` | 持仓查询 | 条件有效 | 已实现 `get_positions()`。 | [api/tastytrade.py](./api/tastytrade.py) |
| `tastytrade` | 活动订单查询 | 条件有效 | 已实现 `get_live_orders()`。 | [api/tastytrade.py](./api/tastytrade.py) |
| `tastytrade` | 历史订单查询 | 条件有效 | 已实现 `get_order_history()`。 | [api/tastytrade.py](./api/tastytrade.py) |
| `tastytrade` | 行情订阅 | 条件有效 | 已实现 `DXLinkStreamer`、`subscribe`、`unsubscribe`。 | [api/tastytrade.py](./api/tastytrade.py) |
| `interactive_brokers` | 行情订阅 | 条件有效 | 已实现 `reqMktData` / `cancelMktData`。 | [api/interactive_brokers.py](./api/interactive_brokers.py) |
| `interactive_brokers` | 下单 | 不支持 | 当前代码明确抛 `NotImplementedError`。 | [api/interactive_brokers.py](./api/interactive_brokers.py) |
| `interactive_brokers` | 撤单 | 不支持 | 当前代码明确抛 `NotImplementedError`。 | [api/interactive_brokers.py](./api/interactive_brokers.py) |
| `interactive_brokers` | 持仓查询 | 不支持 | 当前代码明确抛 `NotImplementedError`。 | [api/interactive_brokers.py](./api/interactive_brokers.py) |
| `interactive_brokers` | 订单查询 | 不支持 | 未覆写 `get_orders()`，沿用基类默认不支持实现。 | [api/base.py](./api/base.py) |

## 三、2026-06-27 真实联调测试结果

### 1. 基础联通与会话链路

| 测试项 | 本轮结果 | 是否需复测 | 说明 |
|---|---|---|---|
| `SM /ping` | 已通过 | 否 | 返回 `200 {"status":"pong"}` |
| `TS /health` | 已通过 | 否 | 返回 `200`，节点状态 `running` |
| `SM /auth/login` | 已通过 | 否 | 用户 `test/test` 登录成功，返回 client token |
| `SM /api/nodes/{server_id}/occupy` | 已通过 | 否 | 节点成功被 `test` 占用 |
| `TS CONNECT` | 已通过 | 否 | 成功建立 WS 连接，会话 `sess_72401fec4ddd5f63` |
| `TS BROKER_LOGIN` | 已通过 | 否 | 返回 `TRADE_SERVICE_LOGIN_OK`，gate 进入 `active=true` |
| `TS BROKER_STATUS_QUERY` | 已通过 | 否 | 返回 `BROKER_STATUS_OK` |
| `TS STATUS_QUERY` | 已通过 | 否 | 返回节点状态、连接数、gate 状态 |
| `TS BROKER_LOGOUT` | 已通过 | 否 | gate 已成功清空 |
| `SM /api/nodes/{server_id}/release` | 已通过 | 否 | 节点释放成功 |

### 2. 交易与行情链路

| 测试项 | 本轮结果 | 是否需复测 | 当前实际返回 | 结论 |
|---|---|---|---|---|
| `TS POSITION_QUERY` | 未通过 | 是 | `BROKER_OFFLINE` | 已真实打到 TS，但 broker 未连上 |
| `TS ORDER_QUERY(live)` | 未通过 | 是 | `BROKER_OFFLINE` | 已真实打到 TS，但 broker 未连上 |
| `TS QUOTE_SUBSCRIBE` | 未通过 | 是 | `success=false, message="Broker not connected"` | 已真实打到 TS，但 broker 未连上 |
| `TS ORDER_SUBMIT` | 未通过 | 是 | `BROKER_OFFLINE` | 已真实打到 TS，但 broker 未连上，未进入真实券商下单阶段 |

### 3. 本轮真实测试结论

| 分类 | 本轮结果 | 是否需复测 | 说明 |
|---|---|---|---|
| `SM -> TS` 认证链路 | 已通过 | 否 | 已确认可以真实登录、占用、连接、释放 |
| `TS` 本地 gate 链路 | 已通过 | 否 | 已确认登录、状态、登出都正常 |
| `TS -> broker` 持仓查询 | 未通过 | 是 | 被 `BROKER_OFFLINE` 拦住 |
| `TS -> broker` 订单查询 | 未通过 | 是 | 被 `BROKER_OFFLINE` 拦住 |
| `TS -> broker` 行情订阅 | 未通过 | 是 | 被 `Broker not connected` 拦住 |
| `TS -> broker` 下单 | 未通过 | 是 | 被 `BROKER_OFFLINE` 拦住 |

## 四、为什么当前券商侧没有通过

| 阻塞项 | 当前状态 | 证据 | 对测试结果的影响 |
|---|---|---|---|
| Tastytrade SDK 未安装 | 已确认 | TS 日志出现 `Tastytrade SDK not installed` | TS 无法建立 tastytrade broker 连接 |
| IB SDK 未安装 | 已确认 | TS 日志出现 `ibapi not installed` | IB 行情链路当前也无法在本机实跑 |
| 当前 TS 节点 broker 未连上 | 已确认 | 交易相关 API 统一返回 `BROKER_OFFLINE` 或 `Broker not connected` | 持仓/订单/行情/下单均未进入真实券商执行阶段 |
| 当前节点配置不是已验证的真实可用券商环境 | 已确认 | 本轮只能验证到 TS 层，未验证到真实券商响应 | 需要后续补真实凭证与 SDK 环境后复测 |

## 五、后续必须复测的项目

下面这些项当前都不是“代码未实现”，而是“真实券商条件未就绪”，因此都必须在补齐环境后再次测试。

| 需复测项 | 复测前提 | 复测目标 |
|---|---|---|
| `POSITION_QUERY` | 安装 tastytrade SDK，TS 能成功连接 broker | 确认能返回真实持仓数据 |
| `ORDER_QUERY(live/all)` | 安装 tastytrade SDK，TS 能成功连接 broker | 确认能返回真实订单数据 |
| `QUOTE_SUBSCRIBE` | 安装 tastytrade SDK 与 DX 行情依赖，TS 能成功连接 broker | 确认真正收到行情推送 |
| `ORDER_SUBMIT` | 安装 tastytrade SDK，TS 能成功连接 broker，具备真实可用凭证 | 确认能真实触发券商下单 |
| `ORDER_CANCEL` | 先能真实下单，再具备可撤订单 | 确认能真实撤单 |

## 六、关键代码事实

### 1. TS 的交易操作都会先过 gate

`ORDER_SUBMIT`、`ORDER_CANCEL`、`POSITION_QUERY`、`ORDER_QUERY` 在进入 broker 前，都会先检查：

- 当前 Client 对应 gate 是否已打开
- 当前 broker 是否已连接
- 当前 broker 实例是否存在

对应代码见 [services/trading_svc.py](./services/trading_svc.py) 中的 `_get_ready_broker()`。

### 2. `BROKER_LOGIN` 当前不是券商真实登录

当前 `BROKER_LOGIN` 需要的是：

- `account_username`
- `account_password`

但它最终调用的是 [config.py](./config.py) 中的 `verify_trade_service_login()`，而不是 tastytrade 官方账户登录接口。

### 3. tastytrade 适配器的调用方式与原型一致

当前 [api/tastytrade.py](./api/tastytrade.py) 中以下逻辑，与 [origin_demo/server.py](../origin_demo/server.py) 的原型调用方式一致：

- `Session(secret, token)`
- `Account.get(session)`
- `Equity.get(session, symbol)`
- `place_order`
- `delete_order`
- `get_positions`
- `get_live_orders`
- `get_order_history`
- `DXLinkStreamer`

这说明当前迁移方向在“调用方式”上是延续原型的，不是新造了一套接口。

## 七、最终归纳

| 问题 | 当前结论 |
|---|---|
| TS 接口是不是都没接上 | 不是，绝大多数都接上了 |
| SM 到 TS 的链路是否可用 | 可用，已真实测试通过 |
| TS 本地交易服务登录是否可用 | 可用，已真实测试通过 |
| 交易相关 API 是否已真实打到 TS | 已经打到 |
| 当前是否已真实操作到券商侧 | 还没有 |
| 当前失败更像代码缺失还是环境阻塞 | 当前更像环境阻塞 |

## 参考

- tastytrade 官方文档：[Getting Started](https://developer.tastytrade.com/getting-started/)


## 2026-07-09 TS 后台接口与 WS 占用/释放复测结果

本轮在隔离目录中启动独立 `Server_manager` 与 `Trader_Server`，不复用当前正式数据库和 TS 配置。测试结论如下：

| 测试项 | 结果 | 说明 |
|---|---|---|
| `SM /ping` | 已通过 | SM 隔离实例可访问 |
| `TS /health` | 已通过 | TS 返回 `status=ok` |
| `TS /api/status` | 已通过 | 可读取注册、心跳、连接数、broker 状态 |
| `TS /api/logs` | 已通过 | 可读取消息日志与统计 |
| `TS /api/logs/clear` | 已通过 | 可清空消息日志 |
| `TS /api/economic-data` | 已通过 | 可返回本地经济数据指标 |
| `TS /api/summary` | 已通过 | 可返回本地摘要报告 |
| `TS /api/register/ping` | 已通过 | TS 可通过本地 API 探测 SM 连通性 |
| `TS /api/register/submit` | 已通过 | 可提交注册申请并返回 `request_id` |
| `TS /api/register/pre-approve-check` | 已通过 | 审批前校验返回 `can_approve=true` |
| `SM /api/nodes/{request_id}/approve` | 已通过 | SM 审批后返回 `server_id` 与节点 token |
| `TS /api/register/await-approval` | 已通过 | TS 代理 SSE 可收到 `approved=true` 并落地注册状态 |
| `SM /auth/login` | 已通过 | 隔离环境创建 `test/test` 交易员账号后可登录并获得 Client token |
| `SM /api/nodes/{server_id}/occupy` | 已通过 | `test` 用户可占用对应 TS 节点 |
| `TS WS CONNECT` | 已通过 | Client token 经 SM 校验后获得 `CONNECT_ACK` |
| `TS WS BROKER_LOGIN` | 已通过 | 使用开发后门 `test/test` 返回 `TRADE_SERVICE_LOGIN_OK` |
| `TS WS BROKER_STATUS_QUERY` | 已通过 | gate 返回 `active=true` |
| `TS WS STATUS_QUERY` | 已通过 | 返回节点状态、连接数与 gate 状态 |
| `SM /api/nodes/{server_id}/release` | 已通过 | 节点占用可释放，TS 连接数恢复为 0 |
| `TS /api/admin/force-disconnect` 未授权保护 | 已通过 | 无 Bearer token 时返回 401 |
| `TS /api/register/clear` | 已通过 | 可清理本地注册凭证 |
| `TS /api/register/cancel` 参数校验 | 已通过 | 缺少 `request_id` 时返回错误 |

补充说明：第一轮 WS 测试曾因隔离数据库没有 `test/test` Client 账号导致 SM 登录失败；随后通过 SM 管理接口创建测试交易员账号后，WS 占用/释放链路完整通过。该失败属于测试环境准备不足，不是 TS WS 链路失败。

仍需复测的部分不变：真实 tastytrade / IB 券商侧操作仍依赖真实凭证、SDK 与券商环境，本轮没有证明真实券商侧下单、撤单、持仓、订单、行情推送已跑通。