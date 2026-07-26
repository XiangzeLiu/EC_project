# TS API 有效性清单

更新时间：2026-07-27。

## 总体结论

- Client、SM、TS 的 HTTPS/WSS 登录、固定 TS 绑定、占用和长连接链路已在生产服务器验证。
- tastytrade 认证模型已改为 TS 管理 OAuth Session，不再由 Client 输入券商用户名密码。
- SM 审批会验证 Client Secret、Refresh Token 和账户列表，并保存明确 Account Number。
- 本地自动化回归 `91/91` 通过。
- 自动化测试没有提交真实订单；实盘持仓、订单、行情、下单和撤单仍需在部署环境人工验收。

## 1. TS WebSocket API

| API | 状态 | 说明 |
|---|---|---|
| `CONNECT` | 有效 | 首包鉴权，通过 SM 复验 Client Token 和节点绑定 |
| `PING` / `PONG` | 有效 | WSS 心跳保活 |
| `STATUS_QUERY` | 有效 | 返回节点和真实 `broker_detail` |
| `BROKER_STATUS_QUERY` | 有效 | 返回 TS OAuth Session、账户、错误和 capability |
| `QUOTE_SUBSCRIBE` | 条件有效 | 依赖券商 Session 和行情权限 |
| `POSITION_QUERY` | 条件有效 | 依赖 `positions=true` |
| `ORDER_QUERY` | 条件有效 | 依赖 `order_query=true` |
| `ORDER_SUBMIT` | 条件有效 | 依赖 `orders=true`；read-only 拒绝 |
| `ORDER_CANCEL` | 条件有效 | 依赖 `cancel_order=true`；read-only 拒绝 |
| `ECONOMIC_DATA_QUERY` | 有效 | TS 本地经济数据 |
| `SUMMARY_REPORT` | 有效 | TS 本地摘要 |
| `BROKER_LOGIN` | 已删除 | Client 不再执行二次券商登录 |
| `BROKER_LOGOUT` | 已删除 | Client 退出不销毁 TS 券商 Session |

## 2. tastytrade 适配器

| 能力 | 实现 | 运行边界 |
|---|---|---|
| OAuth 连接 | `Session(client_secret, refresh_token)` | 凭证必须由 SM 审批验证 |
| 账户选择 | 查询 `/customers/me/accounts` 后严格匹配 Account Number | 指定账户不存在时失败，不回退 |
| 默认账户 | Account Number 留空时首个开放账户 | SM 会把解析结果保存为明确 Account Number |
| 行情 | `DXLinkStreamer` | 依赖券商行情授权与网络 |
| 持仓 | `Account.get_positions()` | owner、trade-only、read-only 均可查询 |
| 订单查询 | live/history API | owner、trade-only、read-only 均可查询 |
| 下单 | `place_order()` | read-only 禁止 |
| 撤单 | `delete_order()` | read-only 禁止 |

## 3. 稳定错误边界

| 场景 | 错误码/行为 |
|---|---|
| OAuth 凭证无效 | SM 阻止审批；TS 运行时为 `BROKER_AUTH_INVALID` |
| 没有账户 | SM 阻止审批；TS 为 `BROKER_ACCOUNT_MISSING` |
| 指定账户不存在 | SM 阻止审批；TS 为 `BROKER_ACCOUNT_NOT_FOUND` |
| 账户已关闭 | SM 阻止审批；TS 为 `BROKER_ACCOUNT_CLOSED` |
| 券商 Session 离线 | `BROKER_OFFLINE` |
| read-only 下单 | `ORDER_NOT_SUPPORTED` |
| read-only 撤单 | `ORDER_CANCEL_NOT_SUPPORTED` |
| 券商不支持某能力 | 对应 `*_NOT_SUPPORTED` |

## 4. 自动化覆盖

当前测试覆盖：

- 有效单账户、多账户和默认首个开放账户。
- 无效凭证、无账户、仅关闭账户、指定账户不存在。
- read-only 允许审批并返回警告。
- 审批接口不能绕过验证按钮。
- 验证操作不提前保存凭证。
- 审批事务原子保存 Account Number 与 OAuth 凭证。
- 节点列表不向浏览器返回完整券商凭证。
- TS 指定账户不存在时不回退。
- read-only 在 TS 和 Client 两端均禁止下单、撤单。
- 不发送 `BROKER_LOGIN`、`BROKER_LOGOUT`。
- Client 断开不销毁 TS 券商 Session。
- TS 断线重连恢复行情订阅。

## 5. 生产验收

上线前仍需在真实部署环境执行：

1. SM 审批弹窗使用生产 OAuth 凭证执行“验证并获取账户”。
2. 核对账户名称、Account Number、账户类型和 Authority。
3. 审批后确认 TS `broker_detail.connected=true` 且账户号一致。
4. 在 Client 验证行情、持仓和订单查询。
5. owner/trade-only 账户执行最小风险人工下单与撤单测试。
6. read-only 账户确认 Buy、Sell、撤单禁用，但查询功能正常。
7. 验证 Client 正常退出和异常断线后，TS 券商 Session 保持，由 SM 正确释放节点占用。

## 参考

- [OAuth](https://developer.tastytrade.com/api-guides/oauth/)
- [Auth Patterns](https://developer.tastytrade.com/api-overview/#auth-patterns)
- [Customer Account Info](https://developer.tastytrade.com/api-guides/customer-account-info/)
