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


# ── 节点注册请求管理（node_requests 表）─────────────────────────────────

def create_node_request(request_id: str, node_name: str, region: str = "",
                        host: str = "", capabilities: list | None = None,
                        contact: str = "", description: str = "",
                        expire_hours: int = 24) -> dict | None:
    """
    创建节点注册请求（暂存区）

    Returns:
        创建的记录字典，或 None（失败）
    """
    import secrets
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc)
        from datetime import timedelta
        expire_at = (now + timedelta(hours=expire_hours)).isoformat()
        conn.execute("""
            INSERT INTO node_requests
                (request_id, node_name, region, host, capabilities,
                 contact, description, status, expire_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
        """, (
            request_id, node_name, region, host,
            json.dumps(capabilities or []),
            contact, description, expire_at, now.isoformat(),
        ))
        conn.commit()
        row = conn.execute(
            "SELECT * FROM node_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        log.info(f"Node registration request created: {request_id} ({node_name})")
        return dict(row) if row else None
    except Exception as e:
        log.error(f"create_node_request failed: {e}")
        return None
    finally:
        conn.close()


def get_node_request_by_id(request_id: str) -> dict | None:
    """通过 request_id 查询注册请求"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM node_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_pending_node_requests() -> list[dict]:
    """获取所有待审核的注册请求"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT * FROM node_requests
            WHERE status = 'pending' AND expire_at > ?
            ORDER BY created_at ASC
        """, (datetime.now(timezone.utc).isoformat(),)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def approve_node_request(request_id: str, reviewer: str = "admin") -> dict | None:
    """
    审核通过注册请求：生成 server_id + token，写入正式表(brokers)

    Returns:
        更新后的记录字典（含 server_id 和 token），或 None
    """
    import secrets
    conn = _get_conn()
    try:
        # 先查询原始请求
        req = conn.execute(
            "SELECT * FROM node_requests WHERE request_id = ? AND status = 'pending'",
            (request_id,),
        ).fetchone()
        if not req:
            return None

        now = datetime.now(timezone.utc).isoformat()
        server_id = f"node_{req['node_name']}_{secrets.token_hex(4)}"
        token = secrets.token_urlsafe(32)

        # 1) 更新 node_requests 状态
        conn.execute("""
            UPDATE node_requests SET
                status='approved', server_id=?, token=?,
                reviewed_by=?, reviewed_at=?
            WHERE request_id=?
        """, (server_id, token, reviewer, now, request_id))

        # 2) 写入 brokers 正式表（复用现有表作为已批准节点存储）
        caps_json = json.loads(req['capabilities']) if isinstance(req['capabilities'], str) else (req['capabilities'] or [])
        config_data = {
            "region": req['region'],
            "capabilities": caps_json,
            "contact": req['contact'],
            "description": req['description'],
        }
        conn.execute("""
            INSERT INTO brokers (name, broker_type, host, port, config, status,
                                registered_at, created_at)
            VALUES (?, 'economic', '', 0, ?, 'online', ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                broker_type='economic',
                host=excluded.host,
                port=excluded.port,
                config=excluded.config,
                status='online',
                registered_at=excluded.registered_at
        """, (server_id, json.dumps(config_data), now, now))

        # 同时在 brokers 行中保存 token 用于心跳验证
        # 复用 config 字段存 token（或可扩展字段）
        # 这里用独立方式：token 存在 node_requests 中，心跳时查询

        conn.commit()
        result = dict(conn.execute("SELECT * FROM node_requests WHERE request_id = ?", (request_id,)).fetchone())
        log.info(f"Node request approved: {request_id} → server_id={server_id}")
        return result
    except Exception as e:
        log.error(f"approve_node_request failed: {e}")
        return None
    finally:
        conn.close()


def reject_node_request(request_id: str, reason: str = "", reviewer: str = "admin") -> bool:
    """审核拒绝注册请求"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("""
            UPDATE node_requests SET
                status='rejected', reject_reason=?,
                reviewed_by=?, reviewed_at=?
            WHERE request_id = ? AND status = 'pending'
        """, (reason, reviewer, now, request_id))
        conn.commit()
        ok = cursor.rowcount > 0
        if ok:
            log.info(f"Node request rejected: {request_id} reason={reason}")
        return ok
    finally:
        conn.close()


def cleanup_expired_requests() -> int:
    """清理过期的待审核请求，返回清理数量"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("""
            UPDATE node_requests
            SET status='expired'
            WHERE status = 'pending' AND expire_at <= ?
        """, (now,))
        count = cursor.rowcount
        if count > 0:
            conn.commit()
            log.info(f"Cleaned {count} expired node registration request(s)")
        return count
    finally:
        conn.close()


def verify_node_token(token: str) -> dict | None:
    """
    验证节点的 Bearer Token

    Returns:
        节点信息字典 或 None（无效/过期）
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM node_requests WHERE token = ? AND status IN ('approved', 'online')",
            (token,),
        ).fetchone()
        if row:
            return dict(row)

        # 也检查是否在 brokers 表中有对应记录
        # 通过 server_id 关联
        return None
    finally:
        conn.close()


def update_node_heartbeat(server_id: str, current_ip: str = "") -> bool:
    """更新已批准节点的心跳时间和 IP"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        # 同步更新 node_requests 和 brokers 两张表
        conn.execute("""
            UPDATE node_requests SET current_ip=?, status='online'
            WHERE server_id = ?
        """, (current_ip, server_id))
        conn.execute("""
            UPDATE brokers SET last_heartbeat=?, status='online'
            WHERE name = ?
        """, (now, server_id))
        conn.commit()
        return True
    except Exception as e:
        log.error(f"update_node_heartbeat failed: {e}")
        return False
    finally:
        conn.close()


# ── 心跳超时配置 ────────────────────────────────────────────────────────

# 心跳超时阈值（秒）：超过此时间未收到心跳则判定为离线
# 建议设为心跳间隔的 3 倍（默认间隔30s → 超时90s）
HEARTBEAT_TIMEOUT_SECONDS = 90


def check_offline_nodes(timeout_seconds: int | None = None) -> int:
    """
    检测并标记心跳超时的在线节点为离线

    扫描所有 status='online' 的节点，对比 last_heartbeat 时间戳，
    超过阈值未收到心跳的节点将被标记为 offline。

    Returns:
        被标记为离线的节点数量
    """
    timeout = timeout_seconds or HEARTBEAT_TIMEOUT_SECONDS
    conn = _get_conn()
    try:
        from datetime import timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=timeout)
        cutoff_iso = cutoff.isoformat()

        # 查找心跳超时但状态仍为 online 的节点
        rows = conn.execute("""
            SELECT b.name AS server_id, nr.node_name, b.last_heartbeat
            FROM brokers b
            JOIN node_requests nr ON nr.server_id = b.name
            WHERE b.status = 'online'
              AND (b.last_heartbeat IS NULL OR b.last_heartbeat < ?)
              AND nr.status = 'online'
        """, (cutoff_iso,)).fetchall()

        count = len(rows)
        if count == 0:
            return 0

        # 批量标记为离线
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            sid = row["server_id"]
            conn.execute("""
                UPDATE node_requests SET status='offline' WHERE server_id = ?
            """, (sid,))
            conn.execute("""
                UPDATE brokers SET status='offline' WHERE name = ?
            """, (sid,))
            log.info(
                f"Node marked OFFLINE: {sid} ({row['node_name']}) "
                f"— last_heartbeat={row['last_heartbeat'] or '(never)'}, "
                f"cutoff={cutoff_iso}"
            )

        conn.commit()
        return count
    except Exception as e:
        log.error(f"check_offline_nodes failed: {e}")
        return 0
    finally:
        conn.close()


def get_all_nodes() -> list[dict]:
    """获取所有已批准节点列表（含在线/暂停/离线状态）"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT nr.server_id, nr.node_name, nr.region, nr.host,
                   nr.capabilities, nr.status AS req_status, nr.current_ip,
                   nr.token, b.status AS broker_status, b.last_heartbeat
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
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_node(server_id: str) -> bool:
    """彻底删除已批准节点（同时清理 node_requests 和 brokers）"""
    conn = _get_conn()
    try:
        # 先删 brokers 表记录
        conn.execute("DELETE FROM brokers WHERE name = ?", (server_id,))
        # 再删 node_requests 记录
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
    """暂停节点（标记为 suspended，心跳验证将拒绝）"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE node_requests SET status='suspended' WHERE server_id = ? AND status IN ('approved','online')
        """, (server_id,))
        conn.execute("""
            UPDATE brokers SET status='suspended' WHERE name = ?
        """, (server_id,))
        conn.commit()
        log.info(f"Node suspended: {server_id}")
        return True
    except Exception as e:
        log.error(f"suspend_node failed: {e}")
        return False
    finally:
        conn.close()


def resume_node(server_id: str) -> bool:
    """恢复被暂停的节点"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        # 恢复为 online 状态（与前端 renderNodes 的 st==='online' 判断一致，
        # 这样恢复后卡片立即显示绿色"运行中"底色）
        # 注意：若节点实际未发心跳，last_heartbeat 可能为空，管理员可通过该字段区分
        conn.execute("""
            UPDATE node_requests SET status='online' WHERE server_id = ? AND status = 'suspended'
        """, (server_id,))
        conn.execute("""
            UPDATE brokers SET status='online' WHERE name = ?
        """, (server_id,))
        conn.commit()
        log.info(f"Node resumed: {server_id}")
        return True
    except Exception as e:
        log.error(f"resume_node failed: {e}")
        return False
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
