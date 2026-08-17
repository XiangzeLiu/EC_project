# Interactive Brokers API / SDK 功能说明

> 文档状态：前期技术调研，暂不代表已完成 IB 券商接入。
> 调研日期：2026-07-27
> 资料来源：Interactive Brokers 官方网站、IBKR Campus 官方 API 文档。
> 本文只记录官方 API/SDK 能力和本项目的适配边界，不记录任何账户、密码、Token 或 API 密钥。

## 1. 结论先行

Interactive Brokers（以下简称 IBKR）不是只有一种 API，官方主要提供以下几类接口：

| 接口 | 连接方式 | 主要用途 | 是否适合当前项目第一版 |
|---|---|---|---|
| TWS API | TCP Socket | 行情、账户、持仓、订单、回调流 | **最适合** |
| Web API | HTTPS REST + WebSocket | 交易、账户、行情、组合和会话管理 | 暂不优先 |
| Flex Web Service | HTTPS 请求/报表文件 | 报表、历史账户数据、对账 | 不作为实时交易接口 |
| FIX | FIX 消息协议 | 机构级订单和交易接入 | 当前不考虑 |

结合本项目的三端结构，推荐的 IBKR 接入方向是：

```text
Client
  │ HTTPS/WSS
  ▼
SM：账号、TS、券商配置管理
  │ HTTPS/WSS
  ▼
TS：券商适配器、行情转发、账户和交易 API
  │ 本机 TCP Socket
  ▼
IB Gateway 或 TWS
  │
  ▼
Interactive Brokers
```

TS 和 IB Gateway/TWS 建议部署在同一台 Windows 设备上，优先通过回环地址连接。SM 和 Client 不直接连接 IBKR，也不应直接暴露 IB Gateway/TWS 端口。

当前项目的 IB 适配器已经可以创建并连接 TWS API，但现阶段仅实现行情能力：

- 已支持：连接、断开、重连、股票行情订阅、Bid/Ask/Last/Volume 回调。
- 当前未支持：账户信息、账户列表、持仓、订单查询、下单、撤单。
- 当前适配器中的 `place_order`、`cancel_order`、`get_positions` 会抛出 `NotImplementedError`。

因此，当前代码属于“IB 行情适配雏形”，不能视为完整的 IB 交易接入。

## 2. 官方 API 体系

### 2.1 TWS API

TWS API 是 IBKR 的 TCP Socket API。程序连接到本机或远端运行的 Trader Workstation（TWS）或 IB Gateway，由 API 客户端发送请求，TWS/Gateway 通过回调返回数据和状态。

官方支持的语言包括：

- Python
- Java
- C++
- C#
- Visual Basic .NET

官方文档将 TWS API 描述为异步、事件回调型接口。官方语言库本质上都是对相同 Socket 消息协议的实现。

适合本项目的原因：

- TS 可以长期持有一个券商连接。
- 行情和账户更新天然适合回调转发。
- TWS API 能覆盖行情、账户、持仓、订单和交易生命周期。
- IB Gateway 可以去掉 TWS 图形界面，更适合部署在 TS 服务器。

### 2.2 Web API

IBKR Web API 使用 HTTPS REST 和 WebSocket，官方资料中包含交易、账户、实时组合、市场数据、合约和会话认证等内容。

Web API 和 TWS API 的定位不同：

| 对比项 | TWS API | Web API |
|---|---|---|
| 传输 | TCP Socket | HTTPS REST/WebSocket |
| 接入对象 | TWS 或 IB Gateway | IBKR Web API/Client Portal 体系 |
| 运行依赖 | TWS/Gateway 登录并开启 API | Web API 会话或 OAuth 认证 |
| 事件模型 | EWrapper 回调 | REST 响应、WebSocket 事件 |
| 本项目部署难度 | 较低 | 较高 |
| 适合 TS 长连接 | 高 | 中 |

Web API 不能简单替换为“给 TS 一个 Secret 和 Token”。它有独立的认证、会话保持和连接管理规则。个体账户通过 Client Portal Gateway 使用 Web API 时，还要处理 Gateway 进程、浏览器登录、会话状态和 keep-alive。

### 2.3 Flex Web Service

Flex Web Service 适合定时生成账户报表、交易记录、持仓和对账数据。它不是实时行情或实时下单链路，不能作为当前 Client 主界面的实时券商连接方案。

### 2.4 FIX

FIX 适合机构级订单传输和专门的交易接入，需要单独的 IBKR FIX 工程支持和机构配置。本项目当前不考虑 FIX。

## 3. TWS API 的连接和登录模型

### 3.1 API 不负责登录 IBKR 用户名和密码

TWS API 通常不是直接把 IBKR 用户名、密码和验证码提交给 API。实际流程是：

1. 人工启动 TWS 或 IB Gateway。
2. 人工完成 IBKR 登录、双因素认证和必要的会话确认。
3. 在 TWS/Gateway 中打开 API Socket 访问。
4. TS 使用 `host`、`port` 和 `client_id` 建立 TCP 连接。
5. TWS/Gateway 代表当前登录账户向 API 返回行情、账户和交易结果。

```text
人工登录 IBKR
      │
      ▼
TWS / IB Gateway 已登录
      │ 开启 API Socket
      ▼
TS api.connect(host, port, client_id)
      │
      ▼
nextValidId / managedAccounts / 数据回调
```

所以，IBKR 的“验证 Secret/Token”不能照搬 tastytrade OAuth 的方式。IBKR TWS API 的验证重点是：

- TWS/Gateway 是否已登录；
- API Socket 是否开启；
- TS 到目标 host/port 是否可达；
- `client_id` 是否可用；
- 账户是否允许当前 API 能力；
- 行情订阅和交易权限是否满足要求。

### 3.2 默认端口

实际端口以 TWS/Gateway 的 API 设置为准，常见默认值如下：

| 环境 | TWS 常见端口 | IB Gateway 常见端口 |
|---|---:|---:|
| 实盘 | 7496 | 4001 |
| 模拟盘 | 7497 | 4002 |

端口不是券商账号密码，也不是公网服务端口。生产环境不应把 7496、7497、4001、4002 暴露给公网，TS 与 Gateway 同机时应使用 `127.0.0.1`。

### 3.3 TWS/Gateway API 设置

需要在 TWS 或 IB Gateway 的 API 设置中确认：

- 允许 ActiveX and Socket Clients；
- 端口和 TS 配置一致；
- 如使用远程连接，配置可信 IP；
- 根据需求决定是否勾选 Read-Only API；
- 是否允许下载 Open Orders；
- 是否启用 Master API Client ID；
- 是否允许错误的连接或格式导致断开；
- 交易前确认订单和风险提示设置。

生产建议：

- TS 与 Gateway 同机，`host=127.0.0.1`；
- 防火墙只允许本机访问 API 端口；
- 交易适配器使用固定且唯一的 `client_id`；
- 不使用公网 IP 直接连接 TWS/Gateway；
- Gateway 进程由人工启动，第一版不自动安装为 Windows Service；
- TWS/Gateway 的登录、2FA、每日重认证和周重认证要纳入运维流程。

### 3.4 连接生命周期

TWS API 连接一般按以下顺序处理：

```text
创建 EWrapper/EClient
      │
      ▼
connect(host, port, client_id)
      │
      ▼
启动 app.run() 读取 Socket 回调
      │
      ├─ nextValidId：连接已进入可用阶段
      ├─ managedAccounts：返回可管理账户
      ├─ currentTime：时间/连接探测
      └─ error：连接、权限、节流和请求错误
      │
      ▼
订阅行情 / 请求账户 / 查询持仓 / 提交订单
      │
      ▼
取消订阅、撤销任务、disconnect()
```

不能只以 TCP connect 成功作为“券商 API 已可用”。至少应等待有效连接回调，并执行一个轻量的账户或合约请求。

## 4. 官方 Python SDK 使用方法

### 4.1 官方 SDK 名称

官方 Python 库使用 `ibapi` 包，常见导入方式：

```python
from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract
from ibapi.order import Order
```

典型结构是：

```python
class IBApp(EWrapper, EClient):
    def __init__(self):
        EWrapper.__init__(self)
        EClient.__init__(self, self)

app = IBApp()
app.connect("127.0.0.1", 7496, clientId=19)
app.run()
```

实际工程必须在独立线程或任务中运行 `app.run()`，不能阻塞 TS 的 asyncio 事件循环。项目当前的 `interactive_brokers.py` 已采用“EClient/EWrapper + 后台线程 + asyncio.Queue”的方向。

### 4.2 SDK 获取和安装

官方推荐从 TWS API 下载包中获取对应语言的 API 源码和样例。Windows 部署时建议：

1. 从 IBKR 官方 TWS API 下载页面获取版本包。
2. 使用包内 `source/pythonclient` 或官方 Python 客户端目录。
3. 在 TS 的虚拟环境中安装 `ibapi`。
4. 用相同解释器执行导入测试。

示例检查命令：

```powershell
python -c "from ibapi.client import EClient; from ibapi.wrapper import EWrapper; print('ibapi import ok')"
```

当前项目的 `Trader_Server/requirements.txt` 已固定使用 IB 官方 TWS API 10.49.2 的 Python 客户端，依赖通过官方压缩包的 `source/pythonclient` 子目录安装。不能只安装 SM 的依赖，因为真正连接 IB Gateway/TWS 的代码运行在 TS。

### 4.3 是否使用 ib_insync

`ib_insync` 是第三方封装，不是 IBKR 官方 SDK。它的编程体验更高层，但项目的官方资料、版本维护和异步模型需要单独评估。当前项目已经直接使用官方 `ibapi` 的 EClient/EWrapper 模型，第一版不建议同时引入 `ib_insync`，避免出现两套连接生命周期和事件循环。

## 5. TWS API 功能映射

以下是后续适配最需要的功能类别。名称以官方 TWS API 的 EClient 请求和 EWrapper 回调为准，具体参数应以目标 API 版本参考文档为准。

### 5.1 连接与账户发现

| 目标 | 常用 API/回调 | 说明 |
|---|---|---|
| 建立连接 | `connect` / `run` | 建立 TCP Socket 并读取回调 |
| 判断连接可用 | `nextValidId` | 常作为连接初始化完成信号 |
| 获取可管理账户 | `managedAccounts` | 返回当前 TWS/Gateway 会话可管理的账户列表 |
| 获取账户摘要 | `reqAccountSummary` / `accountSummary` | 返回净值、现金、保证金等摘要标签 |
| 结束账户摘要 | `cancelAccountSummary` | 释放持续订阅 |
| 账户更新流 | `reqAccountUpdates` / `updateAccountValue` / `updatePortfolio` | 适合实时账户和组合变化 |
| 连接错误 | `error` | 统一处理权限、网络、节流、合约和订单错误 |

账户列表不是 OAuth token 对应的静态账户列表，而是当前 TWS/Gateway 登录会话允许 API 访问的账户集合。SM 后续验证弹窗应显示 `managedAccounts` 返回的账户，允许管理员选择要绑定的账户。

### 5.2 合约和标的

IBKR 的合约识别比单纯股票代码严格。常见字段包括：

- `symbol`：代码；
- `secType`：证券类型，例如 `STK`、`OPT`、`FUT`、`CASH`；
- `exchange`：交易所或 SMART；
- `primaryExchange`：主交易所；
- `currency`：币种；
- `lastTradeDateOrContractMonth`：期货或期权到期信息；
- `strike`、`right`、`multiplier`：期权字段；
- `conId`：永久合约 ID。

官方建议在合约明确时尽量使用 `conId` 和交易所组合，避免只凭代码产生歧义。项目第一版不能把所有 Symbol 都默认构造为 `STK/SMART/USD`，需要根据券商和产品类型建立合约解析流程。

常用方法：

```text
reqContractDetails
contractDetails
contractDetailsEnd
```

### 5.3 实时行情

典型流程：

```text
构造 Contract
      │
      ▼
reqMktData(reqId, contract, ...)
      │
      ├─ tickPrice：Bid / Ask / Last 等价格
      ├─ tickSize：Volume 等数量
      ├─ tickString / tickGeneric：其他字段
      └─ error：权限、订阅、合约或节流错误
      │
      ▼
cancelMktData(reqId)
```

注意事项：

- 多数证券需要对应的 Level 1 实时行情订阅；外汇和加密货币的订阅规则不同，需按官方页面确认。
- 没有实时行情权限时，可能只能拿到延迟行情、冻结行情或错误回调。
- API 行情线数量、请求频率和历史行情请求均有 pacing 限制。
- 必须维护 `symbol/reqId` 双向映射，断线或取消订阅时清理映射。
- 行情回调来自 SDK 线程，转发到 TS asyncio 时要使用线程安全队列。

延迟行情相关方法：

```python
app.reqMarketDataType(3)  # delayed，具体行为以官方版本文档为准
```

当前项目已实现 `reqMktData`、`cancelMktData`、`tickPrice` 和 `tickSize` 的股票行情转发，但合约字段仍是固定股票模板。

### 5.4 历史行情

常用方法：

```text
reqHistoricalData
historicalData
historicalDataEnd
cancelHistoricalData
```

历史数据请求需要处理：

- bar size；
- duration；
- whatToShow；
- useRTH；
- formatDate；
- 时区和交易日历；
- HMDS pacing 限制。

当前项目没有历史行情适配，不应把实时 `tickPrice` 当成历史数据接口。

### 5.5 持仓和组合

常用方式：

| 目标 | API/回调 |
|---|---|
| 请求全部持仓 | `reqPositions` |
| 持仓回调 | `position` |
| 持仓结束 | `positionEnd` |
| 请求账户组合流 | `reqAccountUpdates` |
| 组合变化 | `updatePortfolio` |
| 取消持仓请求 | `cancelPositions` |

对当前项目，推荐第一版先实现 `reqPositions`/`position`/`positionEnd`，将结果归一化为 Client 当前使用的持仓结构；如果需要实时成本、市场价和未实现盈亏，再增加 `updatePortfolio` 方案。

必须明确账户维度。多账户会话不能把所有账户的持仓混在一起，TS 内部应按 `account_id` 隔离。

### 5.6 订单和交易

常用流程：

```text
nextValidId
      │
      ▼
构造 Contract + Order
      │
      ├─ 可选：What-If / 影响预估
      ├─ placeOrder(order_id, contract, order)
      │     ├─ openOrder
      │     ├─ orderStatus
      │     ├─ execDetails
      │     └─ commissionReport
      │
      ├─ cancelOrder(order_id)
      └─ 错误/拒单回调
```

订单适配必须处理：

- `nextValidId` 和订单 ID 持久化/递增；
- 客户端 ID、Master Client ID 和其他 API 客户端的订单可见性；
- `transmit`、预览和真正提交的边界；
- 订单状态不是简单的成功/失败，需要处理 Submitted、Filled、Cancelled、Inactive、Rejected 等状态；
- 部分成交和多次执行回调；
- 佣金、成交明细和拒单原因；
- 时区、交易时段、TIF 和合约最小价格变动；
- 断线重连后的订单状态对账。

当前项目的 Client 已有订单模型，但 IB 适配器尚未接入订单。后续不能直接复用 tastytrade 的订单字段，需要建立券商无关订单模型和 IB 字段映射。

## 6. Web API 使用方法概览

### 6.1 Web API 适合什么场景

官方 Web API 提供 REST 和 WebSocket，覆盖：

- 账户和会话认证；
- 交易账户和账户信息；
- 合约查询；
- 实时市场数据；
- 订单提交、查询和取消；
- 实时组合/投资组合更新；
- 扫描器和其他交易服务。

Web API 使用 HTTPS，并以 JSON 返回数据。需要根据账户类型、应用类型和官方当前认证政策选择 OAuth 或 Client Portal Gateway 方案。

### 6.2 Client Portal Gateway

面向个人账户的 Client Portal API 通常需要运行 IBKR 提供的本地 Client Portal Gateway：

```text
TS Windows
  ├─ Client Portal Gateway
  │    └─ 本地 HTTPS API / WebSocket
  └─ Trader_Server
       └─ 调用本机 Gateway
```

它与 TWS API 的差异：

- 需要单独下载和启动 Gateway；
- 用户需要在 Gateway 的登录页面完成登录和二次认证；
- API 会话需要 keep-alive；
- 还要处理会话失效、重新登录和本地 TLS 证书；
- 不能把 Gateway 的本地 HTTPS 端口暴露到公网。

### 6.3 为什么当前不优先 Web API

当前项目已有 TS 券商适配器和长连接模型，而 Web API 会增加：

- Gateway 进程管理；
- 浏览器登录和 2FA；
- OAuth/会话刷新；
- WebSocket 会话和 keep-alive；
- 本地 HTTPS 证书处理；
- 与现有 TS asyncio 任务的整合。

如果以后 IBKR 官方账户认证模式更适合 Web API，或者需要绕开 TWS/Gateway，才单独评估 Web API 适配，不建议在第一版同时实现 TWS API 和 Web API 两套链路。

## 7. 对本项目的适配边界

### 7.1 现有代码

涉及文件：

- `Trader_Server/api/factory.py`：已注册 `ib`、`interactive_brokers` 别名。
- `Trader_Server/api/interactive_brokers.py`：使用官方 `ibapi`，实现 TWS 行情连接和转发。
- `Trader_Server/api/base.py`：提供统一券商抽象，但还没有账户验证和账户选择的通用方法。
- `Trader_Server/requirements.txt`：固定 IB 官方 TWS API 10.49.2，并通过官方 URL 安装 Python 客户端。
- `Server_manager/main.py`：目前 IB 可以录入 `host/port/client_id`，但 SM 没有 IB API 实连验证。
- `Server_manager/templates/dashboard.html`：已有 IB Host、Port、Client ID 输入项，但审批验证流程仍主要针对 tastytrade。

### 7.2 第一阶段建议目标

第一阶段不要直接做全功能交易，建议按以下顺序：

1. TS 能检查 `ibapi` 是否安装。
2. TS 能连接 `127.0.0.1:7496` 或 `127.0.0.1:4001`。
3. TS 能通过 `managedAccounts` 返回账户列表。
4. TS 能验证管理员选择的 `account_id` 存在。
5. TS 能通过 `reqContractDetails` 验证 Symbol/Contract。
6. TS 能订阅 Bid/Ask/Last/Volume 并转发到 Client。
7. TS 能请求持仓并按账户隔离返回。
8. 最后再实现订单预览、下单、撤单和成交回报。

### 7.3 凭据模型建议

IB TWS API 第一版不应设计为 tastytrade 的 `secret + token` 模型。建议配置如下：

```json
{
  "broker_type": "interactive_brokers",
  "credentials": {
    "host": "127.0.0.1",
    "port": 4001,
    "client_id": 19,
    "account_id": "Uxxxxxxxx"
  }
}
```

字段含义：

- `host`：TWS/Gateway 地址，生产优先 `127.0.0.1`；
- `port`：TWS/Gateway API Socket 端口；
- `client_id`：API 客户端 ID，必须避免和其他客户端冲突；
- `account_id`：从 `managedAccounts` 或账户回调中选择并绑定的账户。

TWS/Gateway 的 IBKR 用户名、密码和验证码由 TWS/Gateway 会话管理，不保存到 SM、TS 配置或 Client。

### 7.4 验证执行位置

由于 IB API 多数场景连接本机 Gateway/TWS，SM 不能在 205 服务器上直接验证 TS 上的 `127.0.0.1:4001`。推荐后续使用：

```text
SM 审批弹窗
      │ 发起“验证 IB 配置”
      ▼
TS 在本机连接 IB Gateway/TWS
      │ 返回脱敏结果
      ▼
SM 展示账户列表和能力
      │
      ▼
管理员选择 account_id 后审批
```

已注册 TS 的配置修改也应由 TS 执行实际连接验证，验证成功后 SM 再保存配置。第一版可以先做格式检查，真实券商连通性验证作为 IB 接入阶段任务。

## 8. 限制和风险

### 8.1 行情权限

证券行情通常需要市场数据订阅。API 连接成功不等于实时行情可用。系统应分别显示：

- Gateway/TWS 已连接；
- 合约解析成功；
- 实时行情权限有效；
- 当前收到实时、延迟或冻结数据。

### 8.2 Pacing 和配额

IBKR 对实时行情线、历史数据、合约查询、扫描器和订单请求存在频率或数量限制。TS 需要：

- 统一请求队列；
- 请求 ID 管理；
- 频率限制和退避重试；
- 取消不再使用的行情和历史请求；
- 记录官方 error code 和 request type；
- 避免 Client 每次输入 Symbol 都创建重复订阅。

### 8.3 会话和重连

TWS/Gateway 可能因每日维护、重新认证、网络断开或人工退出而断开。TS 需要区分：

- TS 到 Gateway 的连接断开；
- Gateway 到 IBKR 的会话断开；
- 行情权限缺失；
- 账户无交易权限；
- API 客户端 ID 冲突；
- 单个请求失败。

这些状态不能统一显示为“交易服务登录失败”。

### 8.4 实盘安全

- 第一阶段先完成账户查询、持仓和行情。
- 下单功能默认关闭，直到订单字段映射、订单状态对账和异常恢复全部完成。
- 生产使用 Read-Only API 时，不应允许 Client 显示可提交订单的可用状态。
- 账户选择和券商配置变更需要重新验证。
- 日志不得记录 IBKR 用户名、密码、验证码或完整账户敏感信息。
- Gateway/TWS API 端口只允许 TS 本机或受控内网访问。

## 9. 后续适配任务拆分

| 阶段 | 目标 | 主要文件/范围 |
|---|---|---|
| IB-1 | 固定官方 `ibapi` 版本和安装方式 | `Trader_Server/requirements.txt` 已完成；打包脚本仍需单独验收 |
| IB-2 | TWS/Gateway 连接健康检查 | `Trader_Server/api/interactive_brokers.py` |
| IB-3 | 账户列表和账户选择 | `Trader_Server/api/base.py`、IB 适配器、SM 验证链路 |
| IB-4 | 合约解析和实时行情增强 | IB 适配器、TS 行情服务 |
| IB-5 | 持仓和账户摘要 | IB 适配器、TS WebSocket 消息、Client 展示 |
| IB-6 | 订单预览和订单状态 | IB 适配器、TS 订单服务、Client 订单模型 |
| IB-7 | 下单、撤单、重连对账 | 三端联调、实盘验收 |
| IB-8 | 多券商验证框架接入 | SM Broker Validation Registry、TS 本机验证回传 |

## 10. 官方资料索引

### 官方总览

- [IBKR API 官方入口（用户提供）](https://www.interactivebrokers.com.hk/en/trading/ib-api.php)
- [IBKR Campus API 总览（用户提供）](https://ibkrcampus.com/campus/ibkr-api-page/)
- [IBKR API Home](https://ibkrcampus.com/docs)
- [TWS API Documentation](https://ibkrcampus.com/docs/tws-api/doc/introduction)
- [TWS API Reference](https://ibkrcampus.com/docs/tws-api/ref/introduction)
- [Web API Documentation](https://ibkrcampus.com/docs/web-api/introduction)

### TWS API 连接和部署

- [TWS API Requirements](https://ibkrcampus.com/docs/tws-api/doc/notes-limitations/requirements)
- [Download TWS or IB Gateway](https://ibkrcampus.com/docs/tws-api/doc/download-tws-or-ib-gateway/download-tws-or-ib-gateway)
- [Install TWS API on Windows](https://ibkrcampus.com/docs/tws-api/doc/download-the-tws-api/install-the-tws-api-on-windows)
- [Establishing an API Connection](https://ibkrcampus.com/docs/tws-api/doc/connectivity/establishing-an-api-connection)
- [Verify API Connection](https://ibkrcampus.com/docs/tws-api/doc/connectivity/verify-api-connection)
- [Remote TWS API Connections](https://ibkrcampus.com/docs/tws-api/doc/connectivity/remote-tws-api-connections-with-trader-workstation)
- [Daily and Weekly Reauthentication](https://ibkrcampus.com/docs/tws-api/doc/tws-settings/daily-weekly-reauthentication)
- [Supported Two Factor Authentication](https://ibkrcampus.com/docs/tws-api/doc/notes-limitations/supported-two-factor-authentication-2-fa)

### TWS API 功能参考

- [Contract Object](https://ibkrcampus.com/docs/tws-api/doc/contracts-financial-instruments/the-contract-object)
- [Account Summary](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/account-summary)
- [Live Market Data](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/live-market-data)
- [Historical Market Data](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/historical-market-data)
- [Place Order](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/place-order)
- [Cancel Order](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/cancel-order)
- [Open Orders](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/open-orders)
- [Executions](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/executions)
- [Positions](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/positions)
- [Portfolio](https://ibkrcampus.com/docs/tws-api/doc/synchronous-api/portfolio)
- [Pacing Behavior](https://ibkrcampus.com/docs/tws-api/doc/pacing-limitations/pacing-behavior)
- [Live Data Limitations](https://ibkrcampus.com/docs/tws-api/doc/market-data-live/live-data-limitations)
- [TWS API Error Codes](https://ibkrcampus.com/docs/tws-api/doc/error-handling/error-codes)

### Web API

- [Web API Introduction](https://ibkrcampus.com/docs/web-api/introduction)
- [Web API Getting Started](https://ibkrcampus.com/docs/web-api/getting-started)
- [Web API Trading](https://ibkrcampus.com/docs/web-api/trading/getting-started/introduction)
- [Web API Account Management](https://ibkrcampus.com/docs/web-api/account-management/account-management-introduction/introduction)
- [Web API Reference](https://ibkrcampus.com/docs/web-api/api-reference/trading-accounts/get-account-owners)
- [Flex Web Service](https://ibkrcampus.com/docs/web-api/flex-web-service/flex-web-service/introduction)

## 11. 当前判断

后续正式接入 IBKR 时，建议以 TWS API/IB Gateway 为主线，先完成 TS 本机连接、账户发现、账户绑定、行情和持仓，再评估订单。Web API、Flex 和 FIX 作为独立能力保留在技术选型中，不混入第一版 TWS 适配。

本文件完成的是 API/SDK 技术说明，不代表以下事项已经完成：

- IBKR 真实账户连接测试；
- IB Gateway 自动化登录；
- IB 账户列表验证；
- IB 持仓和订单接入；
- IB 实盘下单验收；
- 多券商验证框架代码改造。
