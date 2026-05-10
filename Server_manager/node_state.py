"""
Node Runtime State Manager (内存节点状态管理)

设计原则:
  - 实时信号（心跳/在线/占用）只存内存，不写 SQLite
  - 配置数据（server_id/token/node_name 等）留在数据库
  - 启动时从 DB 加载初始状态到内存
  - 定期将关键状态同步回 DB（用于崩溃恢复）

状态优先级: suspended > offline > occupied > online/approved
"""

import time
import logging
from dataclasses import dataclass, field, asdict
from typing import Optional

log = logging.getLogger("node_state")

# ── 常量 ────────────────────────────────────────────────────────────────────

HEARTBEAT_TIMEOUT = 90  # 默认心跳超时秒数（空闲节点）
OCCUPIED_HEARTBEAT_TIMEOUT = 15  # 占用状态下心跳超时秒数（快速检测掉线）
PROBE_ADVANCE_SECONDS = 8  # 提前多少秒开始主动探测（在超时前 N 秒）


@dataclass
class NodeState:
    """单个节点的实时运行状态（纯内存，不持久化到 DB）"""
    server_id: str = ""

    # ── 实时字段（每次心跳更新）──
    status: str = "approved"          # online / offline / suspended / approved
    last_heartbeat: float = 0.0       # time.monotonic() 时间戳
    current_ip: str = ""

    # ── 占用字段（Client 操作时更新）──
    occupied_by: str = ""             # 空字符串表示未被占用
    occupied_at: float = 0.0

    # ── 主动探活字段（SM 巡检时使用）──
    _probing: bool = False            # 是否正在主动探活此节点
    _last_probe_time: float = 0.0     # 上次主动探测时间

    # ── 合并用的配置字段（启动时从 DB 加载，只读）──
    # 这些字段来自 node_requests 表的配置数据
    _node_name: str = ""
    _region: str = ""
    _host: str = ""
    _capabilities: str = ""
    _description: str = ""
    _token: str = ""
    _created_at: str = ""

    @property
    def is_alive(self) -> bool:
        """心跳是否活跃（占用状态下使用更短的超时阈值以快速检测掉线）"""
        if self.last_heartbeat <= 0:
            return False
        # 被占用的节点使用短超时（15秒），空闲节点使用默认超时（90秒）
        timeout = OCCUPIED_HEARTBEAT_TIMEOUT if self.occupied_by else HEARTBEAT_TIMEOUT
        return (time.monotonic() - self.last_heartbeat) < timeout

    @property
    def heartbeat_timeout(self) -> float:
        """返回当前适用的超时阈值"""
        return OCCUPIED_HEARTBEAT_TIMEOUT if self.occupied_by else HEARTBEAT_TIMEOUT

    @property
    def is_online(self) -> bool:
        """节点是否在线（status=online 且心跳活跃）"""
        return self.status == "online" and self.is_alive

    def to_dict(self) -> dict:
        """转为字典（用于 API 响应），合并实时状态 + 配置信息"""
        d = asdict(self)
        # 去掉内部字段前缀
        result = {}
        for k, v in d.items():
            if k.startswith("_"):
                result[k[1:]] = v  # _node_name → node_name
            else:
                result[k] = v
        # 添加 last_heartbeat 的 ISO 格式（前端展示用）
        if self.last_heartbeat > 0:
            from datetime import datetime, timezone
            result["last_heartbeat"] = datetime.fromtimestamp(
                self.last_heartbeat, tz=timezone.utc
            ).isoformat()
        else:
            result["last_heartbeat"] = ""
        return result


# ── 状态管理器 ──────────────────────────────────────────────────────────────


class NodeStateManager:
    """
    全局节点状态管理器（进程级单例）

    内存中维护 {server_id → NodeState} 映射，
    所有实时操作（心跳/占用/暂停等）直接操作内存。
    """

    def __init__(self):
        self._states: dict[str, NodeState] = {}

    # ── 生命周期 ───────────────────────────────────────────────────────

    def register(self, config_row: dict) -> None:
        """
        从数据库行注册/加载一个节点到内存。

        Args:
            config_row: 包含 server_id, node_name, token 等配置字段的字典
                        （来自 node_requests + brokers JOIN 查询）
        """
        sid = config_row.get("server_id", "")
        if not sid:
            return

        state = NodeState(server_id=sid)

        # 从 DB 行加载配置字段（只读）
        state._node_name = config_row.get("node_name", "") or ""
        state._region = config_row.get("region", "") or ""
        state._host = config_row.get("host", "") or ""
        state._capabilities = config_row.get("capabilities", "") or ""
        state._description = config_row.get("description", "") or ""
        state._token = config_row.get("token", "") or ""
        state._created_at = config_row.get("created_at", "") or ""

        # 从 DB 恢复实时状态的"最佳猜测"
        db_status = config_row.get("req_status", "") or config_row.get("broker_status", "") or "approved"
        if db_status in ("online", "offline", "suspended", "approved"):
            state.status = db_status

        # 如果有占用记录且状态是 online，恢复它（但会在巡检时重新判断）
        occ_by = (config_row.get("occupied_by") or "").strip()
        if occ_by and db_status not in ("offline",):
            state.occupied_by = occ_by
            state.occupied_at = 0.0  # 时间戳不可靠，清零让系统重新判定

        # 解析 last_heartbeat
        hb_str = config_row.get("last_heartbeat", "") or ""
        if hb_str:
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(hb_str)
                state.last_heartbeat = dt.timestamp()
            except (ValueError, TypeError):
                state.last_heartbeat = 0.0

        # ★ 关键修复：从 DB 加载后，立即验证节点是否真的在线
        # 如果 DB 中标记为 online，但 last_heartbeat 已经过期，
        # 则直接标记为离线，防止展示错误的在线/占用状态
        if state.status == "online" and state.last_heartbeat > 0:
            # 使用较短的判定阈值（默认超时的 1/3 作为安全边界），
            # 因为 DB 同步间隔最长 5 分钟，如果 heartbeat 比这更老肯定离线
            hb_age = time.monotonic() - state.last_heartbeat
            safe_threshold = HEARTBEAT_TIMEOUT / 3  # 30 秒安全阈值
            if hb_age > safe_threshold:
                old_occ = state.occupied_by
                state.status = "offline"
                state.occupied_by = ""
                state.occupied_at = 0.0
                log.info(
                    f"[NodeState] DB load correction: {sid} marked OFFLINE "
                    f"(heartbeat age={hb_age:.0f}s > {safe_threshold:.0f}s threshold, "
                    f"was occupied by '{old_occ}')"
                )

        self._states[sid] = state
        log.debug(f"[NodeState] registered: {sid} status={state.status} hb={state.last_heartbeat}")

    def unregister(self, server_id: str) -> None:
        """从内存移除节点（删除操作调用）"""
        if server_id in self._states:
            del self._states[server_id]
            log.info(f"[NodeState] unregistered: {server_id}")

    def load_from_db_rows(self, rows: list[dict]) -> int:
        """批量从数据库行加载所有已批准节点。返回加载数量。"""
        count = 0
        for row in rows:
            row_dict = dict(row)
            sid = row_dict.get("server_id", "")
            if sid:
                self.register(row_dict)
                count += 1
        log.info(f"[NodeState] loaded {count} nodes from DB into memory")
        return count

    # ── 心跳操作 ───────────────────────────────────────────────────────

    def update_heartbeat(self, server_id: str, current_ip: str = "") -> tuple[bool, str]:
        """
        更新节点心跳。

        状态守卫：只允许 active 节点恢复为 online。
        suspended 节点的心跳不会改变其暂停状态。
        
        Returns:
            (成功与否, 错误消息)
        """
        state = self._states.get(server_id)
        if not state:
            return False, f"node '{server_id}' not found in memory"

        # 状态守卫：suspended 不被心跳覆盖
        if state.status == "suspended":
            # 只更新 IP 和心跳时间（用于诊断），不改状态
            state.current_ip = current_ip
            state.last_heartbeat = time.monotonic()
            return True, "ok (suspended, heartbeat noted)"

        # offline / approved / online → 都可恢复为 online
        now = time.monotonic()
        state.status = "online"
        state.last_heartbeat = now
        state.current_ip = current_ip
        return True, "ok"

    # ── 巡检操作 ───────────────────────────────────────────────────────

    def check_offline_nodes(self) -> list[str]:
        """
        扫描所有节点，标记心跳超时的为离线 + 自动释放占用。
        
        关键改进：
          - 被占用的节点使用更短的超时阈值（15秒 vs 90秒）
          - 这样当子节点在占用状态下掉线时，能更快检测到并释放
        
        返回被标记为离线的 server_id 列表。
        """
        now = time.monotonic()
        offline_ids = []

        for sid, state in self._states.items():
            if state.status != "online":
                continue

            # 使用动态超时阈值：占用状态用短超时，空闲状态用默认超时
            timeout = state.heartbeat_timeout
            elapsed = now - state.last_heartbeat

            if elapsed < timeout:
                # 节点仍然存活
                # 清除探测标志（如果之前在探测）
                if state._probing:
                    state._probing = False
                continue

            # 心跳超时 → 标记离线 + 自动释放占用
            state.status = "offline"
            old_occ = state.occupied_by
            state.occupied_by = ""
            state.occupied_at = 0.0
            state._probing = False  # 清除探测状态
            offline_ids.append(sid)

            if old_occ:
                log.info(
                    f"[NodeState] OFFLINE & RELEASED (occupied): {sid} "
                    f"(was occupied by '{old_occ}', timeout={timeout}s, elapsed={elapsed:.1f}s)"
                )
            else:
                log.info(f"[NodeState] OFFLINE: {sid} (timeout={timeout}s, elapsed={elapsed:.1f}s)")

        return offline_ids

    def get_nodes_need_probe(self) -> list[dict]:
        """
        获取需要主动探活的占用节点列表。
        
        当被占用节点的心跳时间接近超时阈值（但尚未超时时），
        返回这些节点的信息，以便 SM 发起主动探活请求。
        
        Returns:
            [{"server_id": ..., "current_ip": ..., "occupied_by": ..., "seconds_until_timeout": ...}, ...]
        """
        now = time.monotonic()
        need_probe = []

        for sid, state in self._states.items():
            # 只探测在线且被占用的节点
            if state.status != "online":
                continue
            if not state.occupied_by:
                continue
            if state.last_heartbeat <= 0:
                continue

            timeout = state.heartbeat_timeout
            elapsed = now - state.last_heartbeat
            remaining = timeout - elapsed

            # 如果剩余时间 < PROBE_ADVANCE_SECONDS 且不在探测中（或距离上次探测已超过5秒），则需要探测
            if 0 < remaining <= PROBE_ADVANCE_SECONDS:
                # 防止频繁探测：至少间隔5秒才再次探测
                if state._probing and (now - state._last_probe_time) < 5.0:
                    continue

                need_probe.append({
                    "server_id": sid,
                    "current_ip": state.current_ip,
                    "occupied_by": state.occupied_by,
                    "seconds_until_timeout": round(remaining, 1),
                    "timeout": timeout,
                    "elapsed_since_hb": round(elapsed, 1),
                })

        return need_probe

    def mark_node_probing(self, server_id: str) -> None:
        """标记节点正在被主动探测"""
        state = self._states.get(server_id)
        if state:
            state._probing = True
            state._last_probe_time = time.monotonic()

    # ── 管理员操作 ─────────────────────────────────────────────────────

    def set_suspended(self, server_id: str) -> tuple[bool, str]:
        """暂停节点"""
        state = self._states.get(server_id)
        if not state:
            return False, f"node '{server_id}' not found"
        if state.status not in ("online", "approved"):
            return False, f"cannot suspend: current status is '{state.status}'"
        state.status = "suspended"
        # 暂停时不释放占用（保留记录以便恢复后继续）
        log.info(f"[NodeState] SUSPENDED: {server_id}")
        return True, "ok"

    def set_resumed(self, server_id: str) -> tuple[bool, str]:
        """恢复被暂停的节点"""
        state = self._states.get(server_id)
        if not state:
            return False, f"node '{server_id}' not found"
        if state.status != "suspended":
            return False, f"cannot resume: current status is '{state.status}'"
        state.status = "online"
        state.last_heartbeat = time.monotonic()  # 刷新时间戳避免立即被标离线
        log.info(f"[NodeState] RESUMED: {server_id}")
        return True, "ok"

    def set_deleted(self, server_id: str) -> None:
        """删除节点"""
        self.unregister(server_id)

    # ── 占用操作 ───────────────────────────────────────────────────────

    def occupy(self, server_id: str, username: str) -> tuple[bool, str]:
        """
        标记节点被账户占用。
        要求节点必须在线（有心跳或刚被恢复）。
        """
        state = self._states.get(server_id)
        if not state:
            return False, f"node '{server_id}' not found"

        # 必须是在线状态才能被占用
        if state.status not in ("online", "approved"):
            return False, f"cannot occupy: node status is '{state.status}'"

        # 检查是否已被其他人占用
        if state.occupied_by and state.occupied_by != username:
            return False, (
                f"already occupied by '{state.occupied_by}'"
            )

        state.occupied_by = username
        state.occupied_at = time.monotonic()
        log.info(f"[NodeState] OCCUPIED: {server_id} by '{username}'")
        return True, "ok"

    def release(self, server_id: str, check_offline: bool = True) -> bool:
        """
        释放节点占用。
        
        Args:
            server_id: 节点 ID
            check_offline: 是否检查并标记离线（默认 True）
                           当 Client 主动释放占用时，
                           如果节点心跳已超过短超时阈值（说明可能在占用期就掉了线），
                           直接标记为 offline 防止展示错误的在线状态。
        """
        state = self._states.get(server_id)
        if not state:
            return False
        had = state.occupied_by
        state.occupied_by = ""
        state.occupied_at = 0.0

        # ★ 关键修复：释放占用时检查节点是否真的存活
        # 场景：SE 在占用期间掉线 → Client 取消重连 → 释放占用
        # 如果不立即校验，status 仍是 online 且超时阈值从 15s→90s，
        # 导致 Web 刷新显示错误的"在线"状态长达 ~80 秒
        if check_offline and state.status == "online":
            now = time.monotonic()
            elapsed = now - state.last_heartbeat
            # 使用占用态的超时阈值来判断（比空闲阈值更严格）
            if elapsed > OCCUPIED_HEARTBEAT_TIMEOUT:
                old_status = state.status
                state.status = "offline"
                log.info(
                    f"[NodeState] RELEASE & OFFLINE: {server_id} "
                    f"(was occupied by '{had}', heartbeat stale for {elapsed:.1f}s > "
                    f"{OCCUPIED_HEARTBEAT_TIMEOUT}s threshold)"
                )
            else:
                log.info(f"[NodeState] RELEASED: {server_id} (was '{had}')")
        elif had:
            log.info(f"[NodeState] RELEASED: {server_id} (was '{had}')")

        return True

    def get_occupation_info(self, server_id: str) -> Optional[dict]:
        """查询占用信息"""
        state = self._states.get(server_id)
        if not state or not state.occupied_by:
            return None
        return {
            "occupied_by": state.occupied_by,
            "occupied_at": state.occupied_at,
        }

    # ── 查询接口 ───────────────────────────────────────────────────────

    def get(self, server_id: str) -> Optional[NodeState]:
        """获取单个节点的完整状态"""
        return self._states.get(server_id)

    def get_all_for_display(self) -> list[dict]:
        """
        获取所有节点的展示数据（供刷新 API 使用）。

        对每个节点计算 real_status（四种状态之一），返回可直接传给前端的列表。
        不修改任何状态，纯读取 + 计算。
        """
        results = []
        for state in self._states.values():
            d = state.to_dict()

            # 计算展示用 real_status
            d["real_status"] = self.compute_display_status(state)

            # req_status 用于兼容前端降级逻辑
            d["req_status"] = state.status

            results.append(d)
        return results

    @staticmethod
    def compute_display_status(state: NodeState) -> str:
        """
        计算节点的最终展示状态。

        优先级: suspended(最高) > offline > occupied(需在线) > online/approved
        
        关键规则: 占用状态只在节点实际在线时才生效，
                  离线节点即使有占用记录也显示离线。
        """
        if state.status == "suspended":
            return "suspended"
        if not state.is_alive or state.status == "offline":
            return "offline"
        if state.occupied_by:
            return "occupied"
        # 在线且未被占用
        if state.status in ("online", "approved"):
            return "online"
        return state.status  # fallback

    # ── DB 同步 ────────────────────────────────────────────────────────

    def prepare_db_sync_data(self) -> list[dict]:
        """
        准备需要回写到 DB 的状态快照。
        返回 [(server_id, status, last_heartbeat, current_ip, occupied_by, occupied_at), ...]
        
        由调用方决定何时写入（定期同步或关闭时）。
        """
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        rows = []
        for state in self._states.values():
            hb_iso = ""
            if state.last_heartbeat > 0:
                hb_iso = datetime.fromtimestamp(
                    state.last_heartbeat, tz=timezone.utc
                ).isoformat()
            occ_at_iso = ""
            if state.occupied_at > 0:
                occ_at_iso = datetime.fromtimestamp(
                    state.occupied_at, tz=timezone.utc
                ).isoformat()
            rows.append({
                "server_id": state.server_id,
                "status": state.status,
                "last_heartbeat": hb_iso,
                "current_ip": state.current_ip,
                "occupied_by": state.occupied_by,
                "occupied_at": occ_at_iso,
                "_sync_time": now_iso,
            })
        return rows

    def __len__(self) -> int:
        return len(self._states)

    def __contains__(self, server_id: str) -> bool:
        return server_id in self._states


# ── 全局单例 ──────────────────────────────────────────────────────────────

manager = NodeStateManager()
