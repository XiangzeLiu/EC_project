"""
SQLite Database Operations
轻量化账户/券商管理数据持久化
"""

import json
import os
import sqlite3
import logging
from datetime import datetime, timezone

log = logging.getLogger("server_manager")

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "server_manager.db")


def _get_conn() -> sqlite3.Connection:
    """获取数据库连接，自动创建目录和表"""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """初始化数据库表结构"""
    conn = _get_conn()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT DEFAULT 'trader',
                status TEXT DEFAULT 'active',
                allowed_brokers TEXT DEFAULT '[]',
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
        """)
        conn.commit()
        log.info(f"Database initialized: {_DB_PATH}")

        # 插入默认管理员账号（如果不存在）
        row = conn.execute(
            "SELECT id FROM accounts WHERE username = ?", ("admin",)
        ).fetchone()
        if not row:
            import hashlib
            pw_hash = hashlib.sha256(b"admin123").hexdigest()
            conn.execute(
                "INSERT INTO accounts (username, password_hash, role, status, created_at) "
                "VALUES (?, ?, 'admin', 'active', ?)",
                ("admin", pw_hash, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            log.info("Default admin account created (username: admin, password: admin123)")
    finally:
        conn.close()


def verify_account(username: str, password: str) -> dict | None:
    """
    验证账号密码

    Returns:
        账户字典 或 None（验证失败）
    """
    conn = _get_conn()
    try:
        import hashlib
        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        row = conn.execute(
            "SELECT id, username, role, status, allowed_brokers FROM accounts "
            "WHERE username = ? AND password_hash = ? AND status = 'active'",
            (username, pw_hash),
        ).fetchone()
        if row:
            # 记录审计日志
            _audit_log(username, "LOGIN", "account", f"Login success for {username}")
            return dict(row)
        return None
    finally:
        conn.close()


def get_broker_list() -> list[dict]:
    """获取所有已注册且在线的券商列表"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT name, broker_type, host, port, status FROM brokers WHERE status != 'deleted'"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def register_broker(name: str, broker_type: str = "tastytrade",
                    host: str = "", port: int = 0, config: dict | None = None) -> bool:
    """注册/更新券商信息"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO brokers (name, broker_type, host, port, config, status, registered_at, created_at)
            VALUES (?, ?, ?, ?, ?, 'online', ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                broker_type=excluded.broker_type,
                host=excluded.host,
                port=excluded.port,
                config=excluded.config,
                status='online',
                last_heartbeat=?
        """, (name, broker_type, host, port, json.dumps(config or {}), now, now, now))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"register_broker failed: {e}")
        return False
    finally:
        conn.close()


def update_broker_heartbeat(name: str):
    """更新券商心跳时间"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE brokers SET last_heartbeat=?, status='online' WHERE name=?",
            (now, name),
        )
        conn.commit()
    finally:
        conn.close()


def _audit_log(username: str, action: str, resource: str, detail: str, ip: str = ""):
    """记录审计日志"""
    conn = _get_conn()
    try:
        conn.execute(
            "INSERT INTO audit_log (username, action, resource, detail, ip_address, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (username, action, resource, detail, ip, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
