"""
Heartbeat Sender — 向 Server_manager 定期发送心跳保活

协议: POST /nodes/heartbeat
认证: Authorization: Bearer {token}
间隔: 默认 30s（可由 SM 的 next_interval 动态调整）

特性:
  - 失败指数退避重试（1s→2s→4s→...→最大60s）
  - 成功后重置退避计数器
  - 统计 OK/FAIL 计数
  - 支持优雅关闭
"""

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request

from ..config import state

log = logging.getLogger("server_economic.heartbeat")


class HeartbeatSender:
    """心跳发送器 — 注册成功后在后台循环发送心跳"""

    def __init__(
        self,
        interval: float = 30.0,
        max_backoff: float = 60.0,
    ):
        self.interval = interval
        self.default_interval = interval  # 记录默认间隔（用于非占用状态）
        self.max_backoff = max_backoff
        self._task: asyncio.Task | None = None
        self._sequence: int = 0
        self._ok_count: int = 0
        self._fail_count: int = 0
        self._backoff: float = 1.0
        self._running: bool = False

    @property
    def sequence(self) -> int:
        return self._sequence

    @property
    def stats(self) -> dict:
        return {
            "total": self._sequence,
            "ok": self._ok_count,
            "fail": self._fail_count,
            "backoff": round(self._backoff, 1),
            "running": self._running,
        }

    async def start(self):
        """启动心跳循环（异步）"""
        if self._running:
            log.warning("Heartbeat already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        log.info(f"Heartbeat started (interval={self.interval}s)")

    def stop(self):
        """请求停止心跳循环"""
        self._running = False
        log.info("Heartbeat stop requested")

    async def wait_stopped(self):
        """等待循环真正结束"""
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._task = None
        log.info(f"Heartbeat stopped. Final stats: {self.stats}")

    def send_once_sync(self) -> tuple[bool, str]:
        """
        同步发送一次心跳（阻塞调用，用于测试或首次验证）

        Returns:
            (成功, 响应消息)
        """
        return self._do_heartbeat()

    async def _loop(self):
        """异步心跳主循环"""
        while self._running and not state.is_shutting_down:
            ok, msg = self._do_heartbeat()

            # 根据结果调整下次间隔
            if ok:
                self._backoff = 1.0
                wait = self.interval
            else:
                wait = min(self._backoff, self.max_backoff)
                self._backoff = min(self._backoff * 2, self.max_backoff)

            # 分段 sleep 以响应关闭信号
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline and self._running and not state.is_shutting_down:
                await asyncio.sleep(min(1.0, deadline - time.monotonic()))

    def _do_heartbeat(self) -> tuple[bool, str]:
        """
        执行单次心跳请求

        Returns:
            (是否成功, 响应消息)
        """
        self._sequence += 1
        seq = self._sequence

        if not state.token:
            self._fail_count += 1
            err_msg = "No token available"
            log.error(f"[#{seq}] Heartbeat FAIL: {err_msg}")
            state.heartbeat_ok = False
            state.heartbeat_fail_count += 1
            return False, err_msg

        url = f"{state.manager_url.rstrip('/')}/nodes/heartbeat"
        payload = json.dumps({
            "ts": int(time.time()),
            "ip": "",  # 可扩展为自动检测公网 IP
        }).encode("utf-8")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {state.token}",
        }

        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))

                status = data.get("status", "")
                next_interval = data.get("next_interval", self.interval)

                if status == "ok":
                    self._ok_count += 1
                    state.heartbeat_ok = True
                    state.last_heartbeat_time = time.time()
                    state.heartbeat_fail_count = 0

                    # ★ 占用感知的动态间隔调整
                    # SM 会告知此节点是否被占用：
                    #   - 被占用 → next_interval=5s（快速心跳，配合SM端15秒超时实现快速掉线检测）
                    #   - 未被占用 → next_interval=30s（正常心跳）
                    if next_interval and next_interval > 0:
                        new_interval = float(next_interval)
                        self.interval = new_interval
                        # 记录诊断信息（仅在间隔变化时）
                        is_occupied = data.get("occupied", False)
                        if is_occupied:
                            log.debug(
                                f"[#{seq}] Node is OCCUPIED by '{data.get('occupied_by', '?')}', "
                                f"using fast heartbeat: {new_interval}s "
                                f"(SM timeout={data.get('occupied_timeout', '?')}s)"
                            )
                        elif new_interval != self.default_interval:
                            log.debug(f"[#{seq}] Node released, heartbeat interval reset to {new_interval}s")

                    log.debug(
                        f"[#{seq}] Heartbeat OK "
                        f"(ok={self._ok_count} fail={self._fail_count}) "
                        f"next_interval={next_interval}s"
                    )
                    return True, "ok"

                else:
                    self._fail_count += 1
                    state.heartbeat_ok = False
                    state.heartbeat_fail_count += 1
                    msg = data.get("message", "Unknown error")
                    log.warning(f"[#{seq}] Heartbeat rejected: {msg}")
                    return False, msg

        except urllib.error.HTTPError as e:
            self._fail_count += 1
            state.heartbeat_ok = False
            state.heartbeat_fail_count += 1
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:120]
            except Exception:
                pass
            msg = f"HTTP {e.code}: {body}"
            log.warning(f"[#{seq}] Heartbeat error: {msg}")
            return False, msg

        except Exception as e:
            self._fail_count += 1
            state.heartbeat_ok = False
            state.heartbeat_fail_count += 1
            msg = str(e)[:100]
            log.warning(f"[#{seq}] Heartbeat exception: {msg}")
            return False, msg
