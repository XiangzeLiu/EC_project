# Server_manager 数据库重构实施文档

## 1. 目标

- 保持 `SQLite` 作为正式数据库，满足单机部署、文件复制迁移、快速恢复的要求。
- 将 `SM` 的正式数据统一收口到 `server_manager.db`。
- 拆分当前混杂的数据职责，提升后续多券商、多节点、多客户端扩展能力。
- 为后续迁移建立明确版本号和可重复执行的 migration 机制。

## 2. 当前确认问题

### 2.1 数据源不统一

- 正式库：`Server_manager/data/server_manager.db`
- 运行时仍混用：
  - 内存 `_admin_sessions`
  - 内存 `active_client_tokens`
  - 内存 `session_store`
  - `users.json`
  - `admin.json`

### 2.2 表职责混杂

- `accounts` 同时存在 `se_address` / `ts_address` 历史残留。
- `node_requests` 同时承担：
  - 注册申请历史
  - 已批准节点身份
  - 节点运行状态
  - 节点占用状态
- `brokers` 同时承担：
  - 节点 broker 配置
  - 节点在线状态
  - 配置版本号

### 2.3 迁移机制不正式

- 当前结构升级主要靠代码内零散 `ALTER TABLE`。
- `PRAGMA user_version = 0`，没有正式版本治理。

## 3. 目标结构

### 3.1 accounts

用途：系统账户。

关键字段：

- `id`
- `username`
- `password_hash`
- `role`
- `status`
- `trade_server_address`
- `description`
- `created_at`
- `updated_at`

### 3.2 node_registration_requests_v2

用途：保存注册申请历史。

关键字段：

- `id`
- `request_id`
- `node_name`
- `broker_type`
- `host`
- `capabilities_json`
- `contact`
- `description`
- `status`
- `reviewed_by`
- `reviewed_at`
- `reject_reason`
- `expire_at`
- `created_at`

### 3.3 nodes

用途：保存已批准节点的静态身份。

关键字段：

- `id`
- `server_id`
- `node_name`
- `broker_type`
- `host`
- `token`
- `description`
- `enabled`
- `created_at`
- `updated_at`

### 3.4 node_runtime

用途：保存节点运行态。

关键字段：

- `id`
- `server_id`
- `status`
- `current_ip`
- `last_heartbeat`
- `occupied_by`
- `occupied_at`
- `updated_at`

### 3.5 node_broker_config

用途：保存节点 broker 配置。

关键字段：

- `id`
- `server_id`
- `broker_type`
- `credentials_json`
- `enabled`
- `config_version`
- `updated_at`

### 3.6 audit_log

用途：保存审计日志。

## 4. 第一阶段 migration 设计

### 4.1 新增表

```sql
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL UNIQUE,
    node_name TEXT NOT NULL,
    broker_type TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '' UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS node_runtime (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'offline',
    current_ip TEXT NOT NULL DEFAULT '',
    last_heartbeat TEXT NOT NULL DEFAULT '',
    occupied_by TEXT NOT NULL DEFAULT '',
    occupied_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(server_id) REFERENCES nodes(server_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS node_broker_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL UNIQUE,
    broker_type TEXT NOT NULL DEFAULT '',
    credentials_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    config_version INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(server_id) REFERENCES nodes(server_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS node_registration_requests_v2 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    node_name TEXT NOT NULL,
    broker_type TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    contact TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    expire_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
```

### 4.2 accounts 新列

```sql
ALTER TABLE accounts ADD COLUMN trade_server_address TEXT NOT NULL DEFAULT '';
```

### 4.3 索引

```sql
CREATE INDEX IF NOT EXISTS idx_accounts_role_status
ON accounts(role, status);

CREATE INDEX IF NOT EXISTS idx_node_runtime_status
ON node_runtime(status);

CREATE INDEX IF NOT EXISTS idx_node_runtime_occupied_by
ON node_runtime(occupied_by);

CREATE INDEX IF NOT EXISTS idx_node_requests_v2_status_expire
ON node_registration_requests_v2(status, expire_at);

CREATE INDEX IF NOT EXISTS idx_audit_log_created_at
ON audit_log(created_at);

CREATE INDEX IF NOT EXISTS idx_audit_log_username_created_at
ON audit_log(username, created_at);
```

## 5. 数据迁移规则

### 5.1 accounts

- 来源：旧 `accounts`
- 目标：`trade_server_address`
- 规则：
  - `trade_server_address = COALESCE(NULLIF(se_address,''), NULLIF(ts_address,''), '')`

### 5.2 node_requests -> node_registration_requests_v2

- `region -> broker_type`
- `capabilities -> capabilities_json`
- 其余申请语义字段直接迁移

### 5.3 node_requests + brokers -> nodes

仅迁移状态属于以下集合的记录：

- `approved`
- `online`
- `offline`
- `suspended`

规则：

- `server_id` 非空才迁移
- `broker_type` 优先取 `brokers.broker_type`
- 如果 `brokers.broker_type` 为空，则回退取旧 `region`

### 5.4 node_requests + brokers -> node_runtime

规则：

- `status` 来自 `node_requests.status`
- `current_ip` 来自 `node_requests.current_ip`
- `last_heartbeat` 来自 `brokers.last_heartbeat`
- `occupied_by` / `occupied_at` 来自 `node_requests`

### 5.5 brokers -> node_broker_config

规则：

- `server_id = brokers.name`
- `broker_type = brokers.broker_type`
- `config_version = brokers.config_version`
- `credentials_json` 从旧 `config.credentials` 提取
- `enabled` 从旧 `config.enabled` 提取

## 6. user_version 规划

- `1`：旧结构
- `2`：新表落地，完成第一轮数据迁移
- `3`：代码进入双读双写阶段
- `4`：停止写旧结构
- `5`：删除旧结构依赖

## 7. database.py 重构清单

### 7.1 保留

- `_get_conn()`
- `_sha256()`
- `_audit_log()`
- `ensure_super_admin_account()`

### 7.2 重写

- `init_db()`
- `verify_account()`
- `verify_web_admin()`
- `get_all_accounts()`
- `create_account()`
- `update_account()`
- `get_all_nodes()`
- `get_approved_nodes_for_memory_load()`
- `verify_node_token()`
- `update_node_heartbeat()`
- `sync_node_states_to_db()`
- `occupy_node()`
- `release_node()`
- `get_occupation_info()`
- `get_node_broker_config()`
- `set_node_broker_config()`

### 7.3 新增建议函数

- `run_migrations()`
- `migrate_v1_to_v2()`
- `resolve_trade_server_address()`
- `get_node_by_server_id()`
- `get_node_by_token()`
- `list_nodes_with_runtime()`
- `get_node_runtime()`
- `update_node_runtime_heartbeat()`
- `occupy_node_runtime()`
- `release_node_runtime()`
- `create_registration_request_v2()`
- `approve_registration_request_v2()`
- `reject_registration_request_v2()`
- `cancel_registration_request_v2()`
- `cleanup_expired_registration_requests()`
- `get_node_broker_config_v2()`
- `set_node_broker_config_v2()`

## 8. 一次性迁移脚本设计

建议新增：

- `Server_manager/migrations/migrate_v1_to_v2.py`

执行顺序：

1. 读取 `PRAGMA user_version`
2. 备份原库
3. 执行 `WAL checkpoint`
4. 开事务
5. 创建新表与索引
6. 给 `accounts` 增加 `trade_server_address`
7. 迁移 `accounts`
8. 迁移注册申请历史
9. 迁移已批准节点到 `nodes`
10. 迁移运行态到 `node_runtime`
11. 迁移 broker 配置到 `node_broker_config`
12. 写入 `PRAGMA user_version = 2`
13. 提交事务
14. 输出迁移报告

迁移脚本要求：

- 可重复执行
- 插入时使用 `INSERT OR IGNORE` 或 `UPSERT`
- 失败时整体回滚

## 9. 受影响接口清单

### 9.1 账户相关

- `POST /auth/login`
- `GET /api/accounts/list`
- `POST /api/accounts/create`
- `GET /api/accounts/{account_id}/detail`
- `POST /api/accounts/{account_id}/update`
- `GET /api/accounts/se-status`

### 9.2 注册申请相关

- `POST /nodes/register-request`
- `GET /nodes/await-approval`
- `POST /api/nodes/{request_id}/approve`
- `POST /api/nodes/{request_id}/reject`
- `POST /nodes/cancel-request`
- `GET /api/nodes/pending`

### 9.3 节点身份/运行态相关

- `POST /nodes/heartbeat`
- `GET /api/nodes/list`
- `POST /api/nodes/refresh-status`
- `POST /api/nodes/{server_id}/delete`
- `POST /api/nodes/{server_id}/suspend`
- `POST /api/nodes/{server_id}/resume`
- `POST /api/nodes/{server_id}/occupy`
- `POST /api/nodes/{server_id}/release`
- `POST /api/nodes/{server_id}/force-release`
- `POST /auth/verify-token`

### 9.4 broker 配置相关

- `GET /nodes/config-events`
- `GET /api/nodes/config`
- `PUT /api/nodes/{server_id}/config`
- `POST /api/nodes/{server_id}/reload`

## 10. 建议实施顺序

1. 新增 migration 机制和 `user_version`
2. 新增目标表与索引
3. 编写一次性迁移脚本
4. 重构 `database.py`
5. 改 `auth_router.py`
6. 改 `main.py`
7. 进入双读双写验证
8. 停止写旧结构
9. 移除 `users.json` / `admin.json` 运行时依赖
10. 最后清理旧字段和旧逻辑

## 11. 验收标准

- `SM` 的正式持久化只认 `server_manager.db`
- `accounts` 只有一个正式服务器地址字段
- 注册申请、节点身份、节点运行态、节点配置分离
- 迁移脚本可重复执行
- 数据库可复制到另一设备进行部署
- 结构升级可通过 `user_version` 明确判断
