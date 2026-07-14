# 项目当前架构理解

## 1. 文档定位

本文基于项目当前源码、配置、数据模型、程序入口和近期运行日志整理，用于描述当前系统的实际实现，并厘清其与未来建设目标之间的边界。

阅读依据如下：

- 当前源码、配置和运行行为是判断现状的主要依据；
- `CLOUD_GATEWAY_SECURITY_ARCHITECTURE.md` 是未来安全架构的预期建设文档，不是当前架构说明；
- 项目中其他 Markdown 文档存在时效性问题，仅可作为历史背景，不能直接代表当前实现。

## 2. 项目总体定位

本项目是一套证券交易系统，正在从“三端直连式架构”向“固定安全网关下的跨云动态交易节点架构”演进。

当前系统由以下部分组成：

- **Client**：用户使用的桌面交易终端；
- **Server Manager（SM）**：用户、节点、配置、状态和审计的中心控制面；
- **Trader Server（TS）**：连接真实券商并执行交易业务的节点；
- **Broker API**：Tastytrade、Interactive Brokers 等券商的统一适配层。

系统当前的主要业务目标是：

1. 管理用户账号和 Trader Server 节点；
2. 将用户关联或分配到指定交易节点；
3. 由 Client 查询行情、持仓、订单并发起交易；
4. 将券商凭据和真实券商连接集中在 Trader Server；
5. 由 Server Manager 管理节点注册、配置下发、状态监控和操作审计；
6. 后续将不同云厂商、区域和动态网络中的 Trader Server 收拢到统一安全网关之后。

## 3. 当前实际架构

当前源码实现的主要通信关系如下：

```text
Client
  ├─ HTTP → Server Manager :8800
  │    登录、节点探测、占用和释放节点
  │
  └─ WebSocket → Trader Server :8900/ws
       券商登录、行情、订单、持仓和交易操作

Trader Server
  ├─ HTTP/SSE → Server Manager :8800
  │    注册、审批等待、心跳、配置拉取和配置事件监听
  │
  └─ 券商 SDK/API
       Tastytrade / Interactive Brokers

Server Manager
  └─ SQLite
       用户、节点、运行状态、券商配置和审计日志
```

当前 Client 仍需要知道并直接访问 Trader Server 的地址和端口。Trader Server 运行 FastAPI，并向 Client 开放入站 WebSocket。

因此，当前实现尚未采用 `CLOUD_GATEWAY_SECURITY_ARCHITECTURE.md` 中描述的固定网关路由模式。

## 4. Client：桌面交易终端

Client 的正式入口是 `Client/main.py`，当前启动 PySide6 桌面界面。旧的 Tkinter 界面仍保留在 `Client/ui`，但正式入口使用 `Client/ui_qt`。

Client 当前的主要工作流程是：

1. 调用 Server Manager 的 `/auth/login` 登录；
2. 获取 Server Manager 生成的客户端 Token；
3. 获取账号关联的 Trader Server 地址；
4. 探测目标 Trader Server 并取得 `server_id`；
5. 请求 Server Manager 占用该节点；
6. 直接建立到 Trader Server 的 WebSocket；
7. 在首条 `CONNECT` 消息中携带客户端 Token 和 `server_id`；
8. 通过 WebSocket 完成行情、订单、持仓和交易操作；
9. 在退出、断开或连接失败时向 Server Manager 释放节点。

Client 当前支持：

- 用户登录、重复登录检测和强制接管；
- 券商登录、登出和状态查询；
- 行情订阅；
- 持仓查询；
- 活动订单和历史订单查询；
- 下单与撤单；
- WebSocket 心跳；
- 指数退避重连；
- 节点占用恢复；
- 管理端强制断开处理；
- 部分模拟数据回退。

主要实现文件包括：

- `Client/ui_qt/main_window.py`；
- `Client/services/trading_session.py`；
- `Client/network/ts_websocket.py`；
- `Client/network/http_client.py`。

## 5. Server Manager：中心控制面

`Server_manager/main.py` 是中心管理服务，默认监听 `0.0.0.0:8800`。

Server Manager 的定位是控制面，而不是主要交易执行面。其职责包括：

- 普通用户登录和客户端 Token 管理；
- Web 管理后台和管理员会话管理；
- Trader Server 注册申请、批准、拒绝和取消；
- 节点心跳与在线、离线状态检测；
- 节点暂停、恢复和删除；
- 节点占用、释放和强制释放；
- 将用户账号关联到 Trader Server 地址；
- 保存每个节点的券商类型和券商配置；
- 配置版本管理和热更新通知；
- 用户账号管理；
- 管理员操作审计；
- 将内存中的节点运行状态定期同步到数据库。

### 5.1 数据持久化

Server Manager 当前使用 SQLite，数据库主要实体包括：

- `accounts`：用户和管理员账号；
- `nodes`：节点稳定身份；
- `node_runtime`：节点实时状态；
- `node_broker_config`：节点券商配置；
- `node_registration_requests_v2`：新版节点注册审核流程；
- `brokers`、`node_requests`：旧版兼容表；
- `audit_log`：管理操作审计。

数据库实现位于 `Server_manager/database.py`，目前包含旧结构向新结构迁移和兼容逻辑。`Server_manager/node_state.py` 在内存中维护节点在线、心跳和占用状态，再定期同步到数据库。

整体模式可以概括为：

```text
SQLite 保存稳定状态
       +
内存管理实时节点状态
```

### 5.2 当前用户认证

当前用户 Token 不是 OAuth Token，而是由 Server Manager 生成并保存在进程内存中的随机 Token，默认有效期约一小时。

用户认证目前有三条兼容路径：

1. `users.json` 中的用户；
2. 环境变量或默认配置中的服务账号；
3. SQLite 数据库账号。

Trader Server 收到 Client 的 `CONNECT` 后，会使用自己的节点 Token 调用 Server Manager 的 `/auth/verify-token`。Server Manager 同时检查：

- Trader Server 的节点 Token 是否有效；
- Client Token 是否有效；
- 请求的 `server_id` 是否与当前节点一致；
- 当前节点是否由该用户占用。

当前访问控制的核心条件是：

```text
有效 Client Token
+ 有效节点 Token
+ server_id 匹配
+ occupied_by 等于当前用户
```

## 6. Trader Server：节点管理与交易执行

`Trader_Server/main.py` 当前同时承担以下两类职责：

1. 节点管理 Agent；
2. 面向 Client 的交易服务端。

Trader Server 默认监听 `8900`，当前包含：

- 本地 PySide6 管理界面；
- 测试 Server Manager 连通性；
- 提交节点注册申请；
- 等待管理员审批；
- 保存 Server Manager 分配的 `server_id` 和节点 Token；
- 周期性上报心跳；
- 监听配置变化事件；
- 拉取券商配置并热重载；
- 建立和维护券商连接；
- 向 Client 开放 `/ws`；
- 路由 Client 的交易消息。

节点凭据当前保存在 `Trader_Server/data/config.json` 中，使用的是普通节点 Token，而不是 mTLS 设备证书。

### 6.1 WebSocket 业务协议

Trader Server 当前支持的主要消息包括：

- `ECONOMIC_DATA_QUERY`；
- `STATUS_QUERY`；
- `SUMMARY_REPORT`；
- `ORDER_SUBMIT`；
- `ORDER_CANCEL`；
- `POSITION_QUERY`；
- `ORDER_QUERY`；
- `QUOTE_SUBSCRIBE`；
- `BROKER_LOGIN`；
- `BROKER_STATUS_QUERY`；
- `BROKER_LOGOUT`。

消息入口和路由主要位于 `Trader_Server/network/ws_server.py`。

从未来架构角度看，Trader Server 中的两类职责后续适合进一步分离：

```text
Gateway Agent
  └─ 主动连接网关、mTLS、心跳和消息路由

Trading Runtime
  └─ 券商连接、行情、订单、持仓和交易执行
```

但当前二者仍运行在同一个程序中。

## 7. 券商抽象层

项目已经建立统一的券商接口，基础抽象位于 `Trader_Server/api/base.py`。

统一能力包括：

- 连接、断开和重连；
- 凭据校验；
- 下单；
- 撤单；
- 持仓查询；
- 订单查询；
- 行情订阅和取消订阅；
- 连接错误标准化。

当前适配器包括：

- `Trader_Server/api/tastytrade.py`；
- `Trader_Server/api/interactive_brokers.py`；
- `Trader_Server/api/factory.py` 中的券商工厂。

Tastytrade 实现相对完整，包括订单、持仓、订单历史和行情流。Interactive Brokers 已具备基本接口，但依赖可选的 `ibapi`，部分能力仍不如 Tastytrade 完整。

项目的业务方向已经从单一券商硬编码逐渐转向可配置的统一券商适配层。

## 8. 节点注册和配置生命周期

当前 Trader Server 的主要生命周期如下：

```text
未注册
  → 测试 Server Manager 连通性
  → 提交注册申请
  → 等待管理员审批
  → Server Manager 分配 server_id 和节点 Token
  → Trader Server 本地保存凭据
  → 启动心跳
  → 拉取券商配置
  → 初始化券商连接
  → 对 Client 提供交易服务
```

Server Manager 可以修改节点的券商配置并增加配置版本。Trader Server 通过以下方式感知配置变化：

- 在心跳过程中检查配置版本；
- 通过配置事件监听更快地收到变化。

发现新版本后，Trader Server 拉取最新配置、执行券商热重载，并向已经连接的 Client 广播券商状态变化。

## 9. 节点独占模型

当前系统具有明显的节点独占语义，而不是一个节点同时为任意多个用户提供共享交易服务。

```text
用户登录
  → 获取目标 Trader Server
  → 在 Server Manager 中占用节点
  → 连接 Trader Server
  → 执行交易
  → 退出时释放节点
```

Trader Server 在验证 Client Token 时，还会通过 Server Manager 检查节点的 `occupied_by`。管理员可以强制释放节点，并要求 Trader Server 主动断开现有 Client。

因此，当前 `server_id → user` 的占用关系是业务授权的重要组成部分。

未来引入网关后，这一关系适合成为网关本地授权数据的一部分，避免每个 Trader Server 在 Client 建连时反查 Server Manager。

## 10. 未来目标架构

`CLOUD_GATEWAY_SECURITY_ARCHITECTURE.md` 描述的是下一阶段的预期建设目标：

```text
Client
  └─ HTTPS/WSS → 固定网关

Trader Server Agent
  └─ 主动 mTLS WSS/gRPC → 固定网关

OAuth 服务
  └─ 提供用户短效身份

Gateway
  ├─ 验证用户 OAuth 身份
  ├─ 验证 Trader Server 设备证书
  ├─ 维护 serverId → Agent 连接映射
  ├─ 对每条消息执行授权
  ├─ 防重放、幂等、限流和审计
  └─ 路由 Client 与 Trader Server 之间的消息
```

该建设目标不是简单地在当前系统前增加一个反向代理，而是重新定义连接方向、身份体系和安全边界：

- Client 不再获取或直接连接 Trader Server 公网地址；
- Trader Server 不再开放公网业务入站端口；
- Trader Server Agent 主动连接固定网关；
- 动态 IP 变化转化为网关内部连接映射变化；
- 用户身份升级为 OAuth 短效 Access Token；
- WSS 建连使用一次性短效票据；
- Trader Server 身份升级为独立 mTLS 设备证书；
- 每条消息由网关执行授权、限流、防重放和审计；
- 网关负责跨实例连接目录和消息路由。

## 11. 当前实现与目标架构的差距

| 方面 | 当前实现 | 目标建设 |
|---|---|---|
| Client 连接 | 直接连接 Trader Server | 只连接固定网关 |
| Trader Server 连接方向 | 对外开放 WebSocket | 主动连接网关 |
| 用户认证 | Server Manager 内存随机 Token | OAuth 短效 Token |
| WSS 建连 | 首包携带普通 Token | 一次性短效票据 |
| 节点身份 | JSON 文件中的节点 Token | 独立 mTLS 设备证书 |
| 传输安全 | 默认 HTTP/WS | HTTPS/WSS/mTLS |
| 节点定位 | Client 获取 Trader Server 地址 | 网关按 `serverId` 路由 |
| 授权位置 | Server Manager 建连验证和 TS 业务门禁 | 网关逐消息本地授权 |
| 防重放 | 有消息 ID 和 trace ID，但不完整 | requestId、时间窗和去重缓存 |
| 高可用 | 单控制面、节点直连 | 网关集群和共享连接目录 |
| 凭据存储 | JSON、SQLite、环境变量 | OAuth、KMS 和设备证书体系 |
| 审计 | 管理审计和普通运行日志 | 网关统一安全审计 |

## 12. 当前项目成熟度判断

项目已经形成一套可运行的三端业务骨架：

- GUI Client 已有正式入口；
- 节点注册和管理员审批流程已经实现；
- 节点心跳、离线判断、占用和释放已经实现；
- SQLite 数据库已经历多版本结构调整；
- 券商接口已经抽象；
- Client 与 Trader Server 的交易消息协议已经形成；
- 配置热更新和连接恢复机制已经存在；
- 近期日志表明用户登录、节点占用、TS 鉴权和 WebSocket 连接能够实际运行。

但项目仍处于功能原型向生产架构迁移的阶段，当前可见的工程问题包括：

- 尚未看到系统性的自动化测试体系；
- 新旧数据库结构和兼容代码并行存在；
- PySide6 和旧 Tkinter UI 同时保留；
- 部分源码注释存在编码损坏；
- 默认配置中存在测试登录后门；
- 节点 Token 保存在普通 JSON 文件中；
- 部分用户来源仍使用明文密码；
- 默认网络通信仍是 HTTP/WS；
- Server Manager 启动流程仍调用已不存在的 `_ensure_config_version_column`，近期日志中已出现对应警告；
- 近期 Tastytrade 连接曾因 JWT 无效而失败；
- 当前网络和身份安全边界尚不能直接满足公网跨云生产部署要求。

## 13. 总结

当前项目可以概括为：

> 一套以 Server Manager 为控制中心、以 Trader Server 为独占交易执行节点、由 Client 直连 Trader Server 的证券交易系统。

未来建设方向可以概括为：

> 保留现有账号、节点、券商适配和交易能力，将网络与身份层重构为 OAuth、固定网关、Agent 主动连接和 mTLS 设备身份组成的跨云安全架构。

后续进行设计和开发时，应始终明确区分：

- **当前源码已经实现的能力**；
- **正在演进中的过渡实现**；
- **`CLOUD_GATEWAY_SECURITY_ARCHITECTURE.md` 所描述的目标能力**。
