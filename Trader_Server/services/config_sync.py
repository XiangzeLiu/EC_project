"""
Config Sync Service — 券商配置同步与生命周期管理

职责:
  1. 从 SM 拉取券商配置（初始加载 / 热更新）
  2. 根据配置创建/重建 Broker API 实例
  3. 断线自动重连（指数退避）
  4. 向所有已连接 Client 推送券商状态变更通知

三个触发场景:
  A. 审批通过后初始加载 → init_broker()
  B. 心跳检测到 config_version 变更 → check_and_reload()
  C. SDK 操作失败触发断线重连 → _auto_reconnect_loop()
"""

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
import uuid

from ..api.factory import BrokerFactory
from ..api.base import BaseBrokerAPI
from .https_client import urlopen
from ..config import state
from ..network import ws_server
from .client_security import safe_client_message, safe_order_status_message

log = logging.getLogger("trader_server.config_sync")

# 全局单例: 当前活跃的 Broker 实例
_current_broker: BaseBrokerAPI | None = None
_current_broker_type: str = ""
_local_config_version: int = 0       # 本地缓存的 config_version
_auto_reconnect_task: asyncio.Task | None = None
_config_event_task: asyncio.Task | None = None
_reconnect_enabled: bool = True
_last_reload_trigger_ts: float = 0.0
_min_reload_interval_sec: float = 1.5
_retry_schedule_sec: tuple[int, ...] = (30, 60, 120, 300, 600)
_connect_failure_count: int = 0
_next_connect_retry_at: float = 0.0
_auto_retry_paused: bool = False
_auto_retry_pause_reason: str = ""
_last_connect_error: dict[str, object] = {"code": "", "message": "", "retryable": True}
_runtime_loop: asyncio.AbstractEventLoop | None = None
_broker_lifecycle_lock = asyncio.Lock()
_broker_runtime_task: asyncio.Task | None = None
_broker_runtime_watchdog_task: asyncio.Task | None = None
_runtime_recovery_grace_seconds: float = 120.0


# ── 公共接口 ────────────────────────────────────────────────────

def get_current_broker() -> BaseBrokerAPI | None:
    """获取当前活跃的 Broker 实例（可能为 None）"""
    return _current_broker


def get_broker_status(public: bool = False) -> dict:
    """
    获取当前券商连接状态摘要。能力字段用于上层判断当前券商是否支持行情、下单、撤单、持仓和订单查询。
    """
    if not _current_broker:
        capabilities = {}
        if _current_broker_type and BrokerFactory.is_supported(_current_broker_type):
            try:
                capabilities = BrokerFactory.get_adapter_spec(_current_broker_type).get("capabilities", {})
            except Exception:
                capabilities = {}
        status = {
            "broker_type": _current_broker_type or "none",
            "connected": False,
            "config_version": _local_config_version,
            "last_action": "not_initialized",
            "capabilities": capabilities,
            "error": dict(_last_connect_error),
        }
        return _client_status(status) if public else status
    runtime_health_fn = getattr(_current_broker, "runtime_health", None)
    runtime_health = runtime_health_fn() if callable(runtime_health_fn) else {}
    connected = bool(
        runtime_health.get("operational")
        if isinstance(runtime_health, dict) and "operational" in runtime_health
        else _current_broker._connected
    )
    capabilities_fn = getattr(_current_broker, "effective_capabilities", None)
    capabilities = capabilities_fn() if callable(capabilities_fn) else _current_broker.capabilities()
    detail_fn = getattr(_current_broker, "status_detail", None)
    detail = detail_fn() if callable(detail_fn) else {}
    status = {
        "broker_type": _current_broker.broker_type,
        "connected": connected,
        "config_version": _local_config_version,
        "last_action": "active",
        "capabilities": capabilities,
        "error": {},
    }
    if not connected:
        broker_error_fn = getattr(_current_broker, "get_connection_error", None)
        broker_error = broker_error_fn() if callable(broker_error_fn) else {}
        status["error"] = dict(broker_error or _last_connect_error)
    if isinstance(detail, dict):
        status.update(detail)
    return _client_status(status) if public else status


def _client_status(status: dict) -> dict:
    account = status.get("account") if isinstance(status.get("account"), dict) else {}
    authority = str(account.get("authority_level") or "unknown").strip().lower()
    read_only = authority in {"read-only", "read_only", "readonly"}
    raw_error = status.get("error") if isinstance(status.get("error"), dict) else {}
    error: dict[str, object] = {}
    if raw_error:
        code = str(raw_error.get("code") or "BROKER_CONNECT_FAILED").strip().upper()
        error = {
            "code": "TRADING_SERVICE_UNAVAILABLE",
            "message": safe_client_message(code, str(raw_error.get("message") or ""), "交易服务暂不可用"),
            "retryable": bool(raw_error.get("retryable", True)),
        }
    order_options = status.get("order_options") if isinstance(status.get("order_options"), dict) else {}
    supported_tifs = []
    for item in order_options.get("supported_tifs") or []:
        value = str(item or "").strip()
        if value and value not in supported_tifs:
            supported_tifs.append(value)
    return {
        "connected": bool(status.get("connected")),
        "capabilities": dict(status.get("capabilities") or {}),
        "read_only": read_only,
        "account": {"authority_level": "read-only" if read_only else "full"},
        "order_options": {
            "default_route": str(order_options.get("default_route") or "SMART").strip().upper() or "SMART",
            "routes": [str(item).strip().upper() for item in order_options.get("routes") or ["SMART"] if str(item).strip()],
            "route_editable": bool(order_options.get("route_editable", False)),
            "hidden_order": bool(order_options.get("hidden_order", False)),
            "supported_tifs": supported_tifs,
        },
        "error": error,
    }


def get_client_trading_status() -> dict:
    return get_broker_status(public=True)

def _reset_connect_retry_state() -> None:
    global _connect_failure_count, _next_connect_retry_at, _auto_retry_paused, _auto_retry_pause_reason, _last_connect_error
    _connect_failure_count = 0
    _next_connect_retry_at = 0.0
    _auto_retry_paused = False
    _auto_retry_pause_reason = ""
    _last_connect_error = {"code": "", "message": "", "retryable": True}


def _capture_connect_error(broker: BaseBrokerAPI | None) -> dict[str, object]:
    global _last_connect_error
    err = broker.get_connection_error() if broker and hasattr(broker, "get_connection_error") else {}
    code = str((err or {}).get("code") or "BROKER_CONNECT_FAILED")
    message = str((err or {}).get("message") or "Broker connect failed")[:240]
    retryable = bool((err or {}).get("retryable", True))
    _last_connect_error = {"code": code, "message": message, "retryable": retryable}
    return dict(_last_connect_error)


def _record_connect_failure(broker: BaseBrokerAPI | None, trigger: str) -> dict[str, object]:
    global _connect_failure_count, _next_connect_retry_at, _auto_retry_paused, _auto_retry_pause_reason
    err = _capture_connect_error(broker)
    _connect_failure_count += 1
    if not bool(err.get("retryable", True)):
        if trigger in {"auto", "ensure"}:
            _auto_retry_paused = True
            _auto_retry_pause_reason = str(err.get("message") or err.get("code") or "auth_blocked")
        _next_connect_retry_at = 0.0
        return err

    idx = min(max(_connect_failure_count - 1, 0), len(_retry_schedule_sec) - 1)
    _next_connect_retry_at = time.time() + _retry_schedule_sec[idx]
    _auto_retry_paused = False
    _auto_retry_pause_reason = ""
    return err


def _can_attempt_connect(trigger: str) -> bool:
    if trigger not in {"auto", "ensure"}:
        return True
    if _auto_retry_paused:
        log.warning("connect attempt skipped: auto retry paused (%s)", _auto_retry_pause_reason or _last_connect_error.get("code", ""))
        return False
    if _next_connect_retry_at > time.time():
        remaining = max(1, int(_next_connect_retry_at - time.time()))
        log.info("connect attempt skipped by cooldown: %ss remaining", remaining)
        return False
    return True


async def _restore_quote_subscriptions(broker: BaseBrokerAPI) -> dict:
    try:
        from .quote_provider import restore_subscriptions

        result = await restore_subscriptions(broker)
        if not result.get("success"):
            log.warning("Quote subscription restore skipped: %s", result.get("message", "unknown error"))
        return result
    except Exception as exc:
        log.warning("Quote subscription restore failed: %s", exc)
        return {
            "success": False,
            "code": "QUOTE_SUBSCRIBE_FAILED",
            "message": str(exc),
        }


async def _bind_broker_events(broker: BaseBrokerAPI) -> None:
    global _runtime_loop
    _runtime_loop = asyncio.get_running_loop()
    broker.set_quote_callback(_on_quote_from_broker)
    set_order_callback = getattr(broker, "set_order_event_callback", None)
    if callable(set_order_callback):
        set_order_callback(_on_order_event_from_broker)
    set_position_callback = getattr(broker, "set_position_event_callback", None)
    if callable(set_position_callback):
        set_position_callback(_on_position_event_from_broker)
    set_runtime_callback = getattr(broker, "set_runtime_status_callback", None)
    if callable(set_runtime_callback):
        set_runtime_callback(_on_runtime_status_from_broker)
        health = _runtime_health(broker)
        state_name = str(health.get("state") or "")
        if state_name in {"degraded_waiting", "restoring", "reconnect_required"}:
            recovery_code = str(health.get("recovery_code") or "")
            _on_runtime_status_from_broker({
                "code": recovery_code,
                "state": state_name,
                "data_lost": recovery_code == "1101",
                "generation": int(health.get("generation") or 0),
            })
    start_events = getattr(broker, "start_account_events", None)
    if not callable(start_events):
        return
    try:
        await start_events()
    except Exception as exc:
        log.warning("Account event stream unavailable for %s: %s", broker.broker_type, exc)


def _schedule_broker_push(message: dict) -> None:
    loop = _runtime_loop
    if not loop or not loop.is_running():
        log.warning("Broker event loop unavailable, dropping %s", message.get("type", "event"))
        return

    def submit() -> None:
        asyncio.create_task(ws_server.broadcast_message(message))

    loop.call_soon_threadsafe(submit)


def _on_order_event_from_broker(event: dict) -> None:
    raw_payload = dict(event or {})
    payload = {
        key: raw_payload.get(key)
        for key in (
            "order_id", "symbol", "status", "filled_qty", "remaining_qty",
            "avg_fill_price", "can_cancel", "updated_at", "action",
        )
        if key in raw_payload
    }
    payload["status_message"] = safe_order_status_message(raw_payload)
    payload.setdefault("event_id", f"ordevt_{uuid.uuid4().hex}")
    payload.setdefault("updated_at", "")
    _schedule_broker_push({
        "type": "ORDER_STATUS_UPDATE",
        "id": payload["event_id"],
        "timestamp": int(time.time() * 1000),
        "payload": payload,
    })


def _on_position_event_from_broker(event: dict) -> None:
    raw_payload = dict(event or {})
    payload = {
        key: raw_payload.get(key)
        for key in ("reason", "symbol", "order_id", "updated_at")
        if key in raw_payload
    }
    payload.setdefault("event_id", f"posevt_{uuid.uuid4().hex}")
    payload.setdefault("reason", "account_update")
    _schedule_broker_push({
        "type": "POSITION_INVALIDATED",
        "id": payload["event_id"],
        "timestamp": int(time.time() * 1000),
        "payload": payload,
    })


def _runtime_health(broker: BaseBrokerAPI | None) -> dict[str, object]:
    health_fn = getattr(broker, "runtime_health", None) if broker else None
    if not callable(health_fn):
        return {}
    try:
        health = health_fn()
    except Exception:
        return {}
    return dict(health) if isinstance(health, dict) else {}


def _cancel_runtime_watchdog() -> None:
    global _broker_runtime_watchdog_task
    task = _broker_runtime_watchdog_task
    _broker_runtime_watchdog_task = None
    if task and not task.done():
        task.cancel()


def _start_runtime_task(coro) -> bool:
    global _broker_runtime_task
    if _broker_runtime_task and not _broker_runtime_task.done():
        close = getattr(coro, "close", None)
        if callable(close):
            close()
        return False
    task = asyncio.create_task(coro, name="broker-runtime-recovery")
    _broker_runtime_task = task

    def clear(completed: asyncio.Task) -> None:
        global _broker_runtime_task
        if _broker_runtime_task is completed:
            _broker_runtime_task = None

    task.add_done_callback(clear)
    return True


def _start_runtime_watchdog(broker: BaseBrokerAPI, generation: int) -> None:
    global _broker_runtime_watchdog_task
    _cancel_runtime_watchdog()
    _broker_runtime_watchdog_task = asyncio.create_task(
        _runtime_recovery_watchdog(broker, generation),
        name="broker-runtime-watchdog",
    )


async def _runtime_recovery_watchdog(broker: BaseBrokerAPI, generation: int) -> None:
    try:
        await asyncio.sleep(_runtime_recovery_grace_seconds)
        if broker is not _current_broker:
            return
        health = _runtime_health(broker)
        if int(health.get("generation") or 0) != generation:
            return
        if str(health.get("state") or "") != "degraded_waiting":
            return
        log.warning(
            "IB runtime recovery timed out after %ss; escalating to full reconnect",
            int(_runtime_recovery_grace_seconds),
        )
        marker = getattr(broker, "mark_runtime_reconnect_required", None)
        if callable(marker):
            marker("IB_RUNTIME_RECOVERY_TIMEOUT", "IB runtime recovery timed out")
        _start_runtime_task(_run_runtime_reconnect(broker, generation, "watchdog_timeout"))
    except asyncio.CancelledError:
        return


def _on_runtime_status_from_broker(event: dict) -> None:
    loop = _runtime_loop
    if not loop or not loop.is_running():
        log.warning("Broker runtime loop unavailable, dropping runtime event")
        return

    def submit() -> None:
        broker = _current_broker
        if not broker:
            return
        payload = dict(event or {})
        generation = int(payload.get("generation") or 0)
        health = _runtime_health(broker)
        if generation and int(health.get("generation") or 0) != generation:
            return
        state_name = str(payload.get("state") or "")
        code = str(payload.get("code") or "")
        log.warning(
            "Broker runtime event: type=%s state=%s code=%s generation=%s",
            broker.broker_type,
            state_name,
            code,
            generation,
        )
        if state_name == "degraded_waiting":
            _broadcast_status(broker.broker_type, "disconnected")
            _start_runtime_watchdog(broker, generation)
            return
        _cancel_runtime_watchdog()
        if state_name == "restoring":
            _start_runtime_task(
                _run_runtime_recovery(
                    broker,
                    generation,
                    data_lost=bool(payload.get("data_lost")),
                )
            )
            return
        if state_name == "reconnect_required":
            _broadcast_status(broker.broker_type, "disconnected")
            _start_runtime_task(_run_runtime_reconnect(broker, generation, code or "runtime_disconnect"))

    loop.call_soon_threadsafe(submit)


async def _run_runtime_recovery(
    broker: BaseBrokerAPI,
    generation: int,
    *,
    data_lost: bool,
) -> None:
    async with _broker_lifecycle_lock:
        if broker is not _current_broker:
            return
        health = _runtime_health(broker)
        if int(health.get("generation") or 0) != generation:
            return
        prepare = getattr(broker, "prepare_runtime_recovery", None)
        complete = getattr(broker, "complete_runtime_recovery", None)
        if not callable(prepare) or not callable(complete):
            return
        try:
            prepared = await prepare(data_lost=data_lost)
            if not prepared:
                raise RuntimeError("Broker runtime recovery was not prepared")
            if data_lost:
                await _restore_quote_subscriptions(broker)
            if not complete():
                raise RuntimeError("Broker runtime recovery could not be completed")
            start_events = getattr(broker, "start_account_events", None)
            if callable(start_events):
                await start_events()
            _reset_connect_retry_state()
            _broadcast_status(broker.broker_type, "reconnected")
            log.info(
                "Broker runtime recovery completed: type=%s generation=%s data_lost=%s",
                broker.broker_type,
                generation,
                data_lost,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Broker runtime recovery failed: %s", exc)
            marker = getattr(broker, "mark_runtime_reconnect_required", None)
            if callable(marker):
                marker("IB_RUNTIME_RECOVERY_FAILED", str(exc))
            await _do_hot_reload_locked(trigger="runtime")


async def _run_runtime_reconnect(
    broker: BaseBrokerAPI,
    generation: int,
    reason: str,
) -> None:
    async with _broker_lifecycle_lock:
        if broker is not _current_broker:
            return
        health = _runtime_health(broker)
        if generation and int(health.get("generation") or 0) != generation:
            return
        log.warning(
            "Broker runtime full reconnect: type=%s generation=%s reason=%s",
            broker.broker_type,
            generation,
            reason,
        )
        await _do_hot_reload_locked(trigger="runtime")


async def ensure_broker_connected() -> bool:
    """
    业务触发前保障券商已连接。
    """
    global _current_broker

    if _current_broker:
        try:
            if await _current_broker.is_connected():
                return True
            health = _runtime_health(_current_broker)
            if health and (
                bool(health.get("waiting_for_upstream"))
                or bool(health.get("reconnect_required"))
            ):
                return False
        except Exception:
            pass

    return await _do_hot_reload(trigger="ensure")


async def logout_current_broker() -> None:
    await _destroy_broker()


async def init_broker() -> bool:
    """
    场景A: Trader_Server 注册审批通过后调用，首次从 SM 拉取配置并连接券商
    
    Returns:
        是否成功初始化
    """
    global _current_broker, _current_broker_type, _local_config_version

    async with _broker_lifecycle_lock:
        if not state.token or not state.server_id:
            log.warning("init_broker: no token/server_id, skip")
            return False

        cfg = await _pull_config_from_sm()
        if not cfg:
            log.error("init_broker: failed to pull config from SM")
            return False

        broker_type = cfg.get("broker_type", "")
        credentials = cfg.get("credentials", {})
        config_version = cfg.get("config_version", 0)

        if _current_broker:
            try:
                if _current_broker_type == broker_type and await _current_broker.is_connected():
                    _local_config_version = config_version
                    log.info("init_broker: broker already connected, skip duplicate initialization")
                    return True
            except Exception:
                pass
            await _destroy_broker_locked()

        try:
            broker = BrokerFactory.create(broker_type)
            normalized = broker.normalize_credentials(credentials)
            ok = await broker.connect(normalized)
            if not ok:
                err = _record_connect_failure(broker, "init")
                log.error(f"init_broker: {broker_type} connect failed [{err.get('code')}]: {err.get('message')}")
                _broadcast_status(broker_type, "connect_failed")
                return False

            # Publish the version before releasing the lifecycle lock. A
            # heartbeat waiting behind this initialization will then skip the
            # same configuration instead of creating a second broker.
            _local_config_version = config_version
            _current_broker = broker
            _current_broker_type = broker_type
            await _bind_broker_events(broker)
            await _restore_quote_subscriptions(broker)
            _reset_connect_retry_state()
            _start_auto_reconnect()

            log.info(f"init_broker: {broker_type} initialized successfully (version={_local_config_version})")
            _broadcast_status(broker_type, "connected")
            return True

        except Exception as e:
            log.error(f"init_broker: exception: {e}")
            _broadcast_status(broker_type, "error")
            return False


async def check_and_reload(remote_version: int, source: str = "heartbeat") -> bool:
    """
    场景B: 心跳回调中调用，对比版本号决定是否热更新
    
    Args:
        remote_version: SM 返回的最新 config_version
    
    Returns:
        是否执行了重载
    """
    global _last_reload_trigger_ts

    if remote_version <= _local_config_version:
        return False

    now = time.time()
    if (now - _last_reload_trigger_ts) < _min_reload_interval_sec:
        log.debug(f"check_and_reload skipped by debounce: source={source}, remote={remote_version}")
        return False
    _last_reload_trigger_ts = now

    log.info(
        f"check_andreload: source={source}, version changed "
        f"{_local_config_version} → {remote_version}, pulling new config..."
    )

    return await _do_hot_reload(trigger="config_change", expected_version=remote_version)


async def force_reload() -> bool:
    """
    强制重新拉取配置并重建连接（管理员手动触发 reload 时调用）
    """
    log.info("force_reload: manual trigger")
    return await _do_hot_reload(trigger="manual")


def start_config_event_listener() -> None:
    """启动配置变更 SSE 监听（快速生效通道）"""
    global _config_event_task
    if _config_event_task and not _config_event_task.done():
        return
    if not state.server_id or not state.token or not state.manager_url:
        return
    _config_event_task = asyncio.create_task(_config_event_loop())
    log.info("Config event listener started")


async def _config_event_loop():
    """监听 SM /nodes/config-events，收到 CONFIG_CHANGED 立即热重载"""
    while _reconnect_enabled and not state.is_shutting_down:
        if not state.server_id or not state.token:
            await asyncio.sleep(2)
            continue

        url = f"{state.manager_url.rstrip('/')}/nodes/config-events?server_id={state.server_id}"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {state.token}")
        req.add_header("Accept", "text/event-stream")

        resp = None
        try:
            loop = asyncio.get_running_loop()
            resp = await loop.run_in_executor(None, lambda: urlopen(req, timeout=60))

            while _reconnect_enabled and not state.is_shutting_down:
                raw = await loop.run_in_executor(None, resp.readline)
                if not raw:
                    break

                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue

                try:
                    data = json.loads(payload)
                except Exception:
                    continue

                if data.get("type") != "CONFIG_CHANGED":
                    continue

                version = int(data.get("config_version", 0) or 0)
                if version > 0:
                    await check_and_reload(version, source="sse")

        except Exception as e:
            log.debug(f"Config event stream reconnecting: {e}")
            await asyncio.sleep(2)
        finally:
            if resp is not None:
                try:
                    resp.close()
                except Exception:
                    pass


# ── 内部实现 ──────────────────────────────────────────────────

async def _pull_config_from_sm() -> dict | None:
    """
    从 SM 拉取当前节点的完整券商配置
    
    Returns:
        配置字典或 None（失败时）
    """
    url = (
        f"{state.manager_url.rstrip('/')}/api/nodes/config"
        f"?server_id={state.server_id}&token={state.token}"
    )

    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {state.token}")

    try:
        loop = asyncio.get_running_loop()

        def _fetch_json():
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))

        data = await loop.run_in_executor(None, _fetch_json)

        if data.get("ok"):
            return data

        log.warning(f"_pull_config_from_sm: SM returned error: {data.get('error')}")
        return None

    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        log.error(f"_pull_config_from_sm: HTTP {e.code}: {body}")
        return None
    except Exception as e:
        log.error(f"_pull_config_from_sm: {e}")
        return None


async def _do_hot_reload(trigger: str = "auto", expected_version: int | None = None) -> bool:
    async with _broker_lifecycle_lock:
        if expected_version is not None and expected_version <= _local_config_version:
            return False
        return await _do_hot_reload_locked(trigger)


async def _do_hot_reload_locked(trigger: str = "auto") -> bool:
    """
    执行热更新：拉取新配置 → 断开旧连接 → 创建新实例 → 连接
    """
    global _current_broker, _current_broker_type, _local_config_version, _auto_retry_paused, _auto_retry_pause_reason, _next_connect_retry_at

    if trigger in {"manual", "config_change", "init"}:
        _auto_retry_paused = False
        _auto_retry_pause_reason = ""
        _next_connect_retry_at = 0.0

    if not _can_attempt_connect(trigger):
        return False

    cfg = await _pull_config_from_sm()
    if not cfg:
        log.error("_do_hot_reload: failed to pull new config")
        return False

    new_type = cfg.get("broker_type", "")
    new_creds = cfg.get("credentials", {})
    new_version = cfg.get("config_version", 0)
    old_type = _current_broker_type

    if old_type and old_type != new_type:
        log.info(f"_do_hot_reload: broker type changing {old_type} → {new_type}, full recreate")
        await _destroy_broker_locked()
    elif _current_broker:
        log.info(f"_do_hot_reload: reconnecting {new_type} with latest credentials...")
        try:
            await _current_broker.disconnect()
        except Exception:
            pass
        _current_broker = None

    try:
        broker = BrokerFactory.create(new_type)
        normalized = broker.normalize_credentials(new_creds)
        ok = await broker.connect(normalized)

        if not ok:
            err = _record_connect_failure(broker, trigger)
            _current_broker = broker
            _current_broker_type = new_type
            _local_config_version = new_version
            log.error(f"_do_hot_reload: {new_type} connect failed [{err.get('code')}]: {err.get('message')}")
            _broadcast_status(new_type, "reload_failed")
            return False

        _current_broker = broker
        _current_broker_type = new_type
        _local_config_version = new_version
        await _bind_broker_events(broker)
        await _restore_quote_subscriptions(broker)
        _reset_connect_retry_state()
        _start_auto_reconnect()

        log.info(f"_do_hot_reload: {new_type} reloaded OK (version={new_version})")
        _broadcast_status(new_type, "reloaded")
        return True

    except Exception as e:
        _local_config_version = new_version
        _current_broker = None
        log.error(f"_do_hot_reload: exception: {e}")
        _broadcast_status(new_type or new_type, "reload_error")
        return False


async def _destroy_broker():
    async with _broker_lifecycle_lock:
        await _destroy_broker_locked()


async def _destroy_broker_locked():
    """销毁当前 Broker 实例"""
    global _current_broker, _current_broker_type
    
    if _current_broker:
        broker = _current_broker
        _current_broker = None
        try:
            await broker.disconnect()
        except Exception as e:
            log.warning(f"_destroy_broker disconnect error: {e}")
    _current_broker_type = ""


def _start_auto_reconnect():
    """
    启动场景C的后台自动重连协程
    仅在未运行时启动
    """
    global _auto_reconnect_task
    if _auto_reconnect_task and not _auto_reconnect_task.done():
        return  # 已在运行
    
    _auto_reconnect_task = asyncio.create_task(_auto_reconnect_loop())
    log.info("Auto-reconnect monitor started")


async def _auto_reconnect_loop():
    """
    场景C: 后台监控券商连接状态，断线时自动重连
    """
    global _auto_reconnect_task

    announced_disconnect = False

    while _reconnect_enabled and not state.is_shutting_down:
        try:
            await asyncio.sleep(30)
            if not _current_broker:
                continue

            connected = await _current_broker.is_connected()
            if connected:
                if announced_disconnect:
                    _broadcast_status(_current_broker_type, "reconnected")
                announced_disconnect = False
                continue

            health = _runtime_health(_current_broker)
            if bool(health.get("waiting_for_upstream")):
                announced_disconnect = True
                continue
            if _broker_runtime_task and not _broker_runtime_task.done():
                announced_disconnect = True
                continue

            if not announced_disconnect:
                log.warning("Auto-reconnect: %s disconnected", _current_broker_type)
                _broadcast_status(_current_broker_type, "disconnected")
                announced_disconnect = True

            if _auto_retry_paused:
                log.warning("Auto-reconnect paused: %s", _auto_retry_pause_reason or _last_connect_error.get("code", ""))
                continue

            if _next_connect_retry_at > time.time():
                remaining = max(1, int(_next_connect_retry_at - time.time()))
                log.info("Auto-reconnect cooldown: %ss remaining", remaining)
                continue

            ok = await _do_hot_reload(trigger="auto")
            if ok:
                announced_disconnect = False
                log.info("Auto-reconnect: %s reconnected", _current_broker_type)
                _broadcast_status(_current_broker_type, "reconnected")
            elif _auto_retry_paused:
                _broadcast_status(_current_broker_type, "abandoned")
            else:
                delay = 0
                if _next_connect_retry_at > time.time():
                    delay = max(1, int(_next_connect_retry_at - time.time()))
                log.warning("Auto-reconnect failed, next retry in %ss", delay)

        except asyncio.CancelledError:
            log.debug("Auto-reconnect loop cancelled")
            break
        except Exception as e:
            log.error(f"Auto-reconnect loop unexpected error: {e}")
            await asyncio.sleep(10)


def _on_quote_from_broker(quote: dict):
    """
    Broker 行情数据回调 → 转发为 WS 消息给对应 Client
    
    注意: 此函数可能在 IB 线程中被调用（通过 run_coroutine_threadsafe），
    也可能在 async 上下文中直接被调用。
    quote 格式: {"symbol", "bid", "ask", "last", "volume", "ts"}
    """
    public_quote = {
        key: quote.get(key)
        for key in ("symbol", "bid", "ask", "last", "volume", "ts")
        if key in quote
    }
    msg = {
        "type": "QUOTE_DATA",
        "id": f"quote_{int(time.time() * 1000)}",
        "timestamp": int(time.time() * 1000),
        "payload": public_quote,
    }
    # 广播给所有已连接的 Client
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(ws_server.broadcast_quote_message(msg))
        else:
            log.warning("_on_quote_from_broker: event loop not running, dropping quote")
    except RuntimeError:
        log.warning("_on_quote_from_broker: no event loop, dropping quote")


def _broadcast_status(broker_type: str, status: str):
    """
    向所有已连接 Client 推送券商状态变更消息
    
    status 值:
      connected / disconnected / reconnected / connect_failed /
      reload_failed / reloaded / error / abandoned
    """
    msg = {
        "type": "BROKER_STATUS_CHANGE",
        "id": f"bsc_{int(time.time() * 1000)}",
        "timestamp": int(time.time() * 1000),
        "payload": {
            "status": status,
            "broker_detail": get_client_trading_status(),
        },
    }
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(ws_server.broadcast_message(msg))
    except RuntimeError:
        pass
    log.info(f"Broker status broadcast: {broker_type} → {status}")


# ── 清理接口（关闭时调用）─────────────────────────────────────

async def shutdown():
    """Shutdown config_sync tasks and broker resources."""
    global _reconnect_enabled, _auto_reconnect_task, _config_event_task
    global _broker_runtime_task, _broker_runtime_watchdog_task

    _reconnect_enabled = False

    tasks = []
    if _config_event_task and not _config_event_task.done():
        _config_event_task.cancel()
        tasks.append(_config_event_task)
    if _auto_reconnect_task and not _auto_reconnect_task.done():
        _auto_reconnect_task.cancel()
        tasks.append(_auto_reconnect_task)
    if _broker_runtime_task and not _broker_runtime_task.done():
        _broker_runtime_task.cancel()
        tasks.append(_broker_runtime_task)
    if _broker_runtime_watchdog_task and not _broker_runtime_watchdog_task.done():
        _broker_runtime_watchdog_task.cancel()
        tasks.append(_broker_runtime_watchdog_task)

    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=5,
            )
        except asyncio.TimeoutError:
            log.warning("Config sync shutdown timed out while waiting for background tasks")

    _config_event_task = None
    _auto_reconnect_task = None
    _broker_runtime_task = None
    _broker_runtime_watchdog_task = None

    await _destroy_broker()
    log.info("Config sync service shut down")
