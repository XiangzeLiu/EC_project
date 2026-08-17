# EC_project AI 项目接手指南

> 适用对象：首次接手本项目的 AI、开发人员和维护人员
> 基线日期：2026-08-17
> 目标：在不依赖历史对话的情况下，快速理解系统边界、代码结构、交易规则、生产约束和当前待办。

## 0. 使用方式与事实优先级

本文件是“接手入口”，不是唯一规格。发生冲突时，按以下顺序判断：

1. 当前代码和自动化测试。
2. `生产落地.md` 中最新的 U1-U11 状态与用户明确确认的边界。
3. `docs/product/` 下的生产维护文档。
4. 券商官方 API 文档和当前锁定的 SDK 行为。
5. 历史文档与旧备忘录，仅供追溯，不得直接作为当前实现依据。

以下文件存在历史价值，但内容可能落后于当前代码：

- `PROJECT_CURRENT_ARCHITECTURE_UNDERSTANDING.md`
- `PROJECT_TODO_MEMO.md`
- `证券股票交易系统产品文档_合并版.md`
- `origin_demo/`

新 AI 开始工作前必须先执行：

```powershell
Set-Location C:\Users\Administrator\PycharmProjects\EC_project
git status --short --branch
rg -n "^- \[ \]" 生产落地.md
python -m pytest -q
```

注意：工作区可能已有用户改动。不得回滚、覆盖或格式化与当前任务无关的文件。

## 1. 系统一句话说明

这是一个面向美股交易的三端系统：

- **Client**：交易员使用的 Windows 桌面客户端。
- **Server Manager，简称 SM**：账号、节点、域名、配置和审批中心。
- **Trader Server，简称 TS**：部署在券商接入侧的交易执行节点，负责行情、订单、持仓和券商 API 适配。

Client 不直接连接券商，也不保存券商凭据。SM 决定交易员能连接哪台 TS，TS 才直接连接券商 API。

## 2. 系统边界与核心目标

### 2.1 当前支持的业务

- 交易员账号登录与 24 小时认证。
- 一个交易员账号固定绑定一台 TS。
- TS 注册、审批、心跳、占用、释放和强制释放。
- HTTPS/WSS 生产链路和 Caddy 自动管理。
- 美股股票代码确认与 BID/ASK 行情订阅。
- 左右两个独立交易栏和活动交易栏切换。
- 限价单、市场单及多种 TIF。
- 买入、卖出、持仓、订单查询和撤单。
- 进行中、成交、失效、ALL 四类订单视图。
- 股数快捷键、下单快捷键和固定快捷键。
- TT 与 Interactive Brokers 两套券商适配器。
- SM 管理后台、审计、产品文档和 PDF 下载。

### 2.2 明确不做或暂缓的事项

- 不让 Client 直接登录券商。
- 不把券商账号密码保存到 Client。
- 不在 TS 中保存 IB Gateway 用户名、密码或自动完成 2FA。
- 第一版不做 IB Gateway 自动登录维护。
- 第一版不做业务程序或 Caddy 的 Windows Service。
- 第一版不做业务 EXE 原地升级，只做完整重新安装。
- 第一版不做数据库备份迁移工具。
- 第一版不做已注册 TS 公网 IP 变化后的自动 DNS 修正。
- 自动化测试不得发送真实券商订单。

## 3. 三端架构

```text
交易员
  |
  | HTTPS: 登录、状态查询、占用/释放
  v
Client -----------------------> Server Manager
  |                                |
  |                                | SQLite
  |                                | 账号、节点、配置、域名池、审计
  |                                |
  | WSS: CONNECT、行情、订单        | HTTPS: 注册、审批、心跳、配置同步
  v                                v
Trader Server <-----------------------------------+
  |
  | 券商 SDK / 本机 Socket
  v
TT API 或 IB Gateway/TWS
```

生产公网只应暴露 Caddy 的 `80/443`。SM 的 `8800`、TS 的 `8900`、Caddy admin 的 `2019/2020` 和 IB 的 `4001` 都是内部端口。

## 4. 仓库结构

```text
EC_project/
├─ Client/                  # PySide6 交易员客户端
│  ├─ main.py              # Client 正式入口
│  ├─ network/             # SM HTTP 与 TS WebSocket
│  ├─ services/            # 登录、业务请求、错误脱敏
│  ├─ ui_qt/               # 主界面、设置、协调器和主题
│  ├─ assets/              # 图标等资源
│  └─ tools/               # Client 辅助工具
├─ Server_manager/         # SM FastAPI 管理端
│  ├─ main.py              # SM 入口和主要管理 API
│  ├─ database.py          # SQLite schema、迁移和数据访问
│  ├─ auth.py              # Client token
│  ├─ node_state.py        # 节点在线、心跳和占用状态
│  ├─ routers/             # 认证等路由
│  ├─ services/            # Caddy、凭据验证等服务
│  ├─ templates/           # 管理后台和产品文档页面
│  ├─ caddy/               # SM 自带 Caddy 运行目录
│  └─ data/                # SM 数据库和日志，属于运行态数据
├─ Trader_Server/          # TS FastAPI/WSS 与桌面控制面板
│  ├─ main.py              # TS 入口
│  ├─ network/ws_server.py # Client WSS 网关
│  ├─ services/            # 配置同步、交易、行情、注册、心跳
│  ├─ api/                 # 券商适配器
│  ├─ ui_qt/               # TS 本地管理界面
│  ├─ caddy/               # TS 自带 Caddy 运行目录
│  └─ data/                # TS 注册配置和日志，属于运行态数据
├─ tests/                   # 三端自动化回归测试
├─ docs/product/            # 面向维护者的生产产品文档
├─ deploy/windows/          # Windows 生产启动模板
├─ build_client_installer.bat
├─ 生产落地.md              # 当前生产状态和统一待办
└─ AI项目接手指南.md        # 本文件
```

## 5. 关键入口和持久化位置

| 对象 | 入口/位置 | 说明 |
|---|---|---|
| Client | `python -m Client.main` | 正式 PySide6 入口 |
| Client 配置 | `%APPDATA%/SC Client/hotkey.json` | 本地优先，损坏或校验失败时回退代码默认值 |
| SM | `python -m Server_manager.main` | 默认监听 `127.0.0.1:8800` |
| SM 数据库 | `Server_manager/data/server_manager.db` | SQLite，账号、节点、券商配置、域名池和审计 |
| SM 日志 | `Server_manager/data/logs/` | `sm.log`、`sm_error.log` 等 |
| TS | `python -m Trader_Server.main` | 默认监听 `127.0.0.1:8900`，同时打开本地 GUI |
| TS 正式配置 | `Trader_Server/data/config.json` | 注册成功后的 server_id、token、SM 地址等 |
| TS 注册状态 | `Trader_Server/data/.register_state.json` | 注册审批过程的临时恢复状态 |
| TS 日志 | `Trader_Server/data/logs/` | 日期日志和 `ts_error.log` |

开发依赖分别位于：

- `Client/requirements.txt`
- `Server_manager/requirements.txt`
- `Trader_Server/requirements.txt`
- `requirements-build.txt`，仅打包使用

## 6. 端到端主流程

### 6.1 Client 登录和连接

```text
1. Client -> SM /auth/login
2. SM 校验数据库交易员账号
3. SM 返回 client token、过期时间、账号绑定的 TS 地址
4. Client -> SM 查询 TS 状态
5. Client -> SM 占用绑定 TS
6. Client -> TS WSS，首包发送 CONNECT
7. TS -> SM /auth/verify-token 二次校验 token、账号、节点和占用会话
8. TS 返回 CONNECT_ACK
9. Client 进入主界面并查询交易服务能力
```

关键约束：

- Client token 默认有效期为 `86400` 秒，即 24 小时。
- token 与管理员 Web Cookie 都是内存态，相关服务重启后会话失效。
- “SM 已显示节点被占用”只说明占用步骤成功，不代表 WSS 已连接成功。
- TS 的 CONNECT 校验会把 `connection_id` 绑定到占用会话，防止账号和节点串用。
- 同一节点的新连接会接管旧连接，旧连接应被清理。

### 6.2 TS 注册和配置同步

```text
TS 提交注册申请
  -> SM 分配注册请求和验证身份
  -> 管理员在 SM 审批
  -> SM 验证券商配置/账户
  -> SM 分配 TS 域名并更新 DNSPod
  -> TS 保存 config.json
  -> TS 启动或重载 Caddy
  -> TS 周期心跳并同步 config_version
  -> 配置变化时 TS 热重载券商适配器
```

SM 是券商配置的控制面。TS 通过 node token 拉取自己的配置，不应让 Client 获取原始凭据。

### 6.3 股票确认和行情

```text
交易员输入 SYMBOL
  -> 点击搜索按钮或按 Enter
  -> Client 发送 QUOTE_SUBSCRIBE
  -> TS 解析合约/订阅行情
  -> TS 返回 QUOTE_ACK
  -> 后续通过 QUOTE_DATA 推送 BID/ASK
```

当前行为：

- 不再使用输入后 350ms 自动查询。
- 输入本身不代表股票已确认。
- 后续订单必须围绕已确认的当前 symbol 执行。
- 两个交易栏可分别持有不同 symbol 和订阅意图。
- 连接代次变化后，旧请求和旧行情不能更新新连接界面。

### 6.4 下单

```text
Client 收集 symbol/qty/price/action/order_type/tif/route/hidden
  -> Client 做能力、symbol、价格和快捷键状态检查
  -> ORDER_SUBMIT
  -> TS trading_svc 做统一参数校验
  -> broker adapter 转为具体 SDK 订单
  -> 券商接受或拒绝
  -> ORDER_RESPONSE
  -> 后续 ORDER_STATUS_UPDATE 触发订单/持仓刷新
```

不要在 Client、SM 和 TS 三处重复实现不同版本的业务规则。通用规则放在 TS 服务层，券商差异放在 adapter，Client 只做体验和前置保护。

### 6.5 撤单和刷新

- 双击订单行或点击撤单按钮，撤销当前选中且可撤销的订单。
- `Esc` 不是全局撤单，只撤销当前活动交易栏 symbol 的全部活动订单，同时清理待确认状态。
- `Filled`、`Cancelled`、`Rejected`、`Expired` 等终态不得继续撤销。
- 撤单和手动刷新有冷却与 in-flight 保护。
- 下单的业务级重复单、突发和 in-flight 限制当前有意关闭，只保留键盘输入保护。

### 6.6 断线和释放

- Client 正常退出时注销 token，并请求释放节点。
- WSS 异常断开后，TS 会清理连接并向 SM 重试释放占用。
- Client 有短线自动重连，默认指数退避，最多 10 次。
- TS 券商连接也有独立自动重连和配置热重载，不要把两类重连混为一谈。

## 7. Client 与 TS 的 WSS 协议

统一消息包含：

```json
{
  "type": "ORDER_SUBMIT",
  "id": "request-id",
  "timestamp": 0,
  "payload": {
    "trace_id": "trace-id"
  }
}
```

Client 主动消息：

| 类型 | 用途 |
|---|---|
| `CONNECT` | 首包鉴权并绑定节点会话 |
| `BROKER_STATUS_QUERY` | 查询脱敏后的交易能力和状态 |
| `QUOTE_SUBSCRIBE` | 确认 symbol、订阅/调整行情 |
| `POSITION_QUERY` | 查询持仓和当日活动 |
| `ORDER_QUERY` | 查询订单页签数据 |
| `ORDER_SUBMIT` | 下单 |
| `ORDER_CANCEL` | 撤单 |
| `PING` | 应用层心跳和延迟测量 |

TS 响应或广播：

| 类型 | 用途 |
|---|---|
| `CONNECT_ACK` | 连接完成，附带能力状态 |
| `QUOTE_ACK` | symbol/订阅确认结果 |
| `QUOTE_DATA` | 行情推送 |
| `POSITION_RESPONSE` | 持仓响应 |
| `ORDER_LIST_RESPONSE` | 订单列表 |
| `ORDER_RESPONSE` | 下单结果 |
| `ORDER_CANCEL_RESPONSE` | 撤单结果 |
| `BROKER_STATUS_CHANGE` | 交易服务连接或重载状态变化 |
| `ORDER_STATUS_UPDATE` | 订单状态事件 |
| `POSITION_INVALIDATED` | 持仓缓存失效提示 |
| `PONG` | 心跳响应 |
| `FORCE_DISCONNECT` | 接管或管理员强制断开 |

订单统一字段：

```text
symbol, qty, price, action, order_type, tif, route, hidden
```

增加协议字段时必须同时检查 Client 请求、TS 路由、`trading_svc`、两个券商 adapter、事件回传和自动化测试。

## 8. Client 当前行为

### 8.1 主界面

- 左右两个交易栏，黄色高亮表示当前活动栏。
- 持仓双击后把 symbol 加载到当前活动栏，而不是固定左栏。
- 快捷下单也只作用于当前活动栏。
- SYMBOL 通过 Enter 或搜索图标确认。
- 报价只显示 BID 和 ASK，主交易栏不显示 LAST。
- QTY 只保留输入，已移除 `+10/-10/+1/-1` 按钮。
- ROUTE 和 HIDE 根据 TS 返回的 capability 与 symbol 级 order options 处理。
- 设置页是主窗口内部遮罩，不是独立系统窗口。
- 弱提示位于 Console 上缘，半透明、自动渐隐、不抢焦点。

### 8.2 订单页签

| 页签 | 归一化状态 |
|---|---|
| 进行中 | `Received`、`Routing`、`Live`、`Partial`、`Cancelling` |
| 成交 | `Filled` |
| 失效 | `Cancelled`、`Rejected`、`Expired` |
| ALL | 当前美股交易日内 adapter 可提供的全部范围 |

订单状态事件会触发快速刷新；有活动订单时兜底轮询约 5 秒，无活动订单时约 30 秒。持仓兜底轮询约 15 秒。

### 8.3 快捷键

固定快捷键：

| 按键 | 功能 |
|---|---|
| `Space` | 切换左右交易栏 |
| `Esc` | 清除当前栏待确认订单，并撤销当前 symbol 的全部活动订单 |
| `Enter` | SYMBOL 输入时查询；存在待确认订单时提交 |
| `Up/Down` | PRICE `+0.05/-0.05` |
| `Left/Right` | PRICE `-0.01/+0.01` |

股数快捷键：

- 默认 `Num+1` 到 `Num+9` 对应 100 到 900 股。
- 默认按键固定，只能修改股数和启用状态。
- 可新增自定义规则，总上限 20 条。
- 主键盘数字不等于小键盘数字。

下单快捷键：

- 上限 15 条。
- 默认 `Shift+F1` 到 `Shift+F12` 已预置但默认禁用。
- 可设置方向、订单类型、TIF、ROUTE、价格偏移、HIDE 和启用状态。
- 保存前必须做空按键、格式和冲突检测。
- 普通非 IOC 下单快捷键为两步提交：先准备订单，再按 Enter。
- Limit+IOC 快捷键满足条件时立即提交。

## 9. SM 职责与数据模型

SM 是生产控制面，主要职责：

- `accounts`：交易员、管理员和超级管理员。
- `nodes`：TS 静态信息。
- `node_runtime`：在线、心跳和占用状态。
- `node_broker_config`：每台 TS 的券商配置和 `config_version`。
- `node_registration_requests_v2`：TS 注册审批。
- `ts_domain_pool`：TS 子域名池和冷却状态。
- `dns_provider_config`：DNSPod 配置。
- `audit_log`：管理操作审计。

账号角色：

- `super_admin`：最高权限。
- `admin`：管理交易员等受限管理能力。
- `trader`：Client 登录账号，必须绑定 TS 地址。

新数据库没有超级管理员时，会用 bootstrap 值创建 `admin/admin123`；已有超级管理员不会被启动过程重置。生产首次登录后必须修改默认密码。

当前密码存储仍是现有 SHA-256 兼容实现，密码强度和更强密码哈希属于后续安全改造，不能误写成已完成。

## 10. TS 职责与内部模块

关键文件：

| 文件 | 职责 |
|---|---|
| `Trader_Server/network/ws_server.py` | WSS 鉴权、消息路由、连接接管和释放 |
| `Trader_Server/services/config_sync.py` | 券商创建、配置热重载、状态广播和重连 |
| `Trader_Server/services/trading_svc.py` | 订单参数校验、下单、撤单和查询 |
| `Trader_Server/services/quote_provider.py` | 会话订阅与聚合行情订阅 |
| `Trader_Server/api/base.py` | 券商统一接口和 capability 契约 |
| `Trader_Server/api/factory.py` | adapter 工厂 |
| `Trader_Server/api/tastytrade.py` | TT adapter |
| `Trader_Server/api/interactive_brokers.py` | IB adapter |
| `Trader_Server/services/registration.py` | TS 注册和审批恢复 |
| `Trader_Server/services/heartbeat.py` | SM 心跳和配置版本同步 |
| `Trader_Server/services/caddy_manager.py` | TS Caddy 生成、启动和重载 |

接入新券商时必须实现 `BaseBrokerAPI` 的连接、下单、撤单、持仓、行情方法，并明确：

- `capabilities()`
- `effective_capabilities()`
- `supported_tifs()`
- `get_symbol_order_options()`
- 错误是否可重试
- 订单状态映射
- 订单查询范围
- 凭据归属和审批流程

## 11. 交易领域知识

### 11.1 BID、ASK 和价差

- **BID**：市场当前最高买价。你卖出时，主动成交通常会接近 BID。
- **ASK**：市场当前最低卖价。你买入时，主动成交通常会接近 ASK。
- **Spread**：`ASK - BID`，即买卖价差。

当前快捷键取价规则：

- 普通非 IOC 限价买入：默认 BID。
- 普通非 IOC 限价卖出：默认 BID。
- Limit+IOC 买入：ASK，强调立即成交倾向。
- Limit+IOC 卖出：BID。
- Market+IOC：不依赖 Client 本地报价，但当前 TT 不开放 IOC。

普通卖出也默认 BID 是当前产品确认的行为，不要凭经验自行改成 ASK。

### 11.2 订单方向

| 动作 | 含义 |
|---|---|
| `Buy to Open` | 买入建立多头，或建立需要买入的开仓方向 |
| `Buy to Close` | 买入平掉空头 |
| `Sell to Open` | 卖出建立空头 |
| `Sell to Close` | 卖出平掉多头 |

股票场景最常见的是 `Buy to Open` 和 `Sell to Close`。IB adapter 通过 `orderRef=EC:*` 保留四种业务动作，避免只靠 BUY/SELL 丢失开平语义。

### 11.3 订单类型

| 类型 | 含义 | 风险 |
|---|---|---|
| Limit | 只按指定价格或更优价格成交 | 可能长时间不成交 |
| Market | 按市场可用价格尽快成交 | 波动或流动性不足时可能严重滑点 |

### 11.4 TIF

| TIF | Client 业务含义 |
|---|---|
| `Day` | 当前美股交易日有效，未成交部分通常日终失效 |
| `GTC` | 撤销前持续有效，具体最长时间由券商决定 |
| `IOC` | 立即成交可成交部分，剩余立刻取消 |
| `EXT` | 扩展时段当日单，通常应配合限价单 |
| `GTC_EXT` | GTC 并允许扩展时段，最终规则以券商为准 |

IOC 不是“券商替 Client 自动找一个价格”。Limit+IOC 仍必须携带限价，限价决定可立即成交的价格边界。

### 11.5 HIDE 与 ROUTE

- **ROUTE**：订单送往的交易路径或交易所。SMART 表示由券商智能路由。
- **HIDE**：隐藏订单展示数量/意图的参数，不代表订单不存在，也不保证完全不可见。
- 不支持 HIDE 的通道可以允许保存配置，但实际提交必须按普通订单处理。
- 不支持可编辑 ROUTE 的通道必须固定为 SMART，不能把无效 route 发给券商。

### 11.6 订单生命周期

```text
Received -> Routing -> Live -> Partial -> Filled
                          |        |
                          v        v
                     Cancelling -> Cancelled

任意提交阶段也可能进入 Rejected 或 Expired
```

“限价买入待确认”只是 Client 本地待确认状态，尚未提交到 TS/券商，不属于进行中订单，也不应出现在订单列表中。

### 11.7 美股交易日和时区

- 交易日判断使用 `America/New_York`，不是北京时间零点。
- 当前 Client 将美东 `04:00-20:00` 作为当日活动窗口。
- 一个美股交易日会跨越中国自然日，过北京时间零点不能自动换日。
- 夏令时会改变北京时间对应的开盘时间，逻辑应基于带时区时间。

## 12. 券商差异

### 12.1 能力矩阵

| 能力 | TT | Interactive Brokers |
|---|---|---|
| 行情 | 支持 | 支持，但需要正确的 API 实时行情订阅 |
| 下单/撤单 | 支持 | 支持，前提是已选择受 Gateway 管理的账户 |
| 持仓/订单 | 支持 | 支持 |
| ROUTE | 固定 SMART，禁止修改 | 默认 SMART，可按 symbol 返回的路由选择 |
| HIDE | 不支持，配置可保存但执行忽略 | 支持，映射 `Order.hidden` |
| TIF | Day/GTC/EXT/GTC_EXT | Day/GTC/IOC/EXT/GTC_EXT |
| IOC | Client 当前置灰 | 支持，但依赖有效实时行情和订单条件 |
| 登录维护 | OAuth 凭据由 SM/TS 管理 | Gateway 人工登录和 2FA |

### 12.2 TT 关键规则

- adapter 固定 SMART，不传 Client 自定义路由。
- HIDE 实际执行时必须关闭。
- 当前官方/实测通道路由对 IOC 返回拒绝，因此 Client 通过 `supported_tifs` 禁用 IOC。
- `no compatible order router found` 仍保留为待复测诊断项；复测需要记录 symbol、price、order type、TIF、触发方式和精确时间。
- TT read-only 账户关闭下单和撤单 capability，但仍可保留查询能力。

### 12.3 IB 关键规则

- TS 与 IB Gateway/TWS 必须同机。
- 当前固定 `127.0.0.1:4001`、`client_id=1`。
- 第一版只支持美股 `STK/SMART/USD`。
- 连接成功不能只看 TCP；必须收到 `nextValidId` 和 `managedAccounts`。
- 下单前通过 `contractDetails` 解析合约，歧义时拒绝。
- 未选择 `account_id` 或账户不在 `managedAccounts` 时，订单、撤单、持仓和订单查询 capability 关闭。
- 订单列表只展示当前选择账户中、由本 TS 创建并带 `orderRef=EC:*` 的订单。
- IB Gateway 登录、重新认证和 2FA 目前是人工流程。
- 不得把 IB 用户名密码存入 TS 来规避人工登录。

已确认的 IB 行情错误：

| 错误码 | 当前项目中的含义 |
|---|---|
| `2186` | API 实时行情需要额外订阅；网页/桌面可见行情不等于 API streaming 权限 |
| `10168` | 当前请求无法使用延迟行情或未启用延迟行情 |

对于美股实时 Level 1，维护时重点核对 NYSE Network A/CTA、NYSE American/ARCA 等 Network B/CTA、NASDAQ Network C/UTP。不要为了让界面“有数字”而静默回退延迟行情并用于实盘下单。

IB 运行期状态码 `1100/1101/1102/1300` 的代码恢复逻辑已完成；真实 Gateway 断线、重登和行情/订单恢复仍需按第 17 节验收。

## 13. 安全和产品不变量

以下规则优先级高于局部 UI 或快捷开发需求：

1. Client 对交易员保持券商无感，不直接显示券商名称、凭据、账户号、TS IP/域名或后台技术细节。
2. Client 错误必须经过脱敏，移除 URL、IP、域名、本地路径、内部 id、账户标识和 traceback。
3. 不支持的功能通过 capability 置灰、隐藏或归一化，不能通过提示暴露具体提供商。
4. 券商凭据只存在于 SM/TS 管理边界，不进入 Client。
5. 一个交易员账号固定绑定一台 TS；不能让 Client 自由选择任意节点。
6. 节点占用发生在 WSS 之前，WSS CONNECT 仍必须再次向 SM 验证。
7. 生产公网只开放 `80/443`；内部服务绑定 loopback。
8. 日志可以为管理员保留技术细节，但交易员可见 Console 和弱提示必须脱敏。
9. 自动化测试不得真实下单、撤销真实订单或修改生产 DNS。
10. 真实下单测试必须由用户明确确认并使用最小风险参数。

## 14. 并发、性能和一致性设计

### 14.1 Client

- Qt 主线程只更新 UI，网络和查询走后台线程。
- `QuoteSubscriptionCoordinator` 串行协调两个交易栏的订阅意图，使用 epoch、generation 和 serial 丢弃过期结果。
- `OrderRefreshCoordinator` 对订单和持仓分别做单 in-flight 合并，避免重复请求堆积。
- 订单事件 300ms 合并刷新，提交/撤单后立即刷新并在 800ms 后再做一致性检查。
- `Filled` 后额外刷新持仓。
- WebSocket 请求使用 id 关联响应，行情推送不走同步等待路径。

### 14.2 TS

- WSS 收到普通业务消息后创建独立任务处理，PING/PONG 不应被慢查询阻塞。
- 每个 WebSocket 有发送锁，避免并发 JSON 写入交错。
- `config_sync` 使用锁保护 broker 热重载。
- 券商重连计划为 30、60、120、300、600 秒；不可重试的认证错误会暂停自动重试。
- 行情订阅由 TS 聚合，adapter 重连后需要恢复订阅。

### 14.3 不能随意改变的保护

- Limit+IOC 行情新鲜度为 5 秒，并要求行情属于当前 TS 连接 generation。
- Enter 有 300ms 输入保护。
- 撤单冷却 500ms，批量撤单和手动刷新冷却 1000ms。
- 业务级重复订单限制当前为关闭状态，不要误以为缺失并擅自恢复。

## 15. 开发与测试

### 15.1 安装依赖

```powershell
Set-Location C:\Users\Administrator\PycharmProjects\EC_project
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r Client\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r Server_manager\requirements.txt
.\.venv\Scripts\python.exe -m pip install -r Trader_Server\requirements.txt
```

### 15.2 本地启动

SM：

```powershell
.\.venv\Scripts\python.exe -m Server_manager.main
```

TS：

```powershell
.\.venv\Scripts\python.exe -m Trader_Server.main
```

Client：

```powershell
.\.venv\Scripts\python.exe -m Client.main
```

本地 HTTP/WS 联调必须显式覆盖环境变量；打包 Client 默认只接受生产域名链路。

### 15.3 回归测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

本指南生成时的完整回归基线为 `296 passed, 4 warnings`。4 条 warning 均为 SM 现有 FastAPI `on_event` 生命周期 API 弃用提示，不是本次文档任务引入的失败。

按改动范围补充定向测试：

- Client UI/快捷键：`tests/test_client_*`
- WebSocket 与占用：搜索 `ws`、`occupation`、`reconnect`
- SM 数据库与域名池：搜索 `database`、`domain_pool`、`dnspod`
- TT：搜索 `tastytrade`
- IB：搜索 `ib`、`interactive_brokers`
- 打包：运行 `build_client_installer.bat` 和 Client `--package-self-test`

完成修改至少执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
git diff --check
git status --short
```

涉及 UI 时还应启动实际界面检查 1080p、2K、4K 布局、焦点框、悬浮/按压态和主窗口缩放。

## 16. 常见故障定位

### 16.1 SM 显示占用，但 Client 一直连接失败

依次判断：

1. SM 占用成功不代表 WSS 成功。
2. 检查 TS 域名是否解析到当前服务器。
3. 检查 Caddy `/ws` 是否反代到 `127.0.0.1:8900`。
4. 检查 TS 运行环境是否安装 `websockets`。
5. 检查 TS 日志中的 CONNECT、token verify 和 server_id。
6. 必要时在 SM 强制释放，再重新登录。

### 16.2 TS 注册时报 TLS `CERTIFICATE_VERIFY_FAILED`

- 先在同一个虚拟环境中用 Python `urllib.request.urlopen()` 验证。
- `curl.exe` 使用 Windows Schannel，成功不能证明 Python OpenSSL 证书链成功。
- 当前 HTTPS 客户端已支持 `certifi` 证书路径；确认运行的是更新后的代码和虚拟环境。
- 禁止用全局关闭证书校验作为生产修复。

### 16.3 IB Gateway socket 不可用

```powershell
Test-NetConnection 127.0.0.1 -Port 4001
```

失败通常表示 Gateway 未启动、未登录、API 未开启、端口不是 4001 或只允许特定来源。TS 与 Gateway 同机时不需要开放公网 4001。

### 16.4 IB 能连通但无法获取行情

区分三层：

1. Socket/API 会话是否正常。
2. 合约是否解析成功。
3. 账户是否有 API 实时行情订阅。

如果 `contractDetails` 成功，但返回 `2186` 或 `10168`，优先判断行情订阅，不要误判为 TS 与 IB 完全断开。行情失败不等于一定不能下单，但没有可靠行情时不应自动放行依赖本地价格的实盘策略。

### 16.5 TT 返回 `no compatible order router found`

目前不能只归因于网络或登录。复现时记录：

- symbol
- price
- order type
- TIF
- 点击或快捷键来源
- BID/ASK 和行情年龄
- Client、TS、券商返回的精确时间

当前 TT IOC 已禁用；普通订单若复现，继续对照相同时间的 Client 与 TS `ORDER_DIAG` 日志。

### 16.6 Client 延迟偶发升高

Client 顶部延迟是应用层 PING/PONG 往返时间，包含：

- Client 事件循环和线程调度
- 公网链路
- Caddy/WSS
- TS 事件循环调度

单次 100ms 或 600ms 不等于券商 API 慢。此前专项采集未证明需要重写短线重连，因此当前不做高风险重连改造。

## 17. 当前状态和待办

### 17.1 已完成的主体代码

- 三端 HTTPS/WSS、固定绑定、占用和单 Client 隔离。
- DNSPod 域名池、TS 审批分配和 Caddy 自动管理主体。
- TT 行情、持仓、订单、下单、撤单和实盘链路。
- IB adapter、注册验证、账户选择、行情/订单/持仓 API 代码和自动化测试。
- Client 双交易栏、显式 symbol 查询、四订单页签、快捷键、能力门控、弱提示和错误脱敏。
- Client 24 小时认证和过期回登录页流程。
- 产品维护文档、SM 内嵌页签和 PDF 下载。
- Client onedir、便携 ZIP、自检和 SHA-256 验证。

### 17.2 仍需代码处理

1. **SM/TS 业务 EXE 打包脚本**：目前正式生产包仍未闭环。
2. **TT `no compatible order router found` 诊断复测**：日志已具备基础字段，仍需可交易时段人工复现并最终清理临时诊断需求。

### 17.3 代码已完成但仍需真实环境验收

- IB Gateway 未启动、API 未开启、已登录三种状态。
- IB `1100/1101/1102/1300` 在真实 Gateway 下的降级、原地恢复、行情重订阅、订单校准和完整重连。
- IB 实时行情订阅、持仓、当天订单、ROUTE、HIDE 和撤单。
- IB 真实下单必须由用户单独确认。
- owner/trade-only 与 read-only 权限路径。
- Client 正常退出、真实异常断线、自动重连和 SM 强制释放演练。
- 删除 TS 后域名进入冷却及 Client 自然失败。
- `SM_DOMAIN_COOLDOWN_SECONDS=1800` 的正式上线验收。
- 真实 DNSPod A 记录创建/修改验收。
- Client Setup 在干净 Windows 10/11 x64 设备安装验收。
- SM/TS EXE 和完整重新安装后的 HTTPS/WSS、Caddy、端口验收。

### 17.4 打包前必须检查

- 排除 `Trader_Server/data/config.json`。
- 排除 `Server_manager/data/server_manager.db`。
- 不包含 broker token、secret、密码、DNSPod 凭据、日志、缓存或本机测试配置。
- TS 打包不得把整个 `Trader_Server/data` 目录加入产物。
- Client 本地 `.tt_config.json` 不得进入生产包。

待办状态的最终权威清单仍以 `生产落地.md` 的 U1-U11 为准。

## 18. 新 AI 修改代码时的检查表

### 18.1 修改前

- 阅读目标模块及相邻协调器/服务。
- 搜索同一字段在 Client、SM、TS 和 tests 中的全部使用点。
- 确认工作区已有改动，不覆盖用户文件。
- 判断任务是通用业务规则、券商差异、UI 体验还是生产配置。
- 涉及订单时先说明是否可能发送真实订单。

### 18.2 修改中

- 通用订单校验放 TS `trading_svc`，券商映射放 adapter。
- Client 只消费 capability，不直接按券商名称分叉 UI。
- 所有交易员可见错误使用安全中文，不返回异常对象或后端字符串。
- 异步结果必须校验 connection generation/epoch，避免旧连接污染新界面。
- 连接、订阅、订单事件和刷新都要考虑重复触发与并发。
- 新增配置必须有默认值、读取、校验、损坏回退和原子保存。

### 18.3 修改后

- 添加覆盖正常、失败、重复、断线和重连路径的测试。
- 跑定向测试，再跑完整 `pytest`。
- 执行 `git diff --check`。
- 检查 Client 不暴露券商名、IP、域名、账户号、路径和 traceback。
- 检查 TT 回归和 IB 回归，不能只验证当前目标券商。
- UI 改动需要实际启动检查布局、焦点、键盘和鼠标行为。
- 更新 `生产落地.md` 时只同步真实完成状态，不把“代码完成”写成“生产验收完成”。

## 19. 优先阅读文件

新 AI 建议按以下顺序阅读：

1. `AI项目接手指南.md`
2. `生产落地.md` 的“当前状态与未完成待办项”
3. `Client/ui_qt/main_window.py`
4. `Client/ui_qt/ts_connection_coordinator.py`
5. `Client/services/trading_session.py`
6. `Trader_Server/network/ws_server.py`
7. `Trader_Server/services/config_sync.py`
8. `Trader_Server/services/trading_svc.py`
9. `Trader_Server/api/base.py`
10. 当前任务涉及的券商 adapter
11. `Server_manager/routers/auth_router.py`
12. `Server_manager/node_state.py`
13. `Server_manager/database.py`
14. `docs/product/` 对应章节
15. `tests/` 中与目标模块同名或同关键词的测试

接手时最重要的判断不是“某个函数怎么写”，而是先确认这项行为属于 Client 交互、SM 控制面、TS 通用交易层还是券商 adapter。放错层会造成能力泄露、券商逻辑互相污染，或出现界面看似可用但实际提交参数无效的问题。
