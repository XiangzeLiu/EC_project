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

log = logging.getLogger("server_economic.config_sync")

# 全局单例: 当前活跃的 Broker 实例
_current_broker: BaseBrokerAPI | None = None
_current_broker_type: str = ""
_local_config_version: int = 0       # 本地缓存的 config_version
_auto_reconnect_task: asyncio.Task | None = None
_config_event_task: asyncio.Task | None = None
_reconnect_enabled: bool = True
_last_reload_trigger_ts: float = 0.0
_min_reload_interval_sec: float = 1.5


# ── 公共接口 ────────────────────────────────────────────────────

def get_current_broker() -> BaseBrokerAPI | None:
    """获取当前活跃的 Broker 实例（可能为 None）"""
    return _current_broker


def get_broker_status() -> dict:
    """
    获取当前券商连接状态摘要
    
    Returns:
        {
            "broker_type": str,
            "connected": bool,
            "config_version": int,
            "last_action": str,
        }
    """
    if not _current_broker:
        return {
            "broker_type": _current_broker_type or "none",
            "connected": False,
            "config_version": _local_config_version,
            "last_action": "not_initialized",
        }
    return {
        "broker_type": _current_broker.broker_type,
        "connected": _current_broker._connected,
        "config_version": _local_config_version,
        "last_action": "active",
    }


async def ensure_broker_connected() -> bool:
    """
    业务触发前保障券商已连接。

    关键策略：每次需要执行“登录/重连”时，均先向 SM 拉取最新配置。
    """
    global _current_broker

    if _current_broker:
        try:
            if await _current_broker.is_connected():
                return True
        except Exception:
            pass

    # 未连接或不可用：使用最新配置重建连接
    return await _do_hot_reload()


async def init_broker() -> bool:
    """
    场景A: SE 注册审批通过后调用，首次从 SM 拉取配置并连接券商
    
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

    # 创建并连接
    try:
        broker = BrokerFactory.create(broker_type)
        normalized = broker.normalize_credentials(credentials)
        ok = await broker.connect(normalized)
        if not ok:
            log.error(f"init_broker: {broker_type} connect failed")
            _broadcast_status(broker_type, "connect_failed")
            return False
        
        _current_broker = broker
        _current_broker_type = broker_type
        
        # 注册行情回调 → 推送给订阅的 Client
        broker.set_quote_callback(_on_quote_from_broker)
        
        # 启动自动重连监控
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

    return await _do_hot_reload()


async def force_reload() -> bool:
    """
    强制重新拉取配置并重建连接（管理员手动触发 reload 时调用）
    """
    log.info("force_reload: manual trigger")
    return await _do_hot_reload()


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


async def _do_hot_reload() -> bool:
    """
    执行热更新：拉取新配置 → 断开旧连接 → 创建新实例 → 连接
    
    Returns:
        重载是否成功
    """
    global _current_broker, _current_broker_type, _local_config_version
    
    cfg = await _pull_config_from_sm()
    if not cfg:
        log.error("_do_hot_reload: failed to pull new config")
        return False
    
    new_type = cfg.get("broker_type", "")
    new_creds = cfg.get("credentials", {})
    new_version = cfg.get("config_version", 0)
    
    old_type = _current_broker_type
    
    # 如果类型变了，需要先完全销毁旧实例
    if old_type and old_type != new_type:
        log.info(f"_do_hot_reload: broker type changing {old_type} → {new_type}, full recreate")
        await _destroy_broker()
    
    elif _current_broker:
        # 同类型更新：优雅切换
        log.info(f"_do_hot_reload: reconnecting {new_type} with new credentials...")
        try:
            await _current_broker.disconnect()
        except Exception:
            pass
    
    # 创建新实例
    try:
        broker = BrokerFactory.create(new_type)
        normalized = broker.normalize_credentials(new_creds)
        ok = await broker.connect(normalized)
        
        if not ok:
            log.error(f"_do_hot_reload: {new_type} connect failed after reload")
            _local_config_version = new_version  # 即使失败也更新版本，避免反复拉取
            _broadcast_status(new_type, "reload_failed")
            return False
        
        _current_broker = broker
        _current_broker_type = new_type
        _local_config_version = new_version
        broker.set_quote_callback(_on_quote_from_broker)
        
        # 确保重连任务运行
        _start_auto_reconnect()
        
        log.info(f"_do_hot_reload: {new_type} reloaded OK (version={new_version})")
        _broadcast_status(new_type, "reloaded")
        return True
        
    except Exception as e:
        log.error(f"_do_hot_reload: exception: {e}")
        _local_config_version = new_version
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
    
    策略:
      - 每 30s 检查一次 is_connected()
      - 失败后指数退避重试: 5s→10s→20s→30s(上限)
      - 连续失败 N 次后停止自动重连，等待管理员干预或下次心跳触发 reload
      - 重连成功后恢复正常监控
    """
    global _auto_reconnect_task
    
    consecutive_failures = 0
    max_failures = 10  # 连续失败 10 次后暂停自动重连
    base_delay = 5
    max_delay = 30
    
    while _reconnect_enabled and not state.is_shutting_down:
        try:
            await asyncio.sleep(30)  # 每 30 秒检查一次
            
            if not _current_broker:
                continue
            
            connected = await _current_broker.is_connected()
            
            if connected:
                # 连接正常，重置失败计数
                if consecutive_failures > 0:
                    log.info(
                        f"Auto-reconnect: connection recovered after "
                        f"{consecutive_failures} failures"
                    )
                    _broadcast_status(_current_broker_type, "reconnected")
                consecutive_failures = 0
                continue
            
            # 连接异常，尝试重连
            if consecutive_failures == 0:
                log.warning(
                    f"Auto-reconnect: {_current_broker_type} disconnected, "
                    f"attempting reconnect..."
                )
                _broadcast_status(_current_broker_type, "disconnected")
            
            consecutive_failures += 1
            
            if consecutive_failures > max_failures:
                log.error(
                    f"Auto-reconnect: gave up after {consecutive_failures} "
                    f"consecutive failures. Manual intervention required."
                )
                _broadcast_status(_current_broker_type, "abandoned")
                # 不退出循环，继续监控但不主动重连
                await asyncio.sleep(300)  # 5 分钟后再检查
                consecutive_failures = 0
                continue
            
            # 指数退避延迟
            delay = min(base_delay * (2 ** min(consecutive_failures - 1, 3)), max_delay)
            log.info(
                f"Auto-reconnect: attempt #{consecutive_failures} "
                f"(next in {delay}s if fails)..."
            )
            
            try:
                # 每次重连都从 SM 拉取最新配置，避免使用过期凭证
                ok = await _do_hot_reload()
                if ok:
                    log.info(f"Auto-reconnect: {_current_broker_type} reconnected!")
                    _broadcast_status(_current_broker_type, "reconnected")
                    consecutive_failures = 0
                    # 确保行情回调仍然注册
                    if _current_broker:
                        _current_broker.set_quote_callback(_on_quote_from_broker)
                else:
                    log.warning(f"Auto-reconnect: attempt #{consecutive_failures} failed")
            except Exception as e:
                log.warning(f"Auto-reconnect: attempt #{consecutive_failures} error: {e}")
            
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
            asyncio.ensure_future(ws_server.broadcast_message(msg))
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
    """关闭 config_sync 服务，释放所有资源"""
    global _reconnect_enabled, _auto_reconnect_task
    
    _reconnect_enabled = False
    
    if _auto_reconnect_task and not _auto_reconnect_task.done():
        _auto_reconnect_task.cancel()
        try:
            await _auto_reconnect_task
        except (asyncio.CancelledError, Exception):
            pass
        _auto_reconnect_task = None
    
    await _destroy_broker()
    log.info("Config sync service shut down")
