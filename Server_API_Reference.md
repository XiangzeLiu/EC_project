# Trading Proxy Server API 参考文档

> 基于 `origin_demo/server.py` 整理

## 概述

Trading Proxy Server 是一个阿里云中间代理服务器，提供：
- **HTTP REST API**：登录、下单、撤单、持仓查询、订单查询
- **WebSocket**：实时行情流中转

服务默认端口：`8800`

---

## 鉴权机制

### Token 获取
客户端通过 `/auth/login` 登录后获取 `token`，后续请求需在 Header 中携带：
```
Authorization: Bearer <token>
```

### Token 验证
服务器使用 `HTTPBearer` 验证，Token 为 `username:password` 的 SHA256 哈希值。

---

## API 端点详情

### 1. 健康检查

```
GET /health
```

**无需鉴权**

**响应示例：**
```json
{
    "status": "ok",
    "connected": true
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| status | string | 服务状态 |
| connected | boolean | 是否已连接券商 |

---

### 2. 客户端登录

```
POST /auth/login
```

**无需鉴权**

**请求体：**
```json
{
    "username": "admin",
    "password": "changeme123"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| username | string | 是 | 用户名 |
| password | string | 是 | 密码 |

**响应示例：**
```json
{
    "success": true,
    "token": "a1b2c3d4...",
    "acct_num": "5W12345"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| success | boolean | 登录是否成功 |
| token | string | 后续请求使用的鉴权 Token |
| acct_num | string | 账户号码 |

**错误码：**
- `401`: 用户名或密码错误
- `503`: 服务器未配置券商 Session Token

---

### 3. 登出

```
POST /auth/logout
```

**需要鉴权**

仅断开客户端连接，不影响服务器的券商连接。

**响应示例：**
```json
{
    "success": true
}
```

---

### 4. 获取持仓

```
GET /positions
```

**需要鉴权**

**响应示例：**
```json
{
    "positions": [
        {
            "symbol": "AAPL",
            "quantity": 100,
            "direction": "Long",
            "average_open_price": 150.50,
            "close_price": 155.00,
            "realized_today": 0.0
        }
    ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| symbol | string | 股票代码 |
| quantity | float | 持仓数量 |
| direction | string | 方向 (Long/Short) |
| average_open_price | float | 开仓均价 |
| close_price | float | 收盘价 |
| realized_today | float | 今日已实现盈亏 |

---

### 5. 获取活动订单

```
GET /orders/live
```

**需要鉴权**

**响应示例：**
```json
{
    "orders": [
        {
            "id": "123456",
            "symbol": "AAPL",
            "action": "Buy to Open",
            "qty": "100",
            "price": "150.00",
            "type": "LIMIT",
            "tif": "DAY",
            "status": "LIVE",
            "updated_at": "2024-01-15T10:30:00",
            "legs": []
        }
    ]
}
```

---

### 6. 获取订单历史

```
GET /orders/history
```

**需要鉴权**

**响应格式同 `/orders/live`**

---

### 7. 下单

```
POST /orders/place
```

**需要鉴权**

**请求体：**
```json
{
    "symbol": "AAPL",
    "qty": 100,
    "price": 150.0,
    "action": "Buy to Open",
    "order_type": "limit",
    "tif": "Day"
}
```

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| symbol | string | 是 | - | 股票代码 |
| qty | int | 是 | - | 数量 |
| price | float | 否 | 0.0 | 价格 (市价单可省略) |
| action | string | 是 | - | 订单动作 |
| order_type | string | 否 | "limit" | 订单类型 |
| tif | string | 否 | "Day" | 有效期 |

**action 可选值：**
- `Buy to Open` - 开多
- `Sell to Close` - 平多
- `Sell to Open` - 开空
- `Buy to Close` - 平空

**order_type 可选值：**
- `limit` - 限价单
- `market` - 市价单

**tif (Time In Force) 可选值：**
- `Day` - 当日有效
- `GTC` - 撤单前有效
- `IOC` - 立即成交或取消
- `EXT` - 盘前盘后
- `GTC_EXT` - 撤单前有效(含盘前盘后)

**响应示例：**
```json
{
    "success": true,
    "order_id": "123456"
}
```

---

### 8. 撤单

```
DELETE /orders/{order_id}
```

**需要鉴权**

**路径参数：**
| 参数 | 类型 | 说明 |
|------|------|------|
| order_id | string | 订单ID |

**响应示例：**
```json
{
    "success": true
}
```

---

### 9. 行情订阅 (WebSocket)

```
WebSocket /quotes?token=<token>
```

**需要鉴权** (通过 URL 参数传递 token)

**客户端消息格式：**

订阅：
```json
{
    "action": "subscribe",
    "symbols": ["AAPL", "MSFT"]
}
```

取消订阅：
```json
{
    "action": "unsubscribe",
    "symbols": ["AAPL"]
}
```

**服务器推送格式：**
```json
{
    "symbol": "AAPL",
    "bid": 150.10,
    "ask": 150.15,
    "last": 150.12,
    "volume": 12345,
    "ts": "10:30:45"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| symbol | string | 股票代码 |
| bid | float | 买一价 |
| ask | float | 卖一价 |
| last | float | 最新价 |
| volume | int | 成交量 |
| ts | string | 时间戳 (HH:MM:SS) |

---

## 错误响应

所有错误响应格式：
```json
{
    "detail": "错误描述信息"
}
```

**常见 HTTP 状态码：**
| 状态码 | 说明 |
|--------|------|
| 400 | 请求参数错误 |
| 401 | 未授权 (Token 无效或未登录) |
| 500 | 服务器内部错误 |

---

## 附录

### 订单序列化字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| id | string | 订单ID |
| symbol | string | 股票代码 |
| action | string | 订单动作 |
| qty | string | 数量 |
| price | string | 价格 (市价单显示 "MKT") |
| type | string | 订单类型 |
| tif | string | 有效期 |
| status | string | 订单状态 |
| updated_at | string | 更新时间 |
| legs | array | 订单腿信息 |

### 订单腿 (Legs) 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| symbol | string | 股票代码 |
| action | string | 动作 |
| quantity | string | 数量 |
| fills | array | 成交记录 |

### 成交记录 (Fills) 字段

| 字段 | 类型 | 说明 |
|------|------|------|
| fill_price | string | 成交价格 |
| quantity | string | 成交数量 |
| filled_at | string | 成交时间 |
