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

from ..api.factory import BrokerFactory
from ..api.base import BaseBrokerAPI
from ..config import state
from ..network import ws_server

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


# ── 公共接口 ────────────────────────────────────────────────────

def get_current_broker() -> BaseBrokerAPI | None:
    """获取当前活跃的 Broker 实例（可能为 None）"""
    return _current_broker


def get_broker_status() -> dict:
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
        return {
            "broker_type": _current_broker_type or "none",
            "connected": False,
            "config_version": _local_config_version,
            "last_action": "not_initialized",
            "capabilities": capabilities,
        }
    return {
        "broker_type": _current_broker.broker_type,
        "connected": _current_broker._connected,
        "config_version": _local_config_version,
        "last_action": "active",
        "capabilities": _current_broker.capabilities(),
    }

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


async def ensure_broker_connected() -> bool:
    """
    业务触发前保障券商已连接。
    """
    global _current_broker

    if _current_broker:
        try:
            if await _current_broker.is_connected():
                return True
        except Exception:
            pass

    return await _do_hot_reload(trigger="ensure")


async def login_broker_with_credentials(broker_type: str = "", credentials: dict | None = None) -> dict[str, object]:
    """
    Runtime broker login initiated by the connected Client.

    This is intentionally separate from SM config hot-reload: the Client can enter
    the actual broker username/password after the TS connection is locked to it.
    """
    global _current_broker, _current_broker_type, _local_config_version

    runtime_credentials = dict(credentials or {})
    cfg: dict | None = None
    if not broker_type:
        cfg = await _pull_config_from_sm()
        if cfg:
            broker_type = str(cfg.get("broker_type") or "")
            _local_config_version = int(cfg.get("config_version", _local_config_version) or 0)

    if not broker_type:
        return {
            "success": False,
            "code": "BROKER_CONFIG_MISSING",
            "message": "Broker type is not configured",
            "retryable": False,
        }

    if cfg is None:
        cfg = await _pull_config_from_sm()
        if cfg:
            _local_config_version = int(cfg.get("config_version", _local_config_version) or 0)

    base_credentials = {}
    if isinstance(cfg, dict):
        base_credentials = dict(cfg.get("credentials") or {})

    merged_credentials: dict = {}
    for key in ("account_number", "secret"):
        if base_credentials.get(key):
            merged_credentials[key] = base_credentials.get(key)
    merged_credentials.update(runtime_credentials)

    try:
        broker = BrokerFactory.create(broker_type)
        normalized = broker.normalize_credentials(merged_credentials)
        ok = await broker.connect(normalized)
        if not ok:
            err = broker.get_connection_error() if hasattr(broker, "get_connection_error") else {}
            return {
                "success": False,
                "code": str(err.get("code") or "BROKER_LOGIN_FAILED"),
                "message": str(err.get("message") or "Broker login failed"),
                "retryable": bool(err.get("retryable", False)),
                "challenge_token": str(err.get("challenge_token") or ""),
                "challenge": err.get("challenge") if isinstance(err.get("challenge"), dict) else {},
            }

        await _destroy_broker()
        _current_broker = broker
        _current_broker_type = broker_type
        broker.set_quote_callback(_on_quote_from_broker)
        _reset_connect_retry_state()
        _start_auto_reconnect()
        _broadcast_status(broker_type, "connected")
        return {
            "success": True,
            "code": "BROKER_LOGIN_OK",
            "message": "Broker login active",
            "retryable": False,
        }
    except Exception as e:
        return {
            "success": False,
            "code": "BROKER_LOGIN_FAILED",
            "message": str(e)[:240] or "Broker login failed",
            "retryable": True,
        }


async def logout_current_broker() -> None:
    await _destroy_broker()


async def init_broker() -> bool:
    """
    场景A: Trader_Server 注册审批通过后调用，首次从 SM 拉取配置并连接券商
    
    Returns:
        是否成功初始化
    """
    global _current_broker, _current_broker_type, _local_config_version

    if not state.token or not state.server_id:
        log.warning("init_broker: no token/server_id, skip")
        return False

    cfg = await _pull_config_from_sm()
    if not cfg:
        log.error("init_broker: failed to pull config from SM")
        return False

    broker_type = cfg.get("broker_type", "")
    credentials = cfg.get("credentials", {})
    _local_config_version = cfg.get("config_version", 0)

    try:
        broker = BrokerFactory.create(broker_type)
        normalized = broker.normalize_credentials(credentials)
        ok = await broker.connect(normalized)
        if not ok:
            err = _record_connect_failure(broker, "init")
            log.error(f"init_broker: {broker_type} connect failed [{err.get('code')}]: {err.get('message')}")
            _broadcast_status(broker_type, "connect_failed")
            return False

        _current_broker = broker
        _current_broker_type = broker_type
        broker.set_quote_callback(_on_quote_from_broker)
        _reset_connect_retry_state()
        _start_auto_reconnect()

        log.info(f"init_broker: {broker_type} initialized successfully (version={_local_config_version})")
        _broadcast_status(broker_type, "connected")
        return True

    except Exception as e:
        log.error(f"init_broker: exception: {e}")
        _broadcast_status(broker_type or broker_type, "error")
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

    return await _do_hot_reload(trigger="config_change")


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
            resp = await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=60))

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
            with urllib.request.urlopen(req, timeout=15) as resp:
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


async def _do_hot_reload(trigger: str = "auto") -> bool:
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
        await _destroy_broker()
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
        broker.set_quote_callback(_on_quote_from_broker)
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
    """销毁当前 Broker 实例"""
    global _current_broker, _current_broker_type
    
    if _current_broker:
        try:
            await _current_broker.disconnect()
        except Exception as e:
            log.warning(f"_destroy_broker disconnect error: {e}")
        _current_broker = None
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
    msg = {
        "type": "QUOTE_DATA",
        "id": f"quote_{int(time.time() * 1000)}",
        "timestamp": int(time.time() * 1000),
        "payload": {
            **quote,
            "broker_type": _current_broker_type,
        },
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
            "broker_type": broker_type,
            "status": status,
            "config_version": _local_config_version,
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

    _reconnect_enabled = False

    tasks = []
    if _config_event_task and not _config_event_task.done():
        _config_event_task.cancel()
        tasks.append(_config_event_task)
    if _auto_reconnect_task and not _auto_reconnect_task.done():
        _auto_reconnect_task.cancel()
        tasks.append(_auto_reconnect_task)

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

    await _destroy_broker()
    log.info("Config sync service shut down")
