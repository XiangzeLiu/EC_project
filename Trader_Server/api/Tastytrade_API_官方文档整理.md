·# Tastytrade API 官方文档整理

## 文档目的

这份文档用于给 `EC_project` 后续接入 `tastytrade` 券商时做官方资料索引。内容只基于 2026-06-24 当天从官方开发者站点稳定提取到的页面结构与正文事实整理，不基于第三方转述，不臆测未展示的接口细节。

本次整理结果分成两部分：

- 官方文档全集索引：把当前导航中出现的 API 文档页面和分类全部列出来，方便后续逐项对接。
- 当前可直接提取的关键接入事实：认证、环境、时效、错误排查等能从官方页面正文直接确认的信息。

说明：

- 官方站点大量 API 正文为前端动态渲染，本次环境下可以稳定提取完整导航与部分页面正文。
- 因此，这份文档已经覆盖“官方 API 页面全集索引”，但不等同于“每个接口字段级别的完整离线镜像”。
- 后续如果要做字段级映射、请求参数校验、错误码枚举落表，还需要在逐个页面人工打开或补抓动态内容后继续细化。

## 官方入口

- 主入口：https://developer.tastytrade.com/getting-started/
- API Overview：https://developer.tastytrade.com/api-overview/
- Sandbox/Test Environment：https://developer.tastytrade.com/sandbox/
- Streaming Market Data：https://developer.tastytrade.com/streaming-market-data/
- Streaming Account Data：https://developer.tastytrade.com/streaming-account-data/
- FAQ：https://developer.tastytrade.com/faq/

## 当前可直接确认的官方事实

### 1. Sandbox 环境

来源页面：https://developer.tastytrade.com/sandbox/

- Sandbox 是给 open-api 用户使用的受控环境。
- Sandbox 每 24 小时重置一次。
- 重置会清空 trades、transactions、positions、balances。
- Users、customers、accounts 不会被删除。
- Sandbox API base URL 是 `api.cert.tastyworks.com`。
- Sandbox account streamer WebSocket URL 是 `streamer.cert.tastyworks.com`。
- Sandbox 行情始终延迟 15 分钟。

### 2. 认证与时效

来源页面：https://developer.tastytrade.com/faq/

- Access token 有效期是 15 分钟。
- 后续每个请求都要在 `Authorization` header 中携带有效 access token。
- Sandbox 和 production 使用不同的一套账号密码，不能混用。
- 如果用错环境登录，官方 FAQ 明确会出现 `invalid_credentials`。

### 3. User-Agent 要求

来源页面：https://developer.tastytrade.com/faq/

- 官方对 `User-Agent` 有格式要求。
- 格式要求是 `<product>/<version>`。
- 不满足时，可能返回 401。

### 4. 登录失败与封禁

来源页面：https://developer.tastytrade.com/faq/

- 短时间内过多失败登录尝试会导致 IP 被直接阻断。
- 被阻断期间，请求表现为 timeout。
- FAQ 中说明典型封禁时长约 8 小时。

### 5. 邮箱确认要求

来源页面：https://developer.tastytrade.com/faq/

- 用户注册后需要在 3 天内完成邮箱确认。
- 未确认可能收到 `unconfirmed_user` 错误。

### 6. Production 基础地址

来源页面：https://developer.tastytrade.com/faq/

- FAQ 明确区分了 sandbox 与 production。
- Production base URL 是 `https://api.tastyworks.com`。

## 文档结构总览

官方站点当前导航可归为以下大类：

- Getting Started
- API Overview
- API Docs
- API Guides
- Oauth2
- Order Submission
- Order Flow
- Order Management
- Streaming Market Data
- Streaming Account Data
- Sandbox
- SDKs
- Release Notes
- FAQ

## 全量页面索引

### 1. Getting Started

说明：以下条目在导航中独立显示，但当前都挂在同一个页面 URL `https://developer.tastytrade.com/getting-started/` 下，属于单页分段内容。

| 条目 | URL |
| --- | --- |
| Getting Started | https://developer.tastytrade.com/getting-started/ |
| Create Sandbox Account | https://developer.tastytrade.com/getting-started/ |
| Login Sandbox Account | https://developer.tastytrade.com/getting-started/ |
| Submit a Trade | https://developer.tastytrade.com/getting-started/ |
| Fetch Balance and Positions | https://developer.tastytrade.com/getting-started/ |
| Stream Market Data | https://developer.tastytrade.com/getting-started/ |
| Fetch Market Data | https://developer.tastytrade.com/getting-started/ |
| Stream Account Updates | https://developer.tastytrade.com/getting-started/ |
| Close a Position | https://developer.tastytrade.com/getting-started/ |
| Fetch Option Chain | https://developer.tastytrade.com/getting-started/ |
| Help | https://developer.tastytrade.com/getting-started/ |

### 2. API Overview

说明：导航中的 `API Conventions`、`Api Versions`、`Auth Patterns`、`tastytrade Symbology`、`Error Codes`、`High-level Concepts` 当前都归在 `https://developer.tastytrade.com/api-overview/` 这一页的分段中。

| 条目 | URL |
| --- | --- |
| API Overview | https://developer.tastytrade.com/api-overview/ |
| API Conventions | https://developer.tastytrade.com/api-overview/ |
| Api Versions | https://developer.tastytrade.com/api-overview/ |
| Auth Patterns | https://developer.tastytrade.com/api-overview/ |
| tastytrade Symbology | https://developer.tastytrade.com/api-overview/ |
| Error Codes | https://developer.tastytrade.com/api-overview/ |
| High-level Concepts | https://developer.tastytrade.com/api-overview/ |

### 3. API Docs

说明：这一组是官方按业务域拆分的 Open API 文档入口，后续做 TS 接口映射时应优先逐组核对。

| 模块 | URL |
| --- | --- |
| Account Status | https://developer.tastytrade.com/open-api-spec/account-status/ |
| Accounts and Customers | https://developer.tastytrade.com/open-api-spec/accounts-and-customers/ |
| Backtesting | https://developer.tastytrade.com/open-api-spec/backtesting/ |
| Balances and Positions | https://developer.tastytrade.com/open-api-spec/balances-and-positions/ |
| Instruments | https://developer.tastytrade.com/open-api-spec/instruments/ |
| Margin Requirements | https://developer.tastytrade.com/open-api-spec/margin-requirements/ |
| Market Data | https://developer.tastytrade.com/open-api-spec/market-data/ |
| Market Metrics | https://developer.tastytrade.com/open-api-spec/market-metrics/ |
| Market Sessions | https://developer.tastytrade.com/open-api-spec/market-sessions/ |
| Net Liquidating Value History | https://developer.tastytrade.com/open-api-spec/net-liquidating-value-history/ |
| Orders | https://developer.tastytrade.com/open-api-spec/orders/ |
| Quote Alerts | https://developer.tastytrade.com/open-api-spec/quote-alerts/ |
| Risk Parameters | https://developer.tastytrade.com/open-api-spec/risk-parameters/ |
| Symbol Search | https://developer.tastytrade.com/open-api-spec/symbol-search/ |
| Transactions | https://developer.tastytrade.com/open-api-spec/transactions/ |
| Watchlists | https://developer.tastytrade.com/open-api-spec/watchlists/ |

### 4. API Guides

说明：这一组更偏“按场景使用 API”的说明文档，适合先读完再设计 TS 的调用编排。

| 条目 | URL |
| --- | --- |
| Oauth2 | https://developer.tastytrade.com/api-guides/oauth/ |
| Customer Account Info | https://developer.tastytrade.com/api-guides/customer-account-info/ |
| Account Status | https://developer.tastytrade.com/api-guides/account-status/ |
| Account Positions | https://developer.tastytrade.com/api-guides/account-positions/ |
| Account Balances | https://developer.tastytrade.com/api-guides/account-balances/ |
| Account Transactions | https://developer.tastytrade.com/api-guides/account-transactions/ |
| Instruments | https://developer.tastytrade.com/api-guides/instruments/ |
| Margin Requirements | https://developer.tastytrade.com/api-guides/margin-requirements/ |
| Market Data | https://developer.tastytrade.com/api-guides/market-data/ |

### 5. Oauth2

| 条目 | URL |
| --- | --- |
| Oauth2 | https://developer.tastytrade.com/oauth/ |

### 6. Order Submission

说明：以下条目当前都在 `https://developer.tastytrade.com/order-submission/` 单页中，以分段方式组织。

| 条目 | URL |
| --- | --- |
| Order Submission | https://developer.tastytrade.com/order-submission/ |
| Order Attributes | https://developer.tastytrade.com/order-submission/ |
| Order Type | https://developer.tastytrade.com/order-submission/ |
| Price | https://developer.tastytrade.com/order-submission/ |
| Time In Force | https://developer.tastytrade.com/order-submission/ |
| Value | https://developer.tastytrade.com/order-submission/ |
| Leg Attributes | https://developer.tastytrade.com/order-submission/ |
| Action | https://developer.tastytrade.com/order-submission/ |
| Instrument Type | https://developer.tastytrade.com/order-submission/ |
| Quantity | https://developer.tastytrade.com/order-submission/ |
| Symbol | https://developer.tastytrade.com/order-submission/ |
| Order Responses | https://developer.tastytrade.com/order-submission/ |
| Rejected | https://developer.tastytrade.com/order-submission/ |
| Accepted | https://developer.tastytrade.com/order-submission/ |
| Example Orders | https://developer.tastytrade.com/order-submission/ |
| Complex Orders | https://developer.tastytrade.com/order-submission/ |
| Fractional Stock Orders | https://developer.tastytrade.com/order-submission/ |

### 7. Order Flow

说明：以下条目当前都在 `https://developer.tastytrade.com/order-flow/` 单页中。

| 条目 | URL |
| --- | --- |
| Order Flow | https://developer.tastytrade.com/order-flow/ |
| Phases | https://developer.tastytrade.com/order-flow/ |
| Order Status Definitions | https://developer.tastytrade.com/order-flow/ |
| Examples | https://developer.tastytrade.com/order-flow/ |

### 8. Order Management

说明：以下条目当前都在 `https://developer.tastytrade.com/order-management/` 单页中。

| 条目 | URL |
| --- | --- |
| Order Management | https://developer.tastytrade.com/order-management/ |
| Search Orders | https://developer.tastytrade.com/order-management/ |
| Live Orders | https://developer.tastytrade.com/order-management/ |
| Order Dry Run | https://developer.tastytrade.com/order-management/ |
| Submit Order | https://developer.tastytrade.com/order-management/ |
| Cancel Order | https://developer.tastytrade.com/order-management/ |
| Cancel Replace | https://developer.tastytrade.com/order-management/ |
| Complex Orders | https://developer.tastytrade.com/order-management/ |
| Submit Complex Order | https://developer.tastytrade.com/order-management/ |
| Cancel Complex Order | https://developer.tastytrade.com/order-management/ |

### 9. Streaming Market Data

说明：以下条目当前都在 `https://developer.tastytrade.com/streaming-market-data/` 单页中。

| 条目 | URL |
| --- | --- |
| Streaming Market Data | https://developer.tastytrade.com/streaming-market-data/ |
| Get an Api Quote Token | https://developer.tastytrade.com/streaming-market-data/ |
| DXLink Streamer | https://developer.tastytrade.com/streaming-market-data/ |
| DXLink Symbology | https://developer.tastytrade.com/streaming-market-data/ |
| DXLink Documentation | https://developer.tastytrade.com/streaming-market-data/ |
| Candle Events | https://developer.tastytrade.com/streaming-market-data/ |

### 10. Streaming Account Data

说明：以下条目当前都在 `https://developer.tastytrade.com/streaming-account-data/` 单页中。

| 条目 | URL |
| --- | --- |
| Streaming Account Data | https://developer.tastytrade.com/streaming-account-data/ |
| Getting Started | https://developer.tastytrade.com/streaming-account-data/ |
| Available Actions | https://developer.tastytrade.com/streaming-account-data/ |
| Receiving Notifications | https://developer.tastytrade.com/streaming-account-data/ |
| Notification Nuances | https://developer.tastytrade.com/streaming-account-data/ |
| Hosts | https://developer.tastytrade.com/streaming-account-data/ |
| Demo | https://developer.tastytrade.com/account-streamer-demo/ |

### 11. Sandbox / SDK / Release Notes / FAQ

| 条目 | URL |
| --- | --- |
| Sandbox | https://developer.tastytrade.com/sandbox/ |
| SDKs | https://developer.tastytrade.com/sdk/ |
| Release Notes | https://developer.tastytrade.com/release-notes/ |
| FAQ | https://developer.tastytrade.com/faq/ |

## 对 `EC_project` 后续对接最关键的关注点

按当前项目语境，后续检查 `Client -> Trader_Server -> tastytrade` 链路时，优先看下面这些官方文档组：

- 认证与会话：`API Overview`、`FAQ`、`Sandbox`、`Oauth2`
- 账户与持仓：`Accounts and Customers`、`Balances and Positions`、`Account Positions`、`Account Balances`
- 下单：`Orders`、`Order Submission`、`Order Flow`、`Order Management`
- 行情：`Market Data`、`Streaming Market Data`
- 账户流：`Streaming Account Data`
- 标的与检索：`Instruments`、`Symbol Search`、`Market Sessions`

## 当前这份文档的边界

这份文档已经能支持下面几件事情：

- 让后续开发者快速定位 tastytrade 官方资料入口。
- 为 TS 增加 tastytrade 适配层时，先按模块拆任务。
- 为 Client 触发 TS API 的联调，先区分“登录认证链路”“账户数据链路”“下单链路”“行情链路”“账户推送链路”。

这份文档当前还不能替代下面几件事情：

- 不能替代每个 Open API 页面里的字段级请求/响应定义核对。
- 不能替代错误码全集或状态机全集的离线镜像。
- 不能替代对 streaming message schema 的逐字段适配设计。

## 后续建议使用方式

- 先以 `API Docs` 的业务域模块为主，建立 TS 适配层的接口清单。
- 再以 `API Guides` 和 `Order Submission / Order Flow / Order Management` 作为调用编排参考。
- 遇到登录失败、401、超时、环境混用等问题，优先翻 `FAQ` 和 `Sandbox`。
- 做行情和账户推送时，分别对照 `Streaming Market Data` 与 `Streaming Account Data`。
