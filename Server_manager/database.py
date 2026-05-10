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
                se_address TEXT DEFAULT '',
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
        # 兼容已有数据库：添加 se_address 列（如果不存在）
        try:
            conn.execute("SELECT se_address FROM accounts LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE accounts ADD COLUMN se_address TEXT DEFAULT ''")
            log.info("Added se_address column to accounts table (migration)")

        # 兼容已有数据库：添加 description 列（如果不存在）
        try:
            conn.execute("SELECT description FROM accounts LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE accounts ADD COLUMN description TEXT DEFAULT ''")
            log.info("Added description column to accounts table (migration)")

        # 兼容已有数据库：添加节点占用字段（如果不存在）
        try:
            conn.execute("SELECT occupied_by FROM node_requests LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE node_requests ADD COLUMN occupied_by TEXT DEFAULT ''")
            conn.execute("ALTER TABLE node_requests ADD COLUMN occupied_at TEXT DEFAULT ''")
            log.info("Added occupation columns to node_requests table (migration)")

        conn.commit()
        log.info(f"Database initialized: {_DB_PATH}")
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
            "SELECT id, username, role, status, allowed_brokers, se_address FROM accounts "
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


def check_se_online(se_address: str) -> dict:
    """
    检查指定 SE 地址是否存在对应的在线节点

    Args:
        se_address: SE 地址，如 "127.0.0.1:8900" 或 "127.0.0.1"

    Returns:
        dict: {"online": bool, "node_name": str, "server_id": str, "match_field": str,
               "occupied_by": str, "occupied_at": str}
              match_field 说明匹配方式: "current_ip"(精确IP匹配) / "host"(host字段匹配)
              occupied_by 非空表示该节点已被占用
        如果没有匹配的节点或节点不在线: {"online": False, ...}
    """
    conn = _get_conn()
    try:
        if not se_address:
            return {"online": False, "reason": "未配置 SE 地址"}

        # 解析地址（支持 "ip:port" 或纯 "ip" 格式）
        addr_part = se_address.split(":")[0] if ":" in se_address else se_address
        addr_part = addr_part.strip()

        # 1. 精确匹配 current_ip 字段（心跳上报的实际 IP）
        row = conn.execute("""
            SELECT nr.server_id, nr.node_name, nr.current_ip, nr.host,
                   b.status AS broker_status, nr.status AS req_status,
                   nr.occupied_by, nr.occupied_at
            FROM node_requests nr
            LEFT JOIN brokers b ON nr.server_id = b.name
            WHERE (nr.current_ip = ? OR nr.current_ip LIKE ?)
              AND nr.status IN ('online', 'approved')
            LIMIT 1
        """, (addr_part, f"{addr_part}%")).fetchone()

        if row and dict(row).get("req_status") == "online":
            r = dict(row)
            return {"online": True, "node_name": r["node_name"], "server_id": r["server_id"],
                    "match_field": "current_ip", "address": se_address,
                    "occupied_by": r.get("occupied_by", "") or "",
                    "occupied_at": r.get("occupied_at", "") or ""}

        # 2. 回退到 host 字段匹配
        row2 = conn.execute("""
            SELECT nr.server_id, nr.node_name, nr.current_ip, nr.host,
                   b.status AS broker_status, nr.status AS req_status,
                   nr.occupied_by, nr.occupied_at
            FROM node_requests nr
            LEFT JOIN brokers b ON nr.server_id = b.name
            WHERE nr.host = ?
              AND nr.status IN ('online', 'approved')
            LIMIT 1
        """, (addr_part,)).fetchone()

        if row2 and dict(row2).get("req_status") == "online":
            r = dict(row2)
            return {"online": True, "node_name": r["node_name"], "server_id": r["server_id"],
                    "match_field": "host", "address": se_address,
                    "occupied_by": r.get("occupied_by", "") or "",
                    "occupied_at": r.get("occupied_at", "") or ""}

        return {"online": False, "reason": f"未找到与地址 '{se_address}' 匹配的在线子服务器",
                "address": se_address, "occupied_by": "", "occupied_at": ""}
    except Exception as e:
        log.error(f"check_se_online failed: {e}")
        return {"online": False, "reason": f"查询异常: {e}", "address": se_address,
                "occupied_by": "", "occupied_at": ""}
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
        
    注意: 允许 offline/suspended 状态的节点通过验证，
    以便它们恢复上线后能正常发送心跳将状态更新回 online。
    """
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM node_requests WHERE token = ? AND status IN ('approved', 'online', 'offline', 'suspended')",
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
    """
    更新已批准节点的心跳时间和 IP

    注意：只允许将状态恢复为 online 的场景：
      - online（保持在线）
      - offline（从离线恢复）
      - approved（首次心跳激活）
    不允许覆盖 suspended/occupied 等管理员主动设置的状态，
    避免心跳将暂停的节点强行改回在线。
    """
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        # 同步更新 node_requests 和 brokers 两张表（带状态守卫）
        conn.execute("""
            UPDATE node_requests SET current_ip=?, status='online'
            WHERE server_id = ? AND status IN ('online', 'offline', 'approved')
        """, (current_ip, server_id))
        conn.execute("""
            UPDATE brokers SET last_heartbeat=?, status='online'
            WHERE name = ? AND status IN ('online', 'offline', 'approved')
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
# 此值用于后台自动巡检和前端刷新展示，较长可避免网络抖动导致误判
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

        # 批量标记为离线 + 自动释放占用
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            sid = row["server_id"]
            conn.execute("""
                UPDATE node_requests SET status='offline', occupied_by='', occupied_at=''
                WHERE server_id = ?
            """, (sid,))
            conn.execute("""
                UPDATE brokers SET status='offline' WHERE name = ?
            """, (sid,))
            log.info(
                f"Node marked OFFLINE & RELEASED: {sid} ({row['node_name']}) "
                f"— last_heartbeat={row['last_heartbeat'] or '(never)'}, "
                f"cutoff={cutoff_iso} [occupation auto-cleared]"
            )

        conn.commit()
        return count
    except Exception as e:
        log.error(f"check_offline_nodes failed: {e}")
        return 0
    finally:
        conn.close()


def force_check_all_nodes() -> dict:
    """
    查询所有节点的最新状态（供管理员手动刷新使用，只读不写库）

    基于数据库中的现有状态和心跳时间戳，纯计算每个节点的展示状态。
    不修改任何数据库记录，避免"刷新导致在线/离线反复切换"的问题。

    状态判定逻辑：
      occupied   — 节点被账户占用（最高优先级）
      suspended  — 管理员手动暂停
      offline    — 数据库中标记为离线，或 心跳超时超过阈值
      online     — 有活跃心跳且状态正常

    四种状态的优先级：occupied > suspended > offline > online/approved

    Returns:
        dict: {
            "checked": int,
            "online": int, "offline": int, "occupied": int, "suspended": int,
            "nodes": [
                {所有 get_all_nodes 字段 + "real_status": 计算后的最终展示状态}
            ]
        }
    """
    conn = _get_conn()
    try:
        from datetime import timedelta
        now_utc = datetime.now(timezone.utc)

        # 使用后台巡检相同的宽松阈值做展示判断（仅用于前端显示，不写库）
        _display_timeout = HEARTBEAT_TIMEOUT_SECONDS
        cutoff = now_utc - timedelta(seconds=_display_timeout)
        cutoff_iso = cutoff.isoformat()

        # ── 1) 查询完整节点数据 ──

        rows = conn.execute("""
            SELECT nr.server_id, nr.node_name, nr.region, nr.host,
                   nr.capabilities, nr.status AS req_status, nr.current_ip,
                   nr.token, b.status AS broker_status, b.last_heartbeat,
                   nr.occupied_by, nr.occupied_at, nr.description
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

        # ── 2) 统一计算每个节点的真实最终展示状态（纯计算，不写库）──
        nodes = []
        online_count = 0
        offline_count = 0
        suspended_count = 0
        occupied_count = 0

        for row in rows:
            n = dict(row)
            req_status = n.get("req_status", "")
            occ_by = (n.get("occupied_by") or "").strip()
            last_hb = n.get("last_heartbeat") or ""

            # 四种状态优先级：suspended(管理操作) > offline(含心跳超时) > occupied(需在线) > online/approved
            # 注意：occupied 前提是节点必须实际在线（有心跳），离线节点即使有占用记录也显示离线
            is_alive = bool(last_hb and last_hb >= cutoff_iso)  # 心跳活跃

            if req_status == "suspended":
                real_status = "suspended"
                suspended_count += 1
            elif not is_alive or req_status == "offline":
                # 节点已离线或心跳超时 → 显示离线（不管是否有历史占用记录）
                real_status = "offline"
                offline_count += 1
            elif occ_by:
                # 节点在线且有占用者 → 显示已占用
                real_status = "occupied"
                occupied_count += 1
            else:  # 'online' 或 'approved' 且心跳活跃且未被占用
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
        return {"checked": 0, "marked_offline": 0, "online": 0, "offline": 0,
                "occupied": 0, "suspended": 0, "nodes": [], "error": str(e)}
    finally:
        conn.close()


def get_all_nodes() -> list[dict]:
    """获取所有已批准节点列表（含在线/暂停/离线状态）—— 兼容旧接口，实际应使用 node_state.manager"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT nr.server_id, nr.node_name, nr.region, nr.host,
                   nr.capabilities, nr.status AS req_status, nr.current_ip,
                   nr.token, b.status AS broker_status, b.last_heartbeat,
                   nr.occupied_by, nr.occupied_at, nr.description
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


def get_approved_nodes_for_memory_load() -> list[dict]:
    """
    加载所有已批准节点的配置数据（供 node_state 启动时初始化内存用）。

    返回每个节点的完整配置字段 + DB 中存储的状态快照。
    实时状态以返回数据为准，启动后由心跳覆盖。
    """
    return get_all_nodes()


def sync_node_states_to_db(states: list[dict]) -> int:
    """
    将内存中的实时状态批量回写到数据库（用于定期持久化）。

    Args:
        states: node_state.manager.prepare_db_sync_data() 的输出

    Returns:
        更新的行数
    """
    if not states:
        return 0
    conn = _get_conn()
    try:
        count = 0
        for s in states:
            sid = s["server_id"]
            # 更新 node_requests 实时字段
            conn.execute("""
                UPDATE node_requests SET status=?, current_ip=?,
                                      occupied_by=?, occupied_at=?
                WHERE server_id = ?
            """, (s["status"], s["current_ip"],
                  s["occupied_by"], s["occupied_at"], sid))
            # 更新 brokers 实时字段
            conn.execute("""
                UPDATE brokers SET status=?, last_heartbeat=?
                WHERE name = ?
            """, (s["status"], s["last_heartbeat"], sid))
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
        # 同时更新 last_heartbeat 时间戳，避免后台巡检在90秒内将其标回离线
        conn.execute("""
            UPDATE node_requests SET status='online' WHERE server_id = ? AND status = 'suspended'
        """, (server_id,))
        conn.execute("""
            UPDATE brokers SET status='online', last_heartbeat=? WHERE name = ?
        """, (now, server_id))
        conn.commit()
        log.info(f"Node resumed: {server_id}")
        return True
    except Exception as e:
        log.error(f"resume_node failed: {e}")
        return False
    finally:
        conn.close()


# ── 节点占用管理（occupation）──────────────────────────────────────


def occupy_node(server_id: str, username: str) -> bool:
    """标记节点被指定账户占用（独占锁定）"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            UPDATE node_requests SET occupied_by=?, occupied_at=?
            WHERE server_id = ? AND status IN ('online', 'approved')
        """, (username, now, server_id))
        # 同步到 brokers 表
        conn.execute("""
            UPDATE brokers SET last_heartbeat=last_heartbeat
            WHERE name = ?
        """, (server_id,))
        conn.commit()
        log.info(f"Node {server_id} occupied by account '{username}'")
        return True
    except Exception as e:
        log.error(f"occupy_node failed: {e}")
        return False
    finally:
        conn.close()


def release_node(server_id: str) -> bool:
    """释放节点的占用状态"""
    conn = _get_conn()
    try:
        conn.execute("""
            UPDATE node_requests SET occupied_by='', occupied_at=''
            WHERE server_id = ?
        """, (server_id,))
        conn.commit()
        log.info(f"Node {server_id} released (occupation cleared)")
        return True
    except Exception as e:
        log.error(f"release_node failed: {e}")
        return False
    finally:
        conn.close()


def get_occupation_info(server_id: str) -> dict | None:
    """查询节点的占用信息"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT occupied_by, occupied_at FROM node_requests WHERE server_id = ?",
            (server_id,),
        ).fetchone()
        if row and dict(row).get("occupied_by"):
            return dict(row)
        return None
    finally:
        conn.close()


# ── 账户管理（accounts 表）───────────────────────────────────────────

def create_account(username: str, password: str, se_address: str = "",
                   broker_tag: str = "", description: str = "",
                   role: str = "trader") -> dict | None:
    """
    创建新账户

    Args:
        username: 用户名（唯一）
        password: 明文密码（将 SHA256 哈希存储）
        se_address: Server_economic 地址 (如 127.0.0.1:8900)
        broker_tag: 券商标签
        description: 账户描述信息（非必填）
        role: 角色 (trader/admin)

    Returns:
        创建的账户字典，或 None（失败，如用户名已存在）
    """
    import hashlib
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        pw_hash = hashlib.sha256(password.encode()).hexdigest()
        brokers_json = json.dumps([broker_tag] if broker_tag else [])
        conn.execute("""
            INSERT INTO accounts (username, password_hash, role, status,
                                  allowed_brokers, se_address, description, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)
        """, (username, pw_hash, role, brokers_json, se_address, description, now, now))
        conn.commit()
        row = conn.execute("SELECT * FROM accounts WHERE id = last_insert_rowid()").fetchone()
        log.info(f"Account created: {username} (se={se_address}, broker={broker_tag})")
        return dict(row) if row else None
    except sqlite3.IntegrityError:
        log.warning(f"Account create failed: username '{username}' already exists")
        return None
    except Exception as e:
        log.error(f"create_account failed: {e}")
        return None
    finally:
        conn.close()


def get_all_accounts() -> list[dict]:
    """获取所有账户列表"""
    conn = _get_conn()
    try:
        rows = conn.execute("""
            SELECT id, username, role, status, allowed_brokers, se_address,
                   description, created_at, updated_at
            FROM accounts
            ORDER BY id DESC
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_account(account_id: int) -> bool:
    """删除账户"""
    conn = _get_conn()
    try:
        cursor = conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
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
    """暂停账户（状态设为 disabled）"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("""
            UPDATE accounts SET status='disabled', updated_at=?
            WHERE id = ? AND status != 'disabled'
        """, (now, account_id))
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
    """恢复被暂停的账户"""
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute("""
            UPDATE accounts SET status='active', updated_at=?
            WHERE id = ? AND status = 'disabled'
        """, (now, account_id))
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
    """根据 ID 获取单个账户详情（不含密码哈希）"""
    conn = _get_conn()
    try:
        row = conn.execute("""
            SELECT id, username, role, status, allowed_brokers, se_address,
                   description, created_at, updated_at
            FROM accounts WHERE id = ?
        """, (account_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_account(account_id: int, se_address: str = "", broker_tag: str = "",
                   description: str = "", password: str = "") -> bool:
    """
    更新账户信息

    Args:
        account_id: 账户 ID
        se_address: 新的 SE 地址
        broker_tag: 新的券商标签
        description: 新的描述信息
        password: 新密码（为空则不修改）

    Returns:
        是否更新成功
    """
    import hashlib
    conn = _get_conn()
    try:
        now = datetime.now(timezone.utc).isoformat()
        brokers_json = json.dumps([broker_tag] if broker_tag else [])

        if password:
            # 包含密码修改
            pw_hash = hashlib.sha256(password.encode()).hexdigest()
            cursor = conn.execute("""
                UPDATE accounts SET se_address=?, allowed_brokers=?,
                    description=?, password_hash=?, updated_at=?
                WHERE id = ?
            """, (se_address, brokers_json, description, pw_hash, now, account_id))
        else:
            # 不改密码
            cursor = conn.execute("""
                UPDATE accounts SET se_address=?,
                    allowed_brokers=?, description=?, updated_at=?
                WHERE id = ?
            """, (se_address, brokers_json, description, now, account_id))
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


# ── 审计日志 ─────────────────────────────────────────────────────────────

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
