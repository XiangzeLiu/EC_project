"""
SQLite Database Operations
轻量化账户/券商管理数据持久化
"""

import json
import os
import sqlite3
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("server_manager")

_DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "data",
    "server_manager.db",
)
_DB_PATH = os.environ.get("SERVER_MANAGER_DB_PATH", _DEFAULT_DB_PATH)

DB_SCHEMA_VERSION_V1 = 1
DB_SCHEMA_VERSION_V2 = 2
DB_SCHEMA_VERSION_V3 = 3
DB_SCHEMA_VERSION_V4 = 4


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动创建目录和表"""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256((text or "").encode()).hexdigest()


LEGACY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT DEFAULT 'trader',
    status TEXT DEFAULT 'active',
    allowed_brokers TEXT DEFAULT '[]',
    ts_address TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    updated_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS brokers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    broker_type TEXT DEFAULT 'tastytrade',
    host TEXT DEFAULT '',
    port INTEGER DEFAULT 0,
    config TEXT DEFAULT '{}',
    status TEXT DEFAULT 'offline',
    last_heartbeat TEXT DEFAULT '',
    registered_at TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT DEFAULT '',
    action TEXT DEFAULT '',
    resource TEXT DEFAULT '',
    detail TEXT DEFAULT '',
    ip_address TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS node_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT UNIQUE NOT NULL,
    node_name TEXT NOT NULL,
    region TEXT DEFAULT '',
    host TEXT DEFAULT '',
    capabilities TEXT DEFAULT '[]',
    contact TEXT DEFAULT '',
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    server_id TEXT DEFAULT '',
    token TEXT DEFAULT '',
    current_ip TEXT DEFAULT '',
    expire_at TEXT DEFAULT '',
    reviewed_by TEXT DEFAULT '',
    reviewed_at TEXT DEFAULT '',
    reject_reason TEXT DEFAULT '',
    created_at TEXT DEFAULT ''
);
"""

V2_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    server_id TEXT NOT NULL UNIQUE,
    node_name TEXT NOT NULL,
    broker_type TEXT NOT NULL DEFAULT '',
    host TEXT NOT NULL DEFAULT '',
    public_ip TEXT NOT NULL DEFAULT '',
    assigned_domain TEXT NOT NULL DEFAULT '',
    public_endpoint TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '' UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
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
    public_ip TEXT NOT NULL DEFAULT '',
    source_ip TEXT NOT NULL DEFAULT '',
    assigned_domain TEXT NOT NULL DEFAULT '',
    public_endpoint TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    contact TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    server_id TEXT NOT NULL DEFAULT '',
    token TEXT NOT NULL DEFAULT '',
    reviewed_by TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT '',
    reject_reason TEXT NOT NULL DEFAULT '',
    expire_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
"""

V4_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ts_domain_pool (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fqdn TEXT NOT NULL UNIQUE,
    root_domain TEXT NOT NULL DEFAULT '',
    record_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'available',
    assigned_server_id TEXT NOT NULL DEFAULT '',
    assigned_node_name TEXT NOT NULL DEFAULT '',
    assigned_ip TEXT NOT NULL DEFAULT '',
    public_endpoint TEXT NOT NULL DEFAULT '',
    dns_record_id TEXT NOT NULL DEFAULT '',
    dns_status TEXT NOT NULL DEFAULT 'pending',
    dns_error TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ts_domain_pool_status
ON ts_domain_pool(status, cooldown_until, id);

CREATE INDEX IF NOT EXISTS idx_ts_domain_pool_server
ON ts_domain_pool(assigned_server_id);
"""

V2_INDEXES_SQL = """
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
"""


def get_db_path() -> str:
    return _DB_PATH


def _get_user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version = {int(version)}")


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(r[1] == column_name for r in rows)


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
    if not _column_exists(conn, table_name, column_name):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")
        log.info(f"Added {column_name} column to {table_name} table")


def _ensure_legacy_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(LEGACY_SCHEMA_SQL)
    _ensure_column(conn, "accounts", "ts_address", "ts_address TEXT DEFAULT ''")
    _ensure_column(conn, "accounts", "description", "description TEXT DEFAULT ''")
    _ensure_column(conn, "accounts", "trade_server_address", "trade_server_address TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "accounts", "broker_tag", "broker_tag TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "node_requests", "occupied_by", "occupied_by TEXT DEFAULT ''")
    _ensure_column(conn, "node_requests", "occupied_at", "occupied_at TEXT DEFAULT ''")
    _ensure_column(conn, "brokers", "config_version", "config_version INTEGER DEFAULT 0")


def _create_v2_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(V2_SCHEMA_SQL)
    conn.executescript(V2_INDEXES_SQL)


def _ensure_v3_schema(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "accounts", "broker_tag", "broker_tag TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "nodes", "capabilities_json", "capabilities_json TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "node_registration_requests_v2", "server_id", "server_id TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "node_registration_requests_v2", "token", "token TEXT NOT NULL DEFAULT ''")


def _ensure_v4_schema(conn: sqlite3.Connection) -> None:
    _ensure_column(conn, "nodes", "public_ip", "public_ip TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "nodes", "assigned_domain", "assigned_domain TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "nodes", "public_endpoint", "public_endpoint TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "node_registration_requests_v2", "public_ip", "public_ip TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "node_registration_requests_v2", "source_ip", "source_ip TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "node_registration_requests_v2", "assigned_domain", "assigned_domain TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "node_registration_requests_v2", "public_endpoint", "public_endpoint TEXT NOT NULL DEFAULT ''")
    conn.executescript(V4_SCHEMA_SQL)


def _has_v2_schema(conn: sqlite3.Connection) -> bool:
    required_tables = (
        "nodes",
        "node_runtime",
        "node_broker_config",
        "node_registration_requests_v2",
    )
    return all(_table_exists(conn, name) for name in required_tables) and _column_exists(
        conn,
        "accounts",
        "trade_server_address",
    )


def _needs_v1_to_v2_backfill(conn: sqlite3.Connection) -> bool:
    source_expr = _get_account_address_source_expr(conn)
    account_row = conn.execute(
        f"""
        SELECT 1
        FROM accounts
        WHERE COALESCE(trade_server_address, '') = ''
          AND {source_expr} <> ''
        LIMIT 1
        """
    ).fetchone()
    if account_row:
        return True

    request_row = conn.execute(
        "SELECT 1 FROM node_requests WHERE request_id <> '' LIMIT 1"
    ).fetchone()
    if request_row:
        return True

    broker_row = conn.execute(
        "SELECT 1 FROM brokers WHERE name <> '' LIMIT 1"
    ).fetchone()
    return bool(broker_row)


def _get_account_address_source_expr(conn: sqlite3.Connection) -> str:
    if _column_exists(conn, "accounts", "se_address"):
        return "COALESCE(NULLIF(se_address, ''), NULLIF(ts_address, ''), '')"
    return "COALESCE(NULLIF(ts_address, ''), '')"


def resolve_trade_server_address(data: dict | sqlite3.Row | None) -> str:
    if not data:
        return ""
    if not isinstance(data, dict):
        data = dict(data)
    return (
        (data.get("trade_server_address") or "").strip()
        or (data.get("se_address") or "").strip()
        or (data.get("ts_address") or "").strip()
    )


def _load_json_dict(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_json_list(raw: str | None) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _build_legacy_broker_config_payload(
    broker_type: str,
    credentials: dict | None = None,
    enabled: bool = True,
    base_config: dict | None = None,
) -> dict:
    payload = dict(base_config or {})
    payload["broker_type"] = broker_type or payload.get("broker_type", "") or ""
    payload["credentials"] = dict(credentials or {})
    payload["enabled"] = bool(enabled)
    return payload


def _build_broker_config_response(
    broker_type: str,
    credentials: dict | None = None,
    enabled: bool = True,
    config_version: int = 0,
    raw_config: dict | None = None,
) -> dict:
    raw = _build_legacy_broker_config_payload(
        broker_type=broker_type,
        credentials=credentials,
        enabled=enabled,
        base_config=raw_config,
    )
    return {
        "broker_type": broker_type or raw.get("broker_type", "TT"),
        "credentials": dict(credentials or {}),
        "enabled": bool(enabled),
        "config_version": int(config_version or 0),
        "_raw_config": raw,
    }


def _normalize_broker_tag(broker_tag: str | None = None, allowed_brokers_raw: str | None = None) -> str:
    tag = (broker_tag or "").strip()
    if tag:
        return tag
    if allowed_brokers_raw:
        items = _load_json_list(allowed_brokers_raw)
        if items:
            return str(items[0] or "").strip()
    return ""


def _build_allowed_brokers_json(broker_tag: str) -> str:
    tag = (broker_tag or "").strip()
    return json.dumps([tag] if tag else [], ensure_ascii=False)


def _map_request_v2_to_legacy(data: dict | sqlite3.Row | None) -> dict | None:
    if not data:
        return None
    if not isinstance(data, dict):
        data = dict(data)
    return {
        "id": data.get("id", 0),
        "request_id": data.get("request_id", "") or "",
        "node_name": data.get("node_name", "") or "",
        "region": data.get("broker_type", "") or "",
        "host": data.get("host", "") or "",
        "public_ip": data.get("public_ip", "") or "",
        "source_ip": data.get("source_ip", "") or "",
        "assigned_domain": data.get("assigned_domain", "") or "",
        "public_endpoint": data.get("public_endpoint", "") or "",
        "capabilities": data.get("capabilities_json", "[]") or "[]",
        "contact": data.get("contact", "") or "",
        "description": data.get("description", "") or "",
        "status": data.get("status", "pending") or "pending",
        "server_id": data.get("server_id", "") or "",
        "token": data.get("token", "") or "",
        "current_ip": data.get("current_ip", "") or "",
        "expire_at": data.get("expire_at", "") or "",
        "reviewed_by": data.get("reviewed_by", "") or "",
        "reviewed_at": data.get("reviewed_at", "") or "",
        "reject_reason": data.get("reject_reason", "") or "",
        "created_at": data.get("created_at", "") or "",
    }


def _build_account_compat(data: dict | sqlite3.Row | None) -> dict | None:
    if not data:
        return None
    if not isinstance(data, dict):
        data = dict(data)
    data["se_address"] = resolve_trade_server_address(data)
    broker_tag = _normalize_broker_tag(data.get("broker_tag", ""), data.get("allowed_brokers", ""))
    data["broker_tag"] = broker_tag
    data["allowed_brokers"] = _build_allowed_brokers_json(broker_tag)
    data["ts_address"] = data["se_address"]
    return data


def _ensure_v2_node_record(conn: sqlite3.Connection, server_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM nodes WHERE server_id = ?", (server_id,)).fetchone()
    if row:
        return dict(row)

    legacy_row = conn.execute(
        """
        SELECT nr.server_id, nr.node_name,
               COALESCE(NULLIF(b.broker_type, ''), NULLIF(nr.region, ''), '') AS broker_type,
               COALESCE(NULLIF(nr.host, ''), NULLIF(b.host, ''), '') AS host,
               COALESCE(nr.token, '') AS token,
               COALESCE(nr.description, '') AS description,
               COALESCE(nr.capabilities, '[]') AS capabilities_json,
               COALESCE(nr.created_at, '') AS created_at,
               COALESCE(NULLIF(nr.reviewed_at, ''), NULLIF(b.last_heartbeat, ''), nr.created_at, '') AS updated_at
        FROM node_requests nr
        LEFT JOIN brokers b ON b.name = nr.server_id
        WHERE nr.server_id = ?
        LIMIT 1
        """,
        (server_id,),
    ).fetchone()
    if not legacy_row:
        legacy_row = conn.execute(
            """
            SELECT b.name AS server_id, b.name AS node_name,
                   COALESCE(b.broker_type, '') AS broker_type,
                   COALESCE(b.host, '') AS host,
                   '' AS token,
                   '' AS description,
                   '[]' AS capabilities_json,
                   COALESCE(b.created_at, '') AS created_at,
                   COALESCE(NULLIF(b.last_heartbeat, ''), b.created_at, '') AS updated_at
            FROM brokers b
            WHERE b.name = ?
            LIMIT 1
            """,
            (server_id,),
        ).fetchone()
    if not legacy_row:
        return None

    data = dict(legacy_row)
    conn.execute(
        """
        INSERT INTO nodes (
            server_id, node_name, broker_type, host, token,
            description, capabilities_json, enabled, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(server_id) DO UPDATE SET
            node_name=excluded.node_name,
            broker_type=excluded.broker_type,
            host=excluded.host,
            token=CASE WHEN excluded.token <> '' THEN excluded.token ELSE nodes.token END,
            description=excluded.description,
            capabilities_json=excluded.capabilities_json,
            updated_at=excluded.updated_at
        """,
        (
            data.get("server_id", "") or server_id,
            data.get("node_name", "") or server_id,
            data.get("broker_type", "") or "",
            data.get("host", "") or "",
            data.get("token", "") or "",
            data.get("description", "") or "",
            data.get("capabilities_json", "[]") or "[]",
            data.get("created_at", "") or "",
            data.get("updated_at", "") or data.get("created_at", "") or "",
        ),
    )
    row = conn.execute("SELECT * FROM nodes WHERE server_id = ?", (server_id,)).fetchone()
    return dict(row) if row else None


def _upsert_node_runtime(
    conn: sqlite3.Connection,
    server_id: str,
    status: str,
    current_ip: str = "",
    last_heartbeat: str = "",
    occupied_by: str = "",
    occupied_at: str = "",
    updated_at: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO node_runtime (
            server_id, status, current_ip, last_heartbeat,
            occupied_by, occupied_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(server_id) DO UPDATE SET
            status=excluded.status,
            current_ip=excluded.current_ip,
            last_heartbeat=excluded.last_heartbeat,
            occupied_by=excluded.occupied_by,
            occupied_at=excluded.occupied_at,
            updated_at=excluded.updated_at
        """,
        (
            server_id,
            status or "offline",
            current_ip or "",
            last_heartbeat or "",
            occupied_by or "",
            occupied_at or "",
            updated_at or last_heartbeat or "",
        ),
    )


def _upsert_node_broker_config(
    conn: sqlite3.Connection,
    server_id: str,
    broker_type: str,
    credentials: dict | None = None,
    enabled: bool = True,
    config_version: int = 0,
    updated_at: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO node_broker_config (
            server_id, broker_type, credentials_json, enabled,
            config_version, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(server_id) DO UPDATE SET
            broker_type=excluded.broker_type,
            credentials_json=excluded.credentials_json,
            enabled=excluded.enabled,
            config_version=excluded.config_version,
            updated_at=excluded.updated_at
        """,
        (
            server_id,
            broker_type or "",
            json.dumps(dict(credentials or {}), ensure_ascii=False),
            1 if enabled else 0,
            int(config_version or 0),
            updated_at or "",
        ),
    )


def migrate_v1_to_v2(conn: sqlite3.Connection) -> dict:
    report = {
        "accounts_backfilled": 0,
        "registration_requests_copied": 0,
        "nodes_copied": 0,
        "runtime_copied": 0,
        "broker_configs_copied": 0,
    }

    _create_v2_schema(conn)
    source_expr = _get_account_address_source_expr(conn)

    report["accounts_backfilled"] = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM accounts
        WHERE COALESCE(trade_server_address, '') = ''
          AND {source_expr} <> ''
        """
    ).fetchone()[0]
    conn.execute(
        f"""
        UPDATE accounts
        SET trade_server_address = {source_expr}
        WHERE COALESCE(trade_server_address, '') = ''
        """
    )

    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO node_registration_requests_v2 (
            request_id, node_name, broker_type, host, capabilities_json,
            contact, description, status, server_id, token, reviewed_by, reviewed_at,
            reject_reason, expire_at, created_at
        )
        SELECT
            request_id,
            node_name,
            COALESCE(region, ''),
            COALESCE(host, ''),
            COALESCE(capabilities, '[]'),
            COALESCE(contact, ''),
            COALESCE(description, ''),
            COALESCE(status, 'pending'),
            COALESCE(server_id, ''),
            COALESCE(token, ''),
            COALESCE(reviewed_by, ''),
            COALESCE(reviewed_at, ''),
            COALESCE(reject_reason, ''),
            COALESCE(expire_at, ''),
            COALESCE(created_at, '')
        FROM node_requests
        """
    )
    report["registration_requests_copied"] = conn.total_changes - before

    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO nodes (
            server_id, node_name, broker_type, host, token,
            description, capabilities_json, enabled, created_at, updated_at
        )
        SELECT
            nr.server_id,
            nr.node_name,
            COALESCE(NULLIF(b.broker_type, ''), COALESCE(nr.region, '')),
            COALESCE(nr.host, ''),
            COALESCE(nr.token, ''),
            COALESCE(nr.description, ''),
            COALESCE(nr.capabilities, '[]'),
            1,
            COALESCE(nr.created_at, ''),
            COALESCE(nr.reviewed_at, nr.created_at, '')
        FROM node_requests nr
        LEFT JOIN brokers b ON b.name = nr.server_id
        WHERE nr.server_id <> ''
          AND nr.status IN ('approved', 'online', 'offline', 'suspended')
        """
    )
    report["nodes_copied"] = conn.total_changes - before

    before = conn.total_changes
    conn.execute(
        """
        INSERT OR IGNORE INTO node_runtime (
            server_id, status, current_ip, last_heartbeat,
            occupied_by, occupied_at, updated_at
        )
        SELECT
            nr.server_id,
            COALESCE(nr.status, 'offline'),
            COALESCE(nr.current_ip, ''),
            COALESCE(b.last_heartbeat, ''),
            COALESCE(nr.occupied_by, ''),
            COALESCE(nr.occupied_at, ''),
            COALESCE(b.last_heartbeat, nr.reviewed_at, nr.created_at, '')
        FROM node_requests nr
        LEFT JOIN brokers b ON b.name = nr.server_id
        WHERE nr.server_id <> ''
          AND nr.status IN ('approved', 'online', 'offline', 'suspended')
        """
    )
    report["runtime_copied"] = conn.total_changes - before

    before = conn.total_changes
    node_ids = {
        row[0]
        for row in conn.execute("SELECT server_id FROM nodes").fetchall()
        if row[0]
    }
    broker_rows = conn.execute(
        "SELECT name, broker_type, config, config_version, last_heartbeat FROM brokers"
    ).fetchall()
    for broker_row in broker_rows:
        server_id = broker_row[0]
        if not server_id or server_id not in node_ids:
            continue
        raw_cfg = broker_row[2] or "{}"
        try:
            cfg = json.loads(raw_cfg)
        except Exception:
            cfg = {}
        credentials = dict(cfg.get("credentials", {}) or {})
        enabled = 1 if cfg.get("enabled", True) else 0
        broker_type = broker_row[1] or cfg.get("broker_type", "") or ""
        updated_at = broker_row[4] or ""
        conn.execute(
            """
            INSERT INTO node_broker_config (
                server_id, broker_type, credentials_json, enabled,
                config_version, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(server_id) DO UPDATE SET
                broker_type=excluded.broker_type,
                credentials_json=excluded.credentials_json,
                enabled=excluded.enabled,
                config_version=excluded.config_version,
                updated_at=excluded.updated_at
            """,
            (
                server_id,
                broker_type,
                json.dumps(credentials, ensure_ascii=False),
                enabled,
                broker_row[3] or 0,
                updated_at,
            ),
        )
    report["broker_configs_copied"] = conn.total_changes - before

    _set_user_version(conn, DB_SCHEMA_VERSION_V2)
    return report


def migrate_v2_to_v3(conn: sqlite3.Connection) -> dict:
    report = {
        "accounts_broker_tag_backfilled": 0,
        "request_rows_extended": 0,
        "nodes_capabilities_backfilled": 0,
    }

    _ensure_v3_schema(conn)

    account_rows = conn.execute(
        "SELECT id, broker_tag, allowed_brokers FROM accounts"
    ).fetchall()
    for row in account_rows:
        broker_tag = _normalize_broker_tag(row["broker_tag"], row["allowed_brokers"])
        if broker_tag and (row["broker_tag"] or "") != broker_tag:
            conn.execute(
                "UPDATE accounts SET broker_tag = ? WHERE id = ?",
                (broker_tag, row["id"]),
            )
            report["accounts_broker_tag_backfilled"] += 1

    req_rows = conn.execute(
        "SELECT request_id, server_id, token FROM node_requests WHERE request_id <> ''"
    ).fetchall()
    for row in req_rows:
        cursor = conn.execute(
            """
            UPDATE node_registration_requests_v2
            SET server_id = CASE WHEN server_id = '' THEN ? ELSE server_id END,
                token = CASE WHEN token = '' THEN ? ELSE token END
            WHERE request_id = ?
              AND ((server_id = '' AND ? <> '') OR (token = '' AND ? <> ''))
            """,
            (row["server_id"] or "", row["token"] or "", row["request_id"], row["server_id"] or "", row["token"] or ""),
        )
        report["request_rows_extended"] += cursor.rowcount

    node_rows = conn.execute(
        "SELECT server_id, capabilities FROM node_requests WHERE server_id <> ''"
    ).fetchall()
    for row in node_rows:
        cursor = conn.execute(
            """
            UPDATE nodes
            SET capabilities_json = CASE WHEN capabilities_json = '' OR capabilities_json = '[]' THEN ? ELSE capabilities_json END
            WHERE server_id = ?
              AND (capabilities_json = '' OR capabilities_json = '[]')
            """,
            (row["capabilities"] or "[]", row["server_id"]),
        )
        report["nodes_capabilities_backfilled"] += cursor.rowcount

    _set_user_version(conn, DB_SCHEMA_VERSION_V3)
    return report


def migrate_v3_to_v4(conn: sqlite3.Connection) -> dict:
    _ensure_v4_schema(conn)
    _set_user_version(conn, DB_SCHEMA_VERSION_V4)
    return {
        "domain_pool_created": 1,
        "node_domain_columns_added": 1,
        "request_network_columns_added": 1,
    }


def run_migrations(conn: sqlite3.Connection) -> list[dict]:
    reports: list[dict] = []
    _ensure_legacy_schema(conn)
    _create_v2_schema(conn)
    _ensure_v3_schema(conn)
    _ensure_v4_schema(conn)
    version = _get_user_version(conn)
    if version < DB_SCHEMA_VERSION_V2 and _has_v2_schema(conn) and not _needs_v1_to_v2_backfill(conn):
        _set_user_version(conn, DB_SCHEMA_VERSION_V2)
        version = DB_SCHEMA_VERSION_V2
    if version < DB_SCHEMA_VERSION_V2:
        report = migrate_v1_to_v2(conn)
        report["from_version"] = version
        report["to_version"] = DB_SCHEMA_VERSION_V2
        reports.append(report)
        version = DB_SCHEMA_VERSION_V2
    if version < DB_SCHEMA_VERSION_V3:
        report = migrate_v2_to_v3(conn)
        report["from_version"] = version
        report["to_version"] = DB_SCHEMA_VERSION_V3
        reports.append(report)
        version = DB_SCHEMA_VERSION_V3
    if version < DB_SCHEMA_VERSION_V4:
        report = migrate_v3_to_v4(conn)
        report["from_version"] = version
        report["to_version"] = DB_SCHEMA_VERSION_V4
        reports.append(report)
    return reports


def ensure_super_admin_account() -> None:
    """确保系统始终存在且仅存在一个超级管理员账号。"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()

        rows = conn.execute(
            "SELECT id, username, role, status, created_at FROM accounts WHERE role='super_admin' ORDER BY id ASC"
        ).fetchall()

        # 若不存在超级管理员，则初始化默认账号：admin / admin_sc
        if not rows:
            conn.execute(
                """
                INSERT INTO accounts (
                    username, password_hash, role, status,
                    allowed_brokers, trade_server_address, ts_address, broker_tag,
                    description, created_at, updated_at
                )
                VALUES (?, ?, 'super_admin', 'active', '[]', '', '', '', ?, ?, ?)
                """,
                ("admin", _sha256("admin_sc"), "system built-in super admin", now, now),
            )
            conn.commit()
            return

        # 仅保留最早的 super_admin；其余降级为 admin
        keeper = dict(rows[0])
        conn.execute(
            "UPDATE accounts SET status='active', updated_at=? WHERE id=?",
            (now, keeper["id"]),
        )
        if len(rows) > 1:
            demote_ids = [r["id"] for r in rows[1:]]
            placeholders = ",".join(["?"] * len(demote_ids))
            conn.execute(
                f"UPDATE accounts SET role='admin', updated_at=? WHERE id IN ({placeholders})",
                [now, *demote_ids],
            )

        conn.commit()
    finally:
        conn.close()



def init_db() -> list[dict]:
    """Initialize the database schema and run required migrations."""
    conn = _get_conn()
    reports: list[dict] = []
    try:
        reports = run_migrations(conn)
        conn.commit()
        log.info(f"Database initialized: {_DB_PATH} (schema_version={_get_user_version(conn)})")
    finally:
        conn.close()

    ensure_super_admin_account()
    cleanup_audit_logs()
    return reports


def verify_account(username: str, password: str) -> dict | None:
    """Validate account credentials."""
    conn = _get_conn()
    try:
        pw_hash = _sha256(password)
        row = conn.execute(
            "SELECT id, username, role, status, broker_tag, allowed_brokers, trade_server_address, description "
            "FROM accounts WHERE username = ? AND password_hash = ? AND status = 'active'",
            (username, pw_hash),
        ).fetchone()
        if row:
            _audit_log(username, "LOGIN", "account", f"Login success for {username}")
            return _build_account_compat(row)
        return None
    finally:
        conn.close()

def verify_web_admin(username: str, password: str) -> dict | None:
    """验证 Web 管理后台账号（仅 super_admin / admin）。"""
    conn = _get_conn()
    try:
        row = conn.execute(
            """
            SELECT id, username, role, status
            FROM accounts
            WHERE username = ? AND password_hash = ? AND status = 'active' AND role IN ('super_admin', 'admin')
            """,
            (username, _sha256(password)),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()



def get_broker_list() -> list[dict]:
    """???????????????"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            rows = conn.execute(
                """
                SELECT
                    n.server_id AS name,
                    COALESCE(NULLIF(cfg.broker_type, ''), NULLIF(n.broker_type, ''), 'TT') AS broker_type,
                    COALESCE(NULLIF(n.public_endpoint, ''), n.host, '') AS host,
                    0 AS port,
                    COALESCE(rt.status, 'offline') AS status
                FROM nodes n
                LEFT JOIN node_runtime rt ON rt.server_id = n.server_id
                LEFT JOIN node_broker_config cfg ON cfg.server_id = n.server_id
                ORDER BY n.created_at ASC, n.id ASC
                """
            ).fetchall()
            return [dict(r) for r in rows]

        rows = conn.execute(
            "SELECT name, broker_type, host, port, status FROM brokers WHERE status != 'deleted'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def check_ts_online(ts_address: str) -> dict:
    """???????????????????"""
    conn = _get_conn()
    try:
        if not ts_address:
            return {"online": False, "reason": "?????????"}

        addr_part = ts_address.split(":")[0].strip() if ":" in ts_address else ts_address.strip()
        if not addr_part:
            return {"online": False, "reason": "????????", "address": ts_address}

        if _has_v2_schema(conn):
            row = conn.execute(
                """
                SELECT n.server_id, n.node_name, rt.current_ip, n.host,
                       COALESCE(rt.status, 'offline') AS runtime_status,
                       rt.occupied_by, rt.occupied_at
                FROM nodes n
                LEFT JOIN node_runtime rt ON rt.server_id = n.server_id
                WHERE (rt.current_ip = ? OR rt.current_ip LIKE ?)
                  AND COALESCE(rt.status, 'offline') IN ('online', 'approved')
                LIMIT 1
                """,
                (addr_part, f"{addr_part}%"),
            ).fetchone()
            if row and row["runtime_status"] == "online":
                r = dict(row)
                return {
                    "online": True,
                    "node_name": r["node_name"],
                    "server_id": r["server_id"],
                    "match_field": "current_ip",
                    "address": ts_address,
                    "occupied_by": r.get("occupied_by", "") or "",
                    "occupied_at": r.get("occupied_at", "") or "",
                }

            row = conn.execute(
                """
                SELECT n.server_id, n.node_name, rt.current_ip, n.host,
                       COALESCE(rt.status, 'offline') AS runtime_status,
                       rt.occupied_by, rt.occupied_at
                FROM nodes n
                LEFT JOIN node_runtime rt ON rt.server_id = n.server_id
                WHERE n.host = ?
                  AND COALESCE(rt.status, 'offline') IN ('online', 'approved')
                LIMIT 1
                """,
                (addr_part,),
            ).fetchone()
            if row and row["runtime_status"] == "online":
                r = dict(row)
                return {
                    "online": True,
                    "node_name": r["node_name"],
                    "server_id": r["server_id"],
                    "match_field": "host",
                    "address": ts_address,
                    "occupied_by": r.get("occupied_by", "") or "",
                    "occupied_at": r.get("occupied_at", "") or "",
                }

        row = conn.execute(
            """
            SELECT nr.server_id, nr.node_name, nr.current_ip, nr.host,
                   b.status AS broker_status, nr.status AS req_status,
                   nr.occupied_by, nr.occupied_at
            FROM node_requests nr
            LEFT JOIN brokers b ON nr.server_id = b.name
            WHERE (nr.current_ip = ? OR nr.current_ip LIKE ?)
              AND nr.status IN ('online', 'approved')
            LIMIT 1
            """,
            (addr_part, f"{addr_part}%"),
        ).fetchone()
        if row and dict(row).get("req_status") == "online":
            r = dict(row)
            return {
                "online": True,
                "node_name": r["node_name"],
                "server_id": r["server_id"],
                "match_field": "current_ip",
                "address": ts_address,
                "occupied_by": r.get("occupied_by", "") or "",
                "occupied_at": r.get("occupied_at", "") or "",
            }

        row = conn.execute(
            """
            SELECT nr.server_id, nr.node_name, nr.current_ip, nr.host,
                   b.status AS broker_status, nr.status AS req_status,
                   nr.occupied_by, nr.occupied_at
            FROM node_requests nr
            LEFT JOIN brokers b ON nr.server_id = b.name
            WHERE nr.host = ?
              AND nr.status IN ('online', 'approved')
            LIMIT 1
            """,
            (addr_part,),
        ).fetchone()
        if row and dict(row).get("req_status") == "online":
            r = dict(row)
            return {
                "online": True,
                "node_name": r["node_name"],
                "server_id": r["server_id"],
                "match_field": "host",
                "address": ts_address,
                "occupied_by": r.get("occupied_by", "") or "",
                "occupied_at": r.get("occupied_at", "") or "",
            }

        return {
            "online": False,
            "reason": f"?????? '{ts_address}' ???????",
            "address": ts_address,
            "occupied_by": "",
            "occupied_at": "",
        }
    except Exception as e:
        log.error(f"check_ts_online failed: {e}")
        return {
            "online": False,
            "reason": f"????: {e}",
            "address": ts_address,
            "occupied_by": "",
            "occupied_at": "",
        }
    finally:
        conn.close()

def register_broker(name: str, broker_type: str = "tastytrade",
                    host: str = "", port: int = 0, config: dict | None = None) -> bool:
    """????????????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cfg = dict(config or {})
        credentials = dict(cfg.get("credentials", {}) or {})
        enabled = bool(cfg.get("enabled", True))
        normalized_type = broker_type or cfg.get("broker_type", "") or "TT"
        if _has_v2_schema(conn):
            conn.execute(
                """
                INSERT INTO nodes (
                    server_id, node_name, broker_type, host, token,
                    description, capabilities_json, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, '', '', '[]', 1, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    node_name=excluded.node_name,
                    broker_type=excluded.broker_type,
                    host=excluded.host,
                    updated_at=excluded.updated_at
                """,
                (name, name, normalized_type, host or '', now, now),
            )
            _upsert_node_runtime(
                conn,
                server_id=name,
                status='online',
                current_ip='',
                last_heartbeat=now,
                updated_at=now,
            )
            _upsert_node_broker_config(
                conn,
                server_id=name,
                broker_type=normalized_type,
                credentials=credentials,
                enabled=enabled,
                config_version=cfg.get('config_version', 0) or 0,
                updated_at=now,
            )
        else:
            conn.execute(
                """
                INSERT INTO brokers (name, broker_type, host, port, config, status, registered_at, created_at)
                VALUES (?, ?, ?, ?, ?, 'online', ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    broker_type=excluded.broker_type,
                    host=excluded.host,
                    port=excluded.port,
                    config=excluded.config,
                    status='online',
                    last_heartbeat=?
                """,
                (name, normalized_type, host, port, json.dumps(cfg, ensure_ascii=False), now, now, now),
            )
        conn.commit()
        return True
    except Exception as e:
        log.error(f"register_broker failed: {e}")
        return False
    finally:
        conn.close()


def update_broker_heartbeat(name: str):
    """?????????????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            node = _ensure_v2_node_record(conn, name)
            if node:
                rt = conn.execute(
                    "SELECT status, current_ip, occupied_by, occupied_at FROM node_runtime WHERE server_id = ?",
                    (name,),
                ).fetchone()
                rt_data = dict(rt) if rt else {}
                _upsert_node_runtime(
                    conn,
                    server_id=name,
                    status=rt_data.get('status', 'online') or 'online',
                    current_ip=rt_data.get('current_ip', '') or '',
                    last_heartbeat=now,
                    occupied_by=rt_data.get('occupied_by', '') or '',
                    occupied_at=rt_data.get('occupied_at', '') or '',
                    updated_at=now,
                )
                conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (now, name))
        else:
            conn.execute("UPDATE brokers SET last_heartbeat=?, status='online' WHERE name=?", (now, name))
        conn.commit()
    finally:
        conn.close()

def create_node_request(request_id: str, node_name: str, region: str = "",
                        host: str = "", capabilities: list | None = None,
                        contact: str = "", description: str = "",
                        public_ip: str = "", source_ip: str = "",
                        expire_hours: int = 24) -> dict | None:
    """?????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc)
        from datetime import timedelta

        expire_at = (now + timedelta(hours=expire_hours)).isoformat()
        capabilities_json = json.dumps(capabilities or [], ensure_ascii=False)
        if _has_v2_schema(conn):
            conn.execute(
                """
                INSERT INTO node_registration_requests_v2 (
                    request_id, node_name, broker_type, host, public_ip, source_ip, capabilities_json,
                    contact, description, status, server_id, token,
                    assigned_domain, public_endpoint, reviewed_by, reviewed_at,
                    reject_reason, expire_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '', '', '', '', '', '', ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    node_name=excluded.node_name,
                    broker_type=excluded.broker_type,
                    host=excluded.host,
                    public_ip=excluded.public_ip,
                    source_ip=excluded.source_ip,
                    capabilities_json=excluded.capabilities_json,
                    contact=excluded.contact,
                    description=excluded.description,
                    status='pending',
                    server_id='',
                    token='',
                    assigned_domain='',
                    public_endpoint='',
                    reviewed_by='',
                    reviewed_at='',
                    reject_reason='',
                    expire_at=excluded.expire_at,
                    created_at=excluded.created_at
                """,
                (
                    request_id, node_name, region, host, public_ip, source_ip,
                    capabilities_json, contact, description, expire_at, now.isoformat(),
                ),
            )
            row = conn.execute("SELECT * FROM node_registration_requests_v2 WHERE request_id = ?", (request_id,)).fetchone()
            result = _map_request_v2_to_legacy(row)
        else:
            conn.execute(
                """
                INSERT INTO node_requests
                    (request_id, node_name, region, host, capabilities,
                     contact, description, status, expire_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (request_id, node_name, region, host, capabilities_json, contact, description, expire_at, now.isoformat()),
            )
            row = conn.execute("SELECT * FROM node_requests WHERE request_id = ?", (request_id,)).fetchone()
            result = dict(row) if row else None
        conn.commit()
        log.info(f"Node registration request created: {request_id} ({node_name})")
        return result
    except Exception as e:
        log.error(f"create_node_request failed: {e}")
        return None
    finally:
        conn.close()


def get_node_request_by_id(request_id: str) -> dict | None:
    """?? request_id ???????"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            row = conn.execute("SELECT * FROM node_registration_requests_v2 WHERE request_id = ?", (request_id,)).fetchone()
            return _map_request_v2_to_legacy(row)
        row = conn.execute("SELECT * FROM node_requests WHERE request_id = ?", (request_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pending_node_requests() -> list[dict]:
    """?????????????"""
    conn = _get_conn()
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            rows = conn.execute(
                "SELECT * FROM node_registration_requests_v2 WHERE status = 'pending' AND expire_at > ? ORDER BY created_at ASC",
                (now_iso,),
            ).fetchall()
            return [_map_request_v2_to_legacy(r) for r in rows]
        rows = conn.execute(
            "SELECT * FROM node_requests WHERE status = 'pending' AND expire_at > ? ORDER BY created_at ASC",
            (now_iso,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def approve_node_request(
    request_id: str,
    reviewer: str = "admin",
    domain_assignment: dict | None = None,
) -> dict | None:
    """????????????????????"""
    import secrets

    conn = _get_conn()
    try:
        # Serialize approval of one pending request. Domain allocation happens
        # before this function, so a losing concurrent approver must receive
        # None and let the caller release its reserved domain.
        conn.execute("BEGIN IMMEDIATE")
        has_v2 = _has_v2_schema(conn)
        if has_v2:
            req_row = conn.execute(
                "SELECT * FROM node_registration_requests_v2 WHERE request_id = ? AND status = 'pending'",
                (request_id,),
            ).fetchone()
            if not req_row:
                conn.rollback()
                return None
            req = _map_request_v2_to_legacy(req_row)
        else:
            req_row = conn.execute(
                "SELECT * FROM node_requests WHERE request_id = ? AND status = 'pending'",
                (request_id,),
            ).fetchone()
            if not req_row:
                conn.rollback()
                return None
            req = dict(req_row)

        now = datetime.now(timezone.utc).isoformat()
        server_id = f"node_{req['node_name']}_{secrets.token_hex(4)}"
        token = secrets.token_urlsafe(32)
        approved_broker_type = (req['region'] or 'TT').strip() or 'TT'
        caps_json = _load_json_list(req['capabilities'])
        assignment = domain_assignment or {}
        domain_id = int(assignment.get("id") or 0)
        assigned_domain = (assignment.get("fqdn") or "").strip().lower()
        public_endpoint = (assignment.get("public_endpoint") or "").strip()
        public_ip = (req.get("public_ip") or req.get("source_ip") or "").strip()

        if has_v2:
            request_update = conn.execute(
                """
                UPDATE node_registration_requests_v2
                SET status='approved', server_id=?, token=?, assigned_domain=?, public_endpoint=?,
                    reviewed_by=?, reviewed_at=?, reject_reason=''
                WHERE request_id=? AND status='pending'
                """,
                (server_id, token, assigned_domain, public_endpoint, reviewer, now, request_id),
            )
            if request_update.rowcount != 1:
                conn.rollback()
                return None
            conn.execute(
                """
                INSERT INTO nodes (
                    server_id, node_name, broker_type, host, public_ip,
                    assigned_domain, public_endpoint, token,
                    description, capabilities_json, enabled, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(server_id) DO UPDATE SET
                    node_name=excluded.node_name,
                    broker_type=excluded.broker_type,
                    host=excluded.host,
                    public_ip=excluded.public_ip,
                    assigned_domain=excluded.assigned_domain,
                    public_endpoint=excluded.public_endpoint,
                    token=excluded.token,
                    description=excluded.description,
                    capabilities_json=excluded.capabilities_json,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (
                    server_id,
                    req['node_name'],
                    approved_broker_type,
                    req['host'] or '',
                    public_ip,
                    assigned_domain,
                    public_endpoint,
                    token,
                    req['description'] or '',
                    json.dumps(caps_json, ensure_ascii=False),
                    req['created_at'] or now,
                    now,
                ),
            )
            _upsert_node_runtime(conn, server_id, 'approved', public_ip, '', '', '', now)
            _upsert_node_broker_config(conn, server_id, approved_broker_type, {}, True, 0, now)
            if domain_id:
                cursor = conn.execute(
                    """
                    UPDATE ts_domain_pool
                    SET status='occupied', assigned_server_id=?, assigned_node_name=?, assigned_ip=?,
                        public_endpoint=?, dns_record_id=?, dns_status=?, dns_error='',
                        cooldown_until='', updated_at=?
                    WHERE id=? AND status='allocating'
                    """,
                    (
                        server_id,
                        req['node_name'],
                        public_ip,
                        public_endpoint,
                        str(assignment.get("dns_record_id") or ""),
                        str(assignment.get("dns_status") or "active"),
                        now,
                        domain_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("domain assignment is no longer reserved")
            row = conn.execute("SELECT * FROM node_registration_requests_v2 WHERE request_id = ?", (request_id,)).fetchone()
            result = _map_request_v2_to_legacy(row)
        else:
            request_update = conn.execute(
                """
                UPDATE node_requests SET status='approved', server_id=?, token=?, reviewed_by=?, reviewed_at=?
                WHERE request_id=? AND status='pending'
                """,
                (server_id, token, reviewer, now, request_id),
            )
            if request_update.rowcount != 1:
                conn.rollback()
                return None
            row = conn.execute("SELECT * FROM node_requests WHERE request_id = ?", (request_id,)).fetchone()
            result = dict(row) if row else None

        conn.commit()
        if result is not None:
            result["assigned_domain"] = assigned_domain
            result["public_endpoint"] = public_endpoint
            result["public_ip"] = public_ip
        log.info(f"Node request approved: {request_id} -> server_id={server_id}")
        return result
    except Exception as e:
        conn.rollback()
        log.error(f"approve_node_request failed: {e}")
        return None
    finally:
        conn.close()


def reject_node_request(request_id: str, reason: str = "", reviewer: str = "admin") -> bool:
    """???????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            cursor = conn.execute(
                "UPDATE node_registration_requests_v2 SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=? WHERE request_id = ? AND status = 'pending'",
                (reason, reviewer, now, request_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE node_requests SET status='rejected', reject_reason=?, reviewed_by=?, reviewed_at=? WHERE request_id = ? AND status = 'pending'",
                (reason, reviewer, now, request_id),
            )
        conn.commit()
        ok = cursor.rowcount > 0
        if ok:
            log.info(f"Node request rejected: {request_id} reason={reason}")
        return ok
    finally:
        conn.close()


def cancel_node_request(
    request_id: str,
    reason: str = "node_cancelled",
    reviewer: str = "se_node",
    force_discard_approved: bool = True,
) -> dict:
    """???????????"""
    conn = _get_conn()
    try:
        has_v2 = _has_v2_schema(conn)
        if has_v2:
            row = conn.execute("SELECT * FROM node_registration_requests_v2 WHERE request_id = ?", (request_id,)).fetchone()
            req = _map_request_v2_to_legacy(row)
        else:
            row = conn.execute("SELECT * FROM node_requests WHERE request_id = ?", (request_id,)).fetchone()
            req = dict(row) if row else None
        if not req:
            return {"ok": False, "error": "request_not_found"}

        status = (req.get("status") or "").strip()
        now = datetime.now(timezone.utc).isoformat()
        if status == "pending":
            if has_v2:
                conn.execute(
                    "UPDATE node_registration_requests_v2 SET status='cancelled', reject_reason=?, reviewed_by=?, reviewed_at=? WHERE request_id = ?",
                    (reason or "node_cancelled", reviewer, now, request_id),
                )
            else:
                conn.execute(
                    "UPDATE node_requests SET status='cancelled', reject_reason=?, reviewed_by=?, reviewed_at=? WHERE request_id = ?",
                    (reason or "node_cancelled", reviewer, now, request_id),
                )
            conn.commit()
            return {"ok": True, "action": "cancelled_pending", "request_id": request_id}

        if status in ("approved", "online", "offline", "suspended"):
            server_id = req.get("server_id", "")
            if not force_discard_approved:
                return {"ok": False, "error": "already_approved", "status": status, "server_id": server_id}
            if has_v2:
                if server_id:
                    conn.execute("DELETE FROM nodes WHERE server_id = ?", (server_id,))
                conn.execute("DELETE FROM node_registration_requests_v2 WHERE request_id = ?", (request_id,))
            else:
                if server_id:
                    conn.execute("DELETE FROM brokers WHERE name = ?", (server_id,))
                conn.execute("DELETE FROM node_requests WHERE request_id = ?", (request_id,))
            conn.commit()
            log.warning(f"Node request discarded after approval: {request_id}, server_id={server_id}")
            return {"ok": True, "action": "discarded_approved", "request_id": request_id, "server_id": server_id}

        return {"ok": True, "action": "already_final", "request_id": request_id, "status": status}
    except Exception as e:
        log.error(f"cancel_node_request failed: {e}")
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def cleanup_expired_requests() -> int:
    """????????????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            cursor = conn.execute(
                "UPDATE node_registration_requests_v2 SET status='expired' WHERE status = 'pending' AND expire_at <= ?",
                (now,),
            )
        else:
            cursor = conn.execute(
                "UPDATE node_requests SET status='expired' WHERE status = 'pending' AND expire_at <= ?",
                (now,),
            )
        count = cursor.rowcount
        if count > 0:
            conn.commit()
            log.info(f"Cleaned {count} expired node registration request(s)")
        return count
    finally:
        conn.close()

def verify_node_token(token: str) -> dict | None:
    """???? Bearer Token?"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            row = conn.execute(
                """
                SELECT
                    n.server_id,
                    n.node_name,
                    n.host,
                    n.public_ip,
                    n.assigned_domain,
                    n.public_endpoint,
                    n.token,
                    n.description,
                    n.created_at,
                    COALESCE(rt.status, 'approved') AS status,
                    COALESCE(rt.current_ip, '') AS current_ip,
                    COALESCE(rt.occupied_by, '') AS occupied_by,
                    COALESCE(rt.occupied_at, '') AS occupied_at,
                    COALESCE(rt.last_heartbeat, '') AS last_heartbeat,
                    COALESCE(n.broker_type, '') AS region
                FROM nodes n
                LEFT JOIN node_runtime rt ON rt.server_id = n.server_id
                WHERE n.token = ?
                  AND n.enabled = 1
                  AND COALESCE(rt.status, 'approved') IN ('approved', 'online', 'offline', 'suspended')
                LIMIT 1
                """,
                (token,),
            ).fetchone()
            if row:
                data = dict(row)
                data["req_status"] = data.get("status", "approved")
                return data

        row = conn.execute(
            "SELECT * FROM node_requests WHERE token = ? AND status IN ('approved', 'online', 'offline', 'suspended')",
            (token,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_node_heartbeat(server_id: str, current_ip: str = "") -> bool:
    """??????????????? IP?"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            node_row = _ensure_v2_node_record(conn, server_id)
            if not node_row:
                return False
            runtime_row = conn.execute(
                "SELECT status, occupied_by, occupied_at FROM node_runtime WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            runtime = dict(runtime_row) if runtime_row else {}
            current_status = runtime.get('status', 'approved') or 'approved'
            if current_status not in ('online', 'offline', 'approved'):
                current_status = 'online'
            _upsert_node_runtime(
                conn,
                server_id=server_id,
                status='online',
                current_ip=current_ip,
                last_heartbeat=now,
                occupied_by=runtime.get('occupied_by', '') or '',
                occupied_at=runtime.get('occupied_at', '') or '',
                updated_at=now,
            )
            conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (now, server_id))
            conn.commit()
            return True

        conn.execute(
            "UPDATE node_requests SET current_ip=?, status='online' WHERE server_id = ? AND status IN ('online', 'offline', 'approved')",
            (current_ip, server_id),
        )
        conn.execute(
            "UPDATE brokers SET last_heartbeat=?, status='online' WHERE name = ? AND status IN ('online', 'offline', 'approved')",
            (now, server_id),
        )
        conn.commit()
        return True
    except Exception as e:
        log.error(f"update_node_heartbeat failed: {e}")
        return False
    finally:
        conn.close()

def get_node_broker_config(server_id: str) -> dict | None:
    """????????????"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            row = conn.execute(
                """
                SELECT cfg.broker_type, cfg.credentials_json, cfg.enabled, cfg.config_version, n.broker_type AS node_broker_type
                FROM nodes n
                LEFT JOIN node_broker_config cfg ON cfg.server_id = n.server_id
                WHERE n.server_id = ?
                LIMIT 1
                """,
                (server_id,),
            ).fetchone()
            if row:
                data = dict(row)
                broker_type = data.get("broker_type") or data.get("node_broker_type") or "TT"
                credentials = _load_json_dict(data.get("credentials_json"))
                enabled = bool(data.get("enabled", 1))
                return _build_broker_config_response(
                    broker_type=broker_type,
                    credentials=credentials,
                    enabled=enabled,
                    config_version=data.get("config_version", 0) or 0,
                )

        row = conn.execute("SELECT config, config_version, broker_type FROM brokers WHERE name = ?", (server_id,)).fetchone()
        if not row:
            return None
        cfg = _load_json_dict(row["config"])
        credentials = dict(cfg.get("credentials", {}) or {})
        return _build_broker_config_response(
            broker_type=row["broker_type"] or cfg.get("broker_type", "TT"),
            credentials=credentials,
            enabled=cfg.get("enabled", True),
            config_version=row["config_version"] or 0,
            raw_config=cfg,
        )
    except Exception as e:
        log.error(f"get_node_broker_config failed: {e}")
        return None
    finally:
        conn.close()

def get_node_broker_configs(server_ids: list[str]) -> dict[str, dict]:
    """Return broker configs for multiple nodes in one database round-trip."""
    ids = [str(sid or "").strip() for sid in server_ids if str(sid or "").strip()]
    if not ids:
        return {}

    placeholders = ",".join("?" for _ in ids)
    conn = _get_conn()
    try:
        configs: dict[str, dict] = {}
        if _has_v2_schema(conn):
            rows = conn.execute(
                f"""
                SELECT n.server_id,
                       cfg.broker_type,
                       cfg.credentials_json,
                       cfg.enabled,
                       cfg.config_version,
                       n.broker_type AS node_broker_type
                FROM nodes n
                LEFT JOIN node_broker_config cfg ON cfg.server_id = n.server_id
                WHERE n.server_id IN ({placeholders})
                """,
                ids,
            ).fetchall()
            for row in rows:
                data = dict(row)
                broker_type = data.get("broker_type") or data.get("node_broker_type") or "TT"
                credentials = _load_json_dict(data.get("credentials_json"))
                enabled = bool(data.get("enabled", 1))
                configs[data["server_id"]] = _build_broker_config_response(
                    broker_type=broker_type,
                    credentials=credentials,
                    enabled=enabled,
                    config_version=data.get("config_version", 0) or 0,
                )
            return configs

        rows = conn.execute(
            f"SELECT name, config, config_version, broker_type FROM brokers WHERE name IN ({placeholders})",
            ids,
        ).fetchall()
        for row in rows:
            cfg = _load_json_dict(row["config"])
            credentials = dict(cfg.get("credentials", {}) or {})
            configs[row["name"]] = _build_broker_config_response(
                broker_type=row["broker_type"] or cfg.get("broker_type", "TT"),
                credentials=credentials,
                enabled=cfg.get("enabled", True),
                config_version=row["config_version"] or 0,
                raw_config=cfg,
            )
        return configs
    except Exception as e:
        log.error(f"get_node_broker_configs failed: {e}")
        return {}
    finally:
        conn.close()

def set_node_broker_config(
    server_id: str,
    broker_type: str,
    credentials: dict | None = None,
    enabled: bool = True,
) -> bool:
    """????????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        has_v2 = _has_v2_schema(conn)
        if has_v2:
            node_row = _ensure_v2_node_record(conn, server_id)
            if not node_row:
                log.error(f"set_node_broker_config: server_id '{server_id}' not found")
                return False
            current = conn.execute(
                "SELECT broker_type, credentials_json, enabled, config_version FROM node_broker_config WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            new_version = int(current["config_version"] or 0) + 1 if current else 1
            normalized_type = broker_type or (current["broker_type"] if current else "") or node_row.get("broker_type", "TT") or "TT"
            conn.execute("UPDATE nodes SET broker_type = ?, updated_at = ? WHERE server_id = ?", (normalized_type, now, server_id))
            _upsert_node_broker_config(conn, server_id, normalized_type, credentials, enabled, new_version, now)
            conn.commit()
            log.info(f"Broker config updated for {server_id}: type={normalized_type}, version={new_version}")
            return True

        row = conn.execute("SELECT config, config_version FROM brokers WHERE name = ?", (server_id,)).fetchone()
        if not row:
            log.error(f"set_node_broker_config: server_id '{server_id}' not found")
            return False
        cfg = _load_json_dict(row["config"])
        new_version = (row["config_version"] or 0) + 1
        cfg["broker_type"] = broker_type
        cfg["credentials"] = credentials or {}
        cfg["enabled"] = enabled
        conn.execute(
            "UPDATE brokers SET broker_type = ?, config = ?, config_version = ?, last_heartbeat = ? WHERE name = ?",
            (broker_type, json.dumps(cfg, ensure_ascii=False), new_version, now, server_id),
        )
        conn.commit()
        log.info(f"Broker config updated for {server_id}: type={broker_type}, version={new_version}")
        return True
    except Exception as e:
        log.error(f"set_node_broker_config failed: {e}")
        return False
    finally:
        conn.close()


def increment_reload_flag(server_id: str) -> int:
    """???????????????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            node_row = _ensure_v2_node_record(conn, server_id)
            if not node_row:
                return 0
            current = conn.execute(
                "SELECT broker_type, credentials_json, enabled, config_version FROM node_broker_config WHERE server_id = ?",
                (server_id,),
            ).fetchone()
            if current:
                new_ver = int(current["config_version"] or 0) + 1
                broker_type = current["broker_type"] or node_row.get("broker_type", "TT") or "TT"
                credentials = _load_json_dict(current["credentials_json"])
                enabled = bool(current["enabled"])
            else:
                new_ver = 1
                broker_type = node_row.get("broker_type", "TT") or "TT"
                credentials = {}
                enabled = True
            _upsert_node_broker_config(conn, server_id, broker_type, credentials, enabled, new_ver, now)
            conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (now, server_id))
            conn.commit()
            log.info(f"Reload flag incremented for {server_id}: version={new_ver}")
            return new_ver

        row = conn.execute("SELECT config_version FROM brokers WHERE name = ?", (server_id,)).fetchone()
        if not row:
            return 0
        new_ver = (row["config_version"] or 0) + 1
        conn.execute("UPDATE brokers SET config_version = ? WHERE name = ?", (new_ver, server_id))
        conn.commit()
        log.info(f"Reload flag incremented for {server_id}: version={new_ver}")
        return new_ver
    except Exception as e:
        log.error(f"increment_reload_flag failed: {e}")
        return 0
    finally:
        conn.close()

def verify_node_token_for_config(token: str, target_server_id: str) -> bool:
    """?? token ?????? server_id?"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            row = conn.execute(
                "SELECT 1 FROM nodes WHERE token = ? AND server_id = ? AND enabled = 1",
                (token, target_server_id),
            ).fetchone()
            if row:
                return True

        row = conn.execute("SELECT server_id FROM node_requests WHERE token = ?", (token,)).fetchone()
        return bool(row and row["server_id"] == target_server_id)
    finally:
        conn.close()


HEARTBEAT_TIMEOUT_SECONDS = 60


def check_offline_nodes(timeout_seconds: int | None = None) -> int:
    """??????????????????"""
    timeout = timeout_seconds or HEARTBEAT_TIMEOUT_SECONDS
    conn = _get_conn()
    try:
        from datetime import timedelta

        cutoff_iso = (datetime.now(timezone.utc) - timedelta(seconds=timeout)).isoformat()
        if _has_v2_schema(conn):
            rows = conn.execute(
                """
                SELECT n.server_id, n.node_name,
                       COALESCE(rt.last_heartbeat, '') AS last_heartbeat,
                       COALESCE(rt.current_ip, '') AS current_ip
                FROM nodes n
                JOIN node_runtime rt ON rt.server_id = n.server_id
                WHERE COALESCE(rt.status, 'approved') = 'online'
                  AND (rt.last_heartbeat = '' OR rt.last_heartbeat < ?)
                """,
                (cutoff_iso,),
            ).fetchall()
            count = len(rows)
            if count == 0:
                return 0
            now = datetime.now(timezone.utc).isoformat()
            for row in rows:
                sid = row['server_id']
                _upsert_node_runtime(conn, sid, 'offline', row['current_ip'] or '', row['last_heartbeat'] or '', '', '', now)
                conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (now, sid))
                log.info(
                    f"Node marked OFFLINE & RELEASED: {sid} ({row['node_name']}) - "
                    f"last_heartbeat={row['last_heartbeat'] or '(never)'}, cutoff={cutoff_iso} [occupation auto-cleared]"
                )
            conn.commit()
            return count

        rows = conn.execute(
            """
            SELECT b.name AS server_id, nr.node_name, b.last_heartbeat,
                   nr.current_ip
            FROM brokers b
            JOIN node_requests nr ON nr.server_id = b.name
            WHERE b.status = 'online'
              AND (b.last_heartbeat IS NULL OR b.last_heartbeat < ?)
              AND nr.status = 'online'
            """,
            (cutoff_iso,),
        ).fetchall()
        count = len(rows)
        if count == 0:
            return 0
        for row in rows:
            sid = row['server_id']
            conn.execute("UPDATE node_requests SET status='offline', occupied_by='', occupied_at='' WHERE server_id = ?", (sid,))
            conn.execute("UPDATE brokers SET status='offline' WHERE name = ?", (sid,))
        conn.commit()
        return count
    except Exception as e:
        log.error(f"check_offline_nodes failed: {e}")
        return 0
    finally:
        conn.close()

def force_check_all_nodes() -> dict:
    """????????????????????"""
    try:
        from datetime import timedelta

        now_utc = datetime.now(timezone.utc)
        cutoff_iso = (now_utc - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)).isoformat()
        rows = get_all_nodes()

        nodes = []
        online_count = 0
        offline_count = 0
        suspended_count = 0
        occupied_count = 0

        for row in rows:
            n = dict(row)
            req_status = (n.get("req_status") or "").strip()
            occ_by = (n.get("occupied_by") or "").strip()
            last_hb = (n.get("last_heartbeat") or "").strip()
            is_alive = bool(last_hb and last_hb >= cutoff_iso)

            if req_status == "suspended":
                real_status = "suspended"
                suspended_count += 1
            elif req_status == "offline" or not is_alive:
                real_status = "offline"
                offline_count += 1
            elif occ_by:
                real_status = "occupied"
                occupied_count += 1
            else:
                real_status = "online"
                online_count += 1

            n["real_status"] = real_status
            nodes.append(n)

        log.info(
            f"[Refresh] completed: total={len(nodes)}, "
            f"online={online_count}, offline={offline_count}, "
            f"suspended={suspended_count}, occupied={occupied_count}"
        )
        return {
            "checked": len(nodes),
            "online": online_count,
            "offline": offline_count,
            "occupied": occupied_count,
            "suspended": suspended_count,
            "nodes": nodes,
        }
    except Exception as e:
        log.error(f"force_check_all_nodes failed: {e}")
        return {
            "checked": 0,
            "marked_offline": 0,
            "online": 0,
            "offline": 0,
            "occupied": 0,
            "suspended": 0,
            "nodes": [],
            "error": str(e),
        }

def get_all_nodes() -> list[dict]:
    """?????????????????????"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            rows = conn.execute(
                """
                SELECT
                    n.server_id,
                    n.node_name,
                    COALESCE(NULLIF(cfg.broker_type, ''), NULLIF(n.broker_type, ''), 'TT') AS region,
                    COALESCE(n.host, '') AS host,
                    COALESCE(n.public_ip, '') AS public_ip,
                    COALESCE(n.assigned_domain, '') AS assigned_domain,
                    COALESCE(n.public_endpoint, '') AS public_endpoint,
                    COALESCE(NULLIF(n.capabilities_json, ''), '[]') AS capabilities,
                    COALESCE(rt.status, 'approved') AS req_status,
                    COALESCE(rt.current_ip, '') AS current_ip,
                    COALESCE(n.token, '') AS token,
                    COALESCE(rt.status, 'approved') AS broker_status,
                    COALESCE(rt.last_heartbeat, '') AS last_heartbeat,
                    COALESCE(rt.occupied_by, '') AS occupied_by,
                    COALESCE(rt.occupied_at, '') AS occupied_at,
                    COALESCE(n.description, '') AS description,
                    COALESCE(cfg.broker_type, '') AS cfg_broker_type,
                    COALESCE(cfg.credentials_json, '{}') AS credentials_json,
                    COALESCE(cfg.enabled, 1) AS cfg_enabled,
                    COALESCE(cfg.config_version, 0) AS config_version,
                    COALESCE(n.created_at, '') AS created_at
                FROM nodes n
                LEFT JOIN node_runtime rt ON rt.server_id = n.server_id
                LEFT JOIN node_broker_config cfg ON cfg.server_id = n.server_id
                ORDER BY
                    CASE COALESCE(rt.status, 'approved')
                        WHEN 'online' THEN 1
                        WHEN 'suspended' THEN 2
                        WHEN 'approved' THEN 3
                        WHEN 'offline' THEN 4
                        ELSE 5
                    END,
                    n.created_at ASC,
                    n.id ASC
                """
            ).fetchall()
            results = []
            for row in rows:
                data = dict(row)
                broker_type = data.get("cfg_broker_type") or data.get("region") or "TT"
                credentials = _load_json_dict(data.get("credentials_json"))
                enabled = bool(data.get("cfg_enabled", 1))
                broker_cfg = _build_legacy_broker_config_payload(
                    broker_type=broker_type,
                    credentials=credentials,
                    enabled=enabled,
                )
                data["broker_config"] = json.dumps(broker_cfg, ensure_ascii=False)
                results.append(data)
            return results

        rows = conn.execute(
            """
            SELECT nr.server_id, nr.node_name, nr.region, nr.host,
                   nr.capabilities, nr.status AS req_status, nr.current_ip,
                   nr.token, b.status AS broker_status, b.last_heartbeat,
                   nr.occupied_by, nr.occupied_at, nr.description,
                   b.config AS broker_config
            FROM node_requests nr
            LEFT JOIN brokers b ON nr.server_id = b.name
            WHERE nr.status IN ('approved', 'online', 'suspended', 'offline')
            ORDER BY
                CASE nr.status
                    WHEN 'online' THEN 1
                    WHEN 'suspended' THEN 2
                    WHEN 'approved' THEN 3
                    WHEN 'offline' THEN 4
                END,
                nr.created_at ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

def get_approved_nodes_for_memory_load() -> list[dict]:
    """
    加载所有已批准节点的配置数据（供 node_state 启动时初始化内存用）。

    返回每个节点的完整配置字段 + DB 中存储的状态快照。
    实时状态以返回数据为准，启动后由心跳覆盖。
    """
    return get_all_nodes()


def _refresh_expired_domain_cooldowns(conn: sqlite3.Connection, now_iso: str) -> int:
    cursor = conn.execute(
        """
        UPDATE ts_domain_pool
        SET status='available', assigned_server_id='', assigned_node_name='', assigned_ip='',
            dns_record_id='', dns_status='released', dns_error='', cooldown_until='', updated_at=?
        WHERE status='cooling' AND cooldown_until <> '' AND cooldown_until <= ?
        """,
        (now_iso, now_iso),
    )
    return cursor.rowcount


def import_ts_domain_pool(entries: list[dict]) -> dict:
    conn = _get_conn()
    inserted = 0
    updated = 0
    try:
        now = datetime.now(timezone.utc).isoformat()
        for entry in entries:
            fqdn = (entry.get("fqdn") or "").strip().lower().strip(".")
            if not fqdn:
                continue
            existing = conn.execute(
                "SELECT id FROM ts_domain_pool WHERE fqdn = ?",
                (fqdn,),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO ts_domain_pool (
                    fqdn, root_domain, record_name, status, public_endpoint,
                    dns_status, created_at, updated_at
                ) VALUES (?, ?, ?, 'available', ?, 'pending', ?, ?)
                ON CONFLICT(fqdn) DO UPDATE SET
                    root_domain=excluded.root_domain,
                    record_name=excluded.record_name,
                    public_endpoint=excluded.public_endpoint,
                    updated_at=excluded.updated_at
                """,
                (
                    fqdn,
                    (entry.get("root_domain") or "").strip().lower(),
                    (entry.get("record_name") or "").strip().lower(),
                    (entry.get("public_endpoint") or "").strip(),
                    now,
                    now,
                ),
            )
            if existing:
                updated += 1
            else:
                inserted += 1
        conn.commit()
        return {
            "ok": True,
            "inserted": inserted,
            "updated": updated,
            "existing": updated,
        }
    except Exception as e:
        conn.rollback()
        log.error(f"import_ts_domain_pool failed: {e}")
        return {
            "ok": False,
            "error": str(e),
            "inserted": inserted,
            "updated": updated,
            "existing": updated,
        }
    finally:
        conn.close()


def list_ts_domain_pool(page: int = 1, page_size: int = 20, status: str = "") -> dict:
    conn = _get_conn()
    try:
        safe_page = max(1, int(page or 1))
        safe_size = max(1, min(int(page_size or 20), 100))
        now = datetime.now(timezone.utc).isoformat()
        refreshed = _refresh_expired_domain_cooldowns(conn, now)
        if refreshed:
            conn.commit()
        where = ""
        params: list = []
        normalized_status = (status or "").strip().lower()
        if normalized_status:
            where = "WHERE status = ?"
            params.append(normalized_status)
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM ts_domain_pool {where}",
            params,
        ).fetchone()[0])
        rows = conn.execute(
            f"""
            SELECT * FROM ts_domain_pool
            {where}
            ORDER BY
                CASE status
                    WHEN 'allocating' THEN 1
                    WHEN 'occupied' THEN 2
                    WHEN 'cooling' THEN 3
                    WHEN 'error' THEN 4
                    ELSE 5
                END,
                id ASC
            LIMIT ? OFFSET ?
            """,
            [*params, safe_size, (safe_page - 1) * safe_size],
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": safe_page,
            "page_size": safe_size,
            "total": total,
            "pages": max(1, (total + safe_size - 1) // safe_size),
        }
    finally:
        conn.close()


def get_ts_domain_pool_entry(domain_id: int) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM ts_domain_pool WHERE id = ?",
            (int(domain_id),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_ts_domain_for_server(server_id: str) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM ts_domain_pool WHERE assigned_server_id = ? LIMIT 1",
            ((server_id or "").strip(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_ts_domain_options() -> list[dict]:
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, fqdn, public_endpoint, assigned_server_id,
                   assigned_node_name, assigned_ip, dns_status
            FROM ts_domain_pool
            WHERE status='occupied' AND assigned_server_id <> ''
            ORDER BY fqdn ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def reserve_ts_domain(node_name: str, public_ip: str) -> dict | None:
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat()
        _refresh_expired_domain_cooldowns(conn, now)
        row = conn.execute(
            "SELECT * FROM ts_domain_pool WHERE status='available' ORDER BY id ASC LIMIT 1"
        ).fetchone()
        if not row:
            conn.rollback()
            return None
        cursor = conn.execute(
            """
            UPDATE ts_domain_pool
            SET status='allocating', assigned_server_id='', assigned_node_name=?, assigned_ip=?,
                dns_status='updating', dns_error='', cooldown_until='', updated_at=?
            WHERE id=? AND status='available'
            """,
            ((node_name or "").strip(), (public_ip or "").strip(), now, row["id"]),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        result = dict(row)
        result.update({
            "status": "allocating",
            "assigned_node_name": (node_name or "").strip(),
            "assigned_ip": (public_ip or "").strip(),
            "dns_status": "updating",
        })
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_reserved_domain_dns(domain_id: int, record_id: str, dns_status: str = "active") -> bool:
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            UPDATE ts_domain_pool
            SET dns_record_id=?, dns_status=?, dns_error='',
                status=CASE WHEN assigned_server_id <> '' THEN 'occupied' ELSE status END,
                updated_at=?
            WHERE id=? AND status IN ('allocating', 'occupied', 'error')
            """,
            ((record_id or "").strip(), (dns_status or "active").strip(), now, int(domain_id)),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def abort_reserved_domain(domain_id: int, error: str = "", reusable: bool = True) -> bool:
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        status = "available" if reusable else "error"
        dns_status = "released" if reusable else "error"
        cursor = conn.execute(
            """
            UPDATE ts_domain_pool
            SET status=?, assigned_server_id='', assigned_node_name='', assigned_ip='',
                dns_record_id='', dns_status=?, dns_error=?, cooldown_until='', updated_at=?
            WHERE id=? AND status='allocating'
            """,
            (status, dns_status, (error or "").strip(), now, int(domain_id)),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def release_ts_domain_for_server(
    server_id: str,
    cooldown_seconds: int,
    dns_status: str,
    dns_error: str = "",
) -> dict | None:
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM ts_domain_pool WHERE assigned_server_id = ? LIMIT 1",
            ((server_id or "").strip(),),
        ).fetchone()
        if not row:
            return None
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        has_error = bool((dns_error or "").strip())
        next_status = "error" if has_error else "cooling"
        cooldown_until = "" if has_error else (now_dt + timedelta(seconds=max(0, int(cooldown_seconds)))).isoformat()
        conn.execute(
            """
            UPDATE ts_domain_pool
            SET status=?, assigned_server_id='', assigned_node_name='', assigned_ip='',
                dns_record_id='', dns_status=?, dns_error=?, cooldown_until=?, updated_at=?
            WHERE id=?
            """,
            (
                next_status,
                (dns_status or ("error" if has_error else "released")).strip(),
                (dns_error or "").strip(),
                cooldown_until,
                now,
                row["id"],
            ),
        )
        conn.commit()
        result = dict(row)
        result.update({
            "status": next_status,
            "assigned_server_id": "",
            "assigned_node_name": "",
            "assigned_ip": "",
            "dns_status": dns_status,
            "dns_error": dns_error,
            "cooldown_until": cooldown_until,
            "updated_at": now,
        })
        return result
    finally:
        conn.close()


def mark_ts_domain_error(domain_id: int, error: str, dns_status: str = "error") -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """
            UPDATE ts_domain_pool
            SET status='error', dns_status=?, dns_error=?, updated_at=?
            WHERE id=?
            """,
            (
                (dns_status or "error").strip(),
                (error or "unknown error").strip(),
                datetime.now(timezone.utc).isoformat(),
                int(domain_id),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def reset_ts_domain_entry(domain_id: int) -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """
            UPDATE ts_domain_pool
            SET status='available', assigned_server_id='', assigned_node_name='', assigned_ip='',
                dns_record_id='', dns_status='released', dns_error='', cooldown_until='', updated_at=?
            WHERE id=? AND assigned_server_id=''
            """,
            (datetime.now(timezone.utc).isoformat(), int(domain_id)),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def begin_delete_ts_domain(domain_id: int) -> dict:
    conn = _get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM ts_domain_pool WHERE id = ?",
            (int(domain_id),),
        ).fetchone()
        if not row:
            conn.rollback()
            return {"ok": False, "error": "domain not found"}

        entry = dict(row)
        if (entry.get("assigned_server_id") or "").strip():
            conn.rollback()
            return {
                "ok": False,
                "error": "domain is assigned; delete the TS node first",
                "entry": entry,
            }

        status = (entry.get("status") or "").strip().lower()
        if status not in {"available", "cooling", "error"}:
            conn.rollback()
            return {
                "ok": False,
                "error": f"domain cannot be deleted while status is {status or '-'}",
                "entry": entry,
            }

        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            """
            UPDATE ts_domain_pool
            SET status='deleting', dns_status='deleting', dns_error='', updated_at=?
            WHERE id=? AND assigned_server_id='' AND status=?
            """,
            (now, int(domain_id), status),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return {
                "ok": False,
                "error": "domain state changed before deletion",
                "entry": entry,
            }
        conn.commit()
        entry.update({
            "status": "deleting",
            "previous_status": status,
            "dns_status": "deleting",
            "updated_at": now,
        })
        return {"ok": True, "entry": entry}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def complete_delete_ts_domain(domain_id: int) -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """
            DELETE FROM ts_domain_pool
            WHERE id=? AND status='deleting' AND assigned_server_id=''
            """,
            (int(domain_id),),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def fail_delete_ts_domain(domain_id: int, error: str) -> bool:
    conn = _get_conn()
    try:
        cursor = conn.execute(
            """
            UPDATE ts_domain_pool
            SET status='error', dns_status='error', dns_error=?, updated_at=?
            WHERE id=? AND status='deleting' AND assigned_server_id=''
            """,
            (
                (error or "domain deletion failed").strip(),
                datetime.now(timezone.utc).isoformat(),
                int(domain_id),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def sync_node_states_to_db(states: list[dict]) -> int:
    """????????????????????"""
    if not states:
        return 0
    conn = _get_conn()
    try:
        has_v2 = _has_v2_schema(conn)
        count = 0
        for s in states:
            sid = s["server_id"]
            status = s.get("status", "offline")
            current_ip = s.get("current_ip", "") or ""
            occupied_by = s.get("occupied_by", "") or ""
            occupied_at = s.get("occupied_at", "") or ""
            last_heartbeat = s.get("last_heartbeat", "") or ""
            sync_time = s.get("_sync_time", "") or last_heartbeat
            if has_v2:
                _ensure_v2_node_record(conn, sid)
                _upsert_node_runtime(conn, sid, status, current_ip, last_heartbeat, occupied_by, occupied_at, sync_time)
                conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (sync_time, sid))
            else:
                conn.execute(
                    "UPDATE node_requests SET status=?, current_ip=?, occupied_by=?, occupied_at=? WHERE server_id = ?",
                    (status, current_ip, occupied_by, occupied_at, sid),
                )
                conn.execute("UPDATE brokers SET status=?, last_heartbeat=? WHERE name = ?", (status, last_heartbeat, sid))
            count += 1
        conn.commit()
        if count > 0:
            log.info(f"[DB Sync] synced {count} node states to database")
        return count
    except Exception as e:
        log.error(f"sync_node_states_to_db failed: {e}")
        return 0
    finally:
        conn.close()


def delete_node(server_id: str) -> bool:
    """??????????"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            cursor = conn.execute("DELETE FROM nodes WHERE server_id = ?", (server_id,))
            conn.execute("DELETE FROM node_registration_requests_v2 WHERE server_id = ?", (server_id,))
        else:
            conn.execute("DELETE FROM brokers WHERE name = ?", (server_id,))
            cursor = conn.execute("DELETE FROM node_requests WHERE server_id = ?", (server_id,))
        conn.commit()
        ok = cursor.rowcount > 0
        if ok:
            log.info(f"Node deleted: {server_id}")
        return ok
    except Exception as e:
        log.error(f"delete_node failed: {e}")
        return False
    finally:
        conn.close()


def suspend_node(server_id: str) -> bool:
    """?????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            _ensure_v2_node_record(conn, server_id)
            rt_row = conn.execute("SELECT current_ip, last_heartbeat, occupied_by, occupied_at FROM node_runtime WHERE server_id = ?", (server_id,)).fetchone()
            _upsert_node_runtime(
                conn,
                server_id,
                'suspended',
                (rt_row['current_ip'] if rt_row else '') or '',
                (rt_row['last_heartbeat'] if rt_row else now) or now,
                (rt_row['occupied_by'] if rt_row else '') or '',
                (rt_row['occupied_at'] if rt_row else '') or '',
                now,
            )
            conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (now, server_id))
        else:
            conn.execute("UPDATE node_requests SET status='suspended' WHERE server_id = ? AND status IN ('approved','online')", (server_id,))
            conn.execute("UPDATE brokers SET status='suspended' WHERE name = ?", (server_id,))
        conn.commit()
        log.info(f"Node suspended: {server_id}")
        return True
    except Exception as e:
        log.error(f"suspend_node failed: {e}")
        return False
    finally:
        conn.close()


def resume_node(server_id: str) -> bool:
    """?????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            _ensure_v2_node_record(conn, server_id)
            rt_row = conn.execute("SELECT current_ip, occupied_by, occupied_at FROM node_runtime WHERE server_id = ?", (server_id,)).fetchone()
            _upsert_node_runtime(
                conn,
                server_id,
                'online',
                (rt_row['current_ip'] if rt_row else '') or '',
                now,
                (rt_row['occupied_by'] if rt_row else '') or '',
                (rt_row['occupied_at'] if rt_row else '') or '',
                now,
            )
            conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (now, server_id))
        else:
            conn.execute("UPDATE node_requests SET status='online' WHERE server_id = ? AND status = 'suspended'", (server_id,))
            conn.execute("UPDATE brokers SET status='online', last_heartbeat=? WHERE name = ?", (now, server_id))
        conn.commit()
        log.info(f"Node resumed: {server_id}")
        return True
    except Exception as e:
        log.error(f"resume_node failed: {e}")
        return False
    finally:
        conn.close()


def occupy_node(server_id: str, username: str) -> bool:
    """????????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        if _has_v2_schema(conn):
            _ensure_v2_node_record(conn, server_id)
            rt_row = conn.execute("SELECT status, current_ip, last_heartbeat FROM node_runtime WHERE server_id = ?", (server_id,)).fetchone()
            rt = dict(rt_row) if rt_row else {}
            _upsert_node_runtime(
                conn,
                server_id,
                rt.get('status', 'approved') or 'approved',
                rt.get('current_ip', '') or '',
                rt.get('last_heartbeat', '') or '',
                username,
                now,
                now,
            )
            conn.execute("UPDATE nodes SET updated_at = ? WHERE server_id = ?", (now, server_id))
        else:
            conn.execute("UPDATE node_requests SET occupied_by=?, occupied_at=? WHERE server_id = ? AND status IN ('online', 'approved')", (username, now, server_id))
        conn.commit()
        log.info(f"Node {server_id} occupied by account '{username}'")
        return True
    except Exception as e:
        log.error(f"occupy_node failed: {e}")
        return False
    finally:
        conn.close()


def release_node(server_id: str) -> bool:
    """?????????"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            _ensure_v2_node_record(conn, server_id)
            rt_row = conn.execute("SELECT status, current_ip, last_heartbeat FROM node_runtime WHERE server_id = ?", (server_id,)).fetchone()
            rt = dict(rt_row) if rt_row else {}
            _upsert_node_runtime(
                conn,
                server_id,
                rt.get('status', 'approved') or 'approved',
                rt.get('current_ip', '') or '',
                rt.get('last_heartbeat', '') or '',
                '',
                '',
                datetime.now(timezone.utc).isoformat(),
            )
        else:
            conn.execute("UPDATE node_requests SET occupied_by='', occupied_at='' WHERE server_id = ?", (server_id,))
        conn.commit()
        log.info(f"Node {server_id} released (occupation cleared)")
        return True
    except Exception as e:
        log.error(f"release_node failed: {e}")
        return False
    finally:
        conn.close()


def get_occupation_info(server_id: str) -> dict | None:
    """?????????"""
    conn = _get_conn()
    try:
        if _has_v2_schema(conn):
            row = conn.execute("SELECT occupied_by, occupied_at FROM node_runtime WHERE server_id = ?", (server_id,)).fetchone()
            if row and dict(row).get('occupied_by'):
                return dict(row)
            return None
        row = conn.execute("SELECT occupied_by, occupied_at FROM node_requests WHERE server_id = ?", (server_id,)).fetchone()
        if row and dict(row).get("occupied_by"):
            return dict(row)
        return None
    finally:
        conn.close()

def create_account(username: str, password: str, se_address: str = "",
                   broker_tag: str = "", description: str = "",
                   role: str = "trader") -> dict | None:
    """Create a new account."""
    role = (role or "trader").strip().lower()
    if role not in ("trader", "admin"):
        log.warning(f"Account create failed: unsupported role '{role}'")
        return None

    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        pw_hash = _sha256(password)
        normalized_broker_tag = (broker_tag or '').strip()
        brokers_json = _build_allowed_brokers_json(normalized_broker_tag)
        conn.execute(
            """
            INSERT INTO accounts (
                username, password_hash, role, status,
                broker_tag, allowed_brokers, trade_server_address, ts_address,
                description, created_at, updated_at
            )
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (username, pw_hash, role, normalized_broker_tag, brokers_json, se_address, se_address, description, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM accounts WHERE id = last_insert_rowid()").fetchone()
        log.info(f"Account created: {username} role={role} (se={se_address}, broker={broker_tag})")
        return _build_account_compat(row)
    except sqlite3.IntegrityError:
        log.warning(f"Account create failed: username '{username}' already exists")
        return None
    except Exception as e:
        log.error(f"create_account failed: {e}")
        return None
    finally:
        conn.close()


def get_all_accounts() -> list[dict]:
    """Get all accounts."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, username, role, status, broker_tag, allowed_brokers,
                   trade_server_address, description, created_at, updated_at
            FROM accounts
            ORDER BY id DESC
            """
        ).fetchall()
        return [_build_account_compat(row) for row in rows]
    finally:
        conn.close()


def delete_account(account_id: int) -> bool:
    """?????super_admin ??????"""
    conn = _get_conn()
    try:
        cursor = conn.execute("DELETE FROM accounts WHERE id = ? AND role <> 'super_admin'", (account_id,))
        conn.commit()
        ok = cursor.rowcount > 0
        if ok:
            log.info(f"Account deleted: id={account_id}")
        return ok
    except Exception as e:
        log.error(f"delete_account failed: {e}")
        return False
    finally:
        conn.close()


def suspend_account(account_id: int) -> bool:
    """?????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "UPDATE accounts SET status='disabled', updated_at=? WHERE id = ? AND status != 'disabled' AND role <> 'super_admin'",
            (now, account_id),
        )
        conn.commit()
        ok = cursor.rowcount > 0
        if ok:
            log.info(f"Account suspended: id={account_id}")
        return ok
    except Exception as e:
        log.error(f"suspend_account failed: {e}")
        return False
    finally:
        conn.close()


def resume_account(account_id: int) -> bool:
    """?????????"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "UPDATE accounts SET status='active', updated_at=? WHERE id = ? AND status = 'disabled'",
            (now, account_id),
        )
        conn.commit()
        ok = cursor.rowcount > 0
        if ok:
            log.info(f"Account resumed: id={account_id}")
        return ok
    except Exception as e:
        log.error(f"resume_account failed: {e}")
        return False
    finally:
        conn.close()


def get_account_by_id(account_id: int) -> dict | None:
    """Get account details by id."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, role, status, broker_tag, allowed_brokers, trade_server_address, description, created_at, updated_at FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        return _build_account_compat(row)
    finally:
        conn.close()


def get_account_by_username(username: str) -> dict | None:
    """Get account details by username."""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, username, role, status, broker_tag, allowed_brokers, trade_server_address, description, created_at, updated_at FROM accounts WHERE username = ?",
            (username,),
        ).fetchone()
        return _build_account_compat(row)
    finally:
        conn.close()

def rename_super_admin_username(account_id: int, new_username: str) -> tuple[bool, str]:
    """重命名超级管理员用户名（仅允许超级管理员本人）。"""
    new_username = (new_username or "").strip()
    if not new_username:
        return False, "用户名不能为空"

    conn = _get_conn()
    try:
        target = conn.execute(
            "SELECT id, role FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not target:
            return False, "账户不存在"
        if target["role"] != "super_admin":
            return False, "仅超级管理员支持此操作"

        exists = conn.execute(
            "SELECT id FROM accounts WHERE username = ? AND id <> ?",
            (new_username, account_id),
        ).fetchone()
        if exists:
            return False, "用户名已存在"

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE accounts SET username=?, updated_at=? WHERE id=?",
            (new_username, now, account_id),
        )
        conn.commit()
        return True, "ok"
    except Exception as e:
        log.error(f"rename_super_admin_username failed: {e}")
        return False, "更新失败"
    finally:
        conn.close()



def update_super_admin_password(account_id: int, current_password: str, new_password: str) -> tuple[bool, str]:
    """超级管理员修改自己的密码（需校验旧密码）。"""
    current_password = (current_password or "").strip()
    new_password = (new_password or "").strip()

    if not current_password:
        return False, "当前密码不能为空"
    if not new_password:
        return False, "新密码不能为空"
    if len(new_password) < 6:
        return False, "新密码长度至少 6 位"
    if current_password == new_password:
        return False, "新密码不能与当前密码相同"

    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT id, role, password_hash FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if not row:
            return False, "账户不存在"
        if row["role"] != "super_admin":
            return False, "仅超级管理员支持此操作"

        if row["password_hash"] != _sha256(current_password):
            return False, "当前密码错误"

        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE accounts SET password_hash=?, updated_at=? WHERE id=?",
            (_sha256(new_password), now, account_id),
        )
        conn.commit()
        return True, "ok"
    except Exception as e:
        log.error(f"update_super_admin_password failed: {e}")
        return False, "更新失败"
    finally:
        conn.close()



def update_account(account_id: int, se_address: str = "", broker_tag: str = "",
                   description: str = "", password: str = "") -> bool:
    """Update account fields."""
    import hashlib

    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        normalized_broker_tag = (broker_tag or '').strip()
        brokers_json = _build_allowed_brokers_json(normalized_broker_tag)

        if password:
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
            cursor = conn.execute(
                """
                UPDATE accounts SET trade_server_address=?, ts_address=?, broker_tag=?, allowed_brokers=?,
                    description=?, password_hash=?, updated_at=?
                WHERE id = ?
                """,
                (se_address, se_address, normalized_broker_tag, brokers_json, description, pw_hash, now, account_id),
            )
        else:
            cursor = conn.execute(
                """
                UPDATE accounts SET trade_server_address=?, ts_address=?, broker_tag=?,
                    allowed_brokers=?, description=?, updated_at=?
                WHERE id = ?
                """,
                (se_address, se_address, normalized_broker_tag, brokers_json, description, now, account_id),
            )
        conn.commit()
        ok = cursor.rowcount > 0
        if ok:
            log.info(f"Account updated: id={account_id} (se={se_address}, broker={broker_tag})")
        return ok
    except Exception as e:
        log.error(f"update_account failed: {e}")
        return False
    finally:
        conn.close()

def record_audit_log(username: str, action: str, resource: str, detail: str, ip: str = "") -> None:
    """Record one SM operation event for dashboard and audit views."""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_log (username, action, resource, detail, ip_address, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                username or "system",
                action or "UNKNOWN",
                resource or "system",
                detail or "",
                ip or "",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except Exception as e:
        log.warning(f"record_audit_log failed: {e}")
    finally:
        conn.close()


def _audit_log(username: str, action: str, resource: str, detail: str, ip: str = ""):
    """Backward-compatible audit log wrapper."""
    record_audit_log(username, action, resource, detail, ip)


def get_audit_logs(limit: int = 10, days: int = 7, offset: int = 0) -> list[dict]:
    """Return recent audit events, newest first."""
    safe_limit = max(1, min(int(limit or 10), 500))
    safe_offset = max(0, int(offset or 0))
    safe_days = max(1, min(int(days or 7), 3650))
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=safe_days)).isoformat()
    conn = _get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, username, action, resource, detail, ip_address, created_at
            FROM audit_log
            WHERE created_at >= ?
            ORDER BY created_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (cutoff, safe_limit, safe_offset),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_audit_logs(days: int = 7) -> int:
    """Count audit events within the recent days window."""
    safe_days = max(1, min(int(days or 7), 3650))
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=safe_days)).isoformat()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS total FROM audit_log WHERE created_at >= ?",
            (cutoff,),
        ).fetchone()
        return int(row["total"] or 0) if row else 0
    finally:
        conn.close()


def cleanup_audit_logs(retention_days: int | None = None) -> int:
    """Delete audit events older than retention_days. Defaults to 30 days."""
    raw_days = retention_days
    if raw_days is None:
        raw_days = os.environ.get("SM_AUDIT_LOG_RETENTION_DAYS", "30")
    try:
        safe_days = max(1, int(raw_days or 30))
    except (TypeError, ValueError):
        safe_days = 30

    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=safe_days)).isoformat()
    conn = _get_conn()
    try:
        cursor = conn.execute("DELETE FROM audit_log WHERE created_at < ?", (cutoff,))
        conn.commit()
        deleted = cursor.rowcount if cursor.rowcount is not None else 0
        if deleted:
            log.info(f"Cleaned {deleted} audit log row(s), retention_days={safe_days}")
        return deleted
    except Exception as e:
        log.warning(f"cleanup_audit_logs failed: {e}")
        return 0
    finally:
        conn.close()
