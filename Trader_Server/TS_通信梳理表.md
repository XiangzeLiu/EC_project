# Trader Server 通信梳理表

更新时间：2026-07-27。

## 1. TS 与 SM

| 方向 | 协议与接口 | 用途 |
|---|---|---|
| TS -> SM | `GET /ping` | 注册前检查 SM 可达性 |
| TS -> SM | `POST /nodes/register-request` | 上报节点名称、公网 IP、能力并申请注册 |
| TS <- SM | `GET /nodes/await-approval` SSE | 等待审批结果，取得 `server_id`、节点 Token 和域名 |
| TS -> SM | `POST /nodes/heartbeat` | 心跳、占用状态和配置版本同步 |
| TS -> SM | `GET /api/nodes/config` | 拉取券商类型、OAuth 凭证、Account Number 和配置版本 |
| TS <- SM | `CONFIG_CHANGED` SSE | 券商配置修改后立即触发热重载 |
| TS -> SM | `POST /auth/verify-token` | 校验连接当前 TS 的 Client Token 和占用关系 |
| TS <- SM | `POST /api/admin/force-disconnect` | 管理员强制断开当前 Client |
| TS -> SM | `POST /nodes/release-occupation` | Client 断开宽限期后释放 TS 占用 |

SM 审批 tastytrade TS 前必须验证 Client Secret、Refresh Token 和账户列表。审批时保存明确的 Account Number；TS 不自行选择其他账户兜底。

## 2. TS 券商会话生命周期

```text
TS 启动/审批完成
  -> 从 SM 拉取 broker config
  -> Session(Client Secret, Refresh Token)
  -> 查询 OAuth grant 可访问账户
  -> 严格选择配置的 Account Number
  -> 建立 TS 全局券商 Session
  -> Client 连接、断线、重连均复用该 Session
```

券商 Session 只在 TS 关闭、配置清理或配置热更新时销毁。Client 退出不会触发券商登出。

## 3. TS 接收 Client 消息

| 消息 | 处理职责 |
|---|---|
| `CONNECT` | 首包鉴权；向 SM 复验 Token、绑定节点和 connection_id |
| `STATUS_QUERY` | 返回节点状态和 `broker_detail` |
| `BROKER_STATUS_QUERY` | 返回真实 OAuth Session、所选账户、错误和 capability |
| `QUOTE_SUBSCRIBE` | 管理订阅集合并调用券商行情流 |
| `POSITION_QUERY` | 查询所选账户持仓 |
| `ORDER_QUERY` | 查询所选账户活动/历史订单 |
| `ORDER_SUBMIT` | 校验参数和 `orders` capability 后真实下单 |
| `ORDER_CANCEL` | 校验 `cancel_order` capability 后真实撤单 |
| `PING` | 返回 `PONG` |

`BROKER_LOGIN`、`BROKER_LOGOUT`、用户名密码、验证码和 `broker_gate` 已删除。

## 4. TS 发给 Client 消息

| 消息 | 核心字段 |
|---|---|
| `CONNECT_ACK` | `session_id` `node_info` `broker_detail` |
| `STATUS_RESPONSE` | `node_info` `broker_detail` |
| `BROKER_STATUS_RESPONSE` | `broker_detail` |
| `BROKER_STATUS_CHANGE` | `status` `config_version` `broker_detail` |
| `QUOTE_ACK` / `QUOTE_DATA` | 订阅结果与实时行情 |
| `POSITION_RESPONSE` | 持仓结果或稳定错误码 |
| `ORDER_LIST_RESPONSE` | 订单结果或稳定错误码 |
| `ORDER_RESPONSE` | 下单结果或稳定错误码 |
| `ORDER_CANCEL_RESPONSE` | 撤单结果或稳定错误码 |
| `FORCE_DISCONNECT` | 管理员释放原因 |
| `ERROR` / `PONG` | 通用错误与保活 |

## 5. 账户权限

| Authority | 行情 | 持仓 | 订单查询 | 下单 | 撤单 |
|---|---:|---:|---:|---:|---:|
| `owner` | 是 | 是 | 是 | 是 | 是 |
| `trade-only` | 是 | 是 | 是 | 是 | 是 |
| `read-only` | 是 | 是 | 是 | 否 | 否 |

TS 在 `effective_capabilities()` 中应用运行时账户权限。服务端仍会再次校验 capability，不能只依赖 Client 按钮禁用。

## 6. 单 Client 锁定

移除 broker gate 不影响 TS 单 Client 模型：

- SM 仍在 Client 建立 WSS 前登记节点占用。
- TS 新连接仍替换同节点旧连接。
- Client 异常断开后，TS 保留短宽限期再通知 SM 释放占用。
- SM force-release 仍可强制关闭当前连接。

## 7. 当前自动化结论

2026-07-27 本地虚拟环境全量回归 `91/91` 通过，覆盖 OAuth 凭证预验证、审批复验、原子保存、严格账户匹配、read-only capability、无运行时登录的持仓查询、Client 查询/交易门控和单 Client 连接生命周期。
