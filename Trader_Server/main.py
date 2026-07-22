"""
Trader_Server — 交易服务子服务端 主入口

启动方式:
    python Trader_Server/main.py        # 自动启动 FastAPI + 桌面 GUI
    python -m Trader_Server.main         # 同上
    uvicorn Trader_Server.main:app      # 仅启动 API 服务（无 GUI）

命令行参数:
    --manager-url   Server_manager 地址 (默认 https://scjrdomain.com)
    --node-name     节点名称 (默认 trader-node-01)
    --broker-type   券商类型 (默认 TT)
    --bind-host     本机监听地址 (默认 127.0.0.1)
    --ws-port       WebSocket 监听端口 (默认 8900)

启动流程:
  1. 后台线程启动 FastAPI + WebSocket 服务
  2. 主线程打开桌面控制面板 GUI
  3. 用户通过 GUI 完成注册、查看状态、管理凭证
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

# ── 包路径修正（兼容 python main.py 和 python -m 两种启动方式）──
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

# 项目根目录必须可搜索（uvicorn -m 启动需要）
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 直接执行 main.py 时，Python 不设 __package__，导致子模块的相对导入（from ..config）越界
# 这里手动补齐，使 from .xxx / from .services.xxx 均可正常解析
if __name__ == "__main__" and __package__ is None:
    __package__ = "Trader_Server"

from fastapi import FastAPI, WebSocket, Query, Request

from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# 全部使用相对导入，与子模块风格一致（依赖上面的 __package__ 修正）
from .config import (
    state,
    DEFAULT_MANAGER_URL,
    DEFAULT_NODE_NAME,
    DEFAULT_REGION,
    DEFAULT_BIND_HOST,
    DEFAULT_WS_PORT,
    DEFAULT_HEARTBEAT_INTERVAL,
    TS_CADDY_REQUIRED,
    init_logging,
    read_recent_error_lines,
    LOG_DIR,
    ERROR_LOG_FILE,
)
from .services.registration import (
    run_full_registration,
    submit_registration,
    cancel_registration_request,
    await_approval,
    check_and_restore_session,
    test_connection,
)

from .services.heartbeat import HeartbeatSender
from .services.economic_data import get_all_indicators, generate_summary_report
from .network.ws_server import handle_client_connection, broadcast_message, force_disconnect_all_clients



# ── FastAPI App ───────────────────────────────────────────────────────────

app = FastAPI(
    title="Trader_Server",
    description="交易服务子服务端 — 业务执行侧组件",
    version="1.0.0",
)

# CORS（开发环境允许跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 全局心跳实例
_heartbeat: HeartbeatSender | None = None
_STARTED_AT = time.time()


# ── HTTP API 端点 ────────────────────────────────────────────────────────


# ── 注册 API（供桌面 GUI 调用）──

@app.post("/api/register/ping")
async def api_register_ping(body: dict):
    """
    Step A: 测试到 Server_manager 的连通性
    前端调用，验证 SM 地址是否可达
    """
    import time as _time
    manager_url = body.get("manager_url", state.manager_url)
    if manager_url:
        state.manager_url = manager_url

    t0 = _time.time()
    ok, msg = test_connection()
    latency = round((_time.time() - t0) * 1000)

    if ok:
        return {"ok": True, "latency": latency, "message": msg}

    err_type = "SM_PING_FAILED"
    if "WinError 10061" in str(msg) or "urlopen error" in str(msg):
        err_type = "SM_UNREACHABLE"
    return {
        "ok": False,
        "error": msg,
        "error_type": err_type,
        "manager_url": manager_url,
    }



@app.post("/api/register/submit")
async def api_register_submit(body: dict):
    """
    Step B: 提交注册请求
    接收前端表单数据并转发给 SM
    """
    log = logging.getLogger("trader_server.main")

    # 打印收到的完整 payload，方便调试
    log.info(f"[Register Submit] Received body: {body}")
    log.info(f"[Register Submit] Current state.manager_url = {state.manager_url}")

    # 已注册节点不允许重复提交注册
    if state.server_id and state.token and state.status in ("approved", "running", "online"):
        return {
            "ok": False,
            "error": "节点已完成注册，请勿重复提交",
            "error_type": "ALREADY_REGISTERED",
            "server_id": state.server_id,
        }

    try:

        result = submit_registration(
            node_name=body.get("node_name"),
            region=body.get("region"),
            host=body.get("host"),
            public_ip=body.get("public_ip"),
            capabilities=body.get("capabilities"),
            contact=body.get("contact"),
            description=body.get("description"),
        )
        log.info(f"[Register Submit] submit_registration returned: {result}")

        if result:
            return {
                "ok": True,
                "request_id": result.get("request_id"),
                "expire_at": result.get("expire_at"),
                "resumed": bool(result.get("resumed")),
            }
        return {"ok": False, "error": "Registration submission failed (SM returned no result)"}

    except Exception as e:
        log.error(f"[Register Submit] Exception: {e}", exc_info=True)
        return {"ok": False, "error": f"Internal error: {e}"}


@app.post("/api/register/cancel")
async def api_register_cancel(body: dict):
    """取消/废弃一次注册申请（用于 GUI 等待审批时用户主动取消）。"""
    request_id = (body.get("request_id") or "").strip()
    reason = (body.get("reason") or "node_cancelled_by_user").strip()
    force_discard_approved = bool(body.get("force_discard_approved", True))

    if not request_id:
        return {"ok": False, "error": "request_id is required"}

    manager_url = body.get("manager_url", state.manager_url)
    if manager_url:
        state.manager_url = manager_url

    log = logging.getLogger("trader_server.main")
    result = cancel_registration_request(
        request_id=request_id,
        reason=reason,
        force_discard_approved=force_discard_approved,
    )
    log.info(f"[Register Cancel] request_id={request_id}, result={result}")
    return result


@app.get("/api/register/pre-approve-check")
async def api_register_pre_approve_check(request_id: str = Query(...)):
    """供 SM 审批前问询：确认该 request 仍有效，避免审批废弃申请。"""
    from .config import load_register_state

    rid = (request_id or "").strip()
    if not rid:
        return {"ok": False, "can_approve": False, "reason": "request_id is required"}

    reg_state = load_register_state()
    if not reg_state:
        return {
            "ok": True,
            "can_approve": False,
            "reason": "request_abandoned_or_not_waiting",
        }

    waiting_rid = (reg_state.get("request_id") or "").strip()
    if waiting_rid != rid:
        return {
            "ok": True,
            "can_approve": False,
            "reason": "request_mismatch_or_abandoned",
            "current_request_id": waiting_rid,
        }

    if state.server_id and state.token and state.status in ("approved", "running", "online"):
        return {
            "ok": True,
            "can_approve": False,
            "reason": "already_registered",
            "server_id": state.server_id,
        }

    return {"ok": True, "can_approve": True, "reason": "waiting_approval"}




@app.get("/api/register/await-approval")
async def api_await_approval(request_id: str = Query(...)):


    """
    Step C: SSE ??????
    ?? SM ? SSE ???????????? approved ???????/????
    """
    from fastapi.responses import StreamingResponse
    import json as _json_mod
    import urllib.parse
    import urllib.request
    from .config import save_config, clear_register_state

    params = urllib.parse.urlencode({"request_id": request_id})
    url = f"{state.manager_url.rstrip('/')}/nodes/await-approval?{params}"

    log = logging.getLogger("trader_server.main")
    log.info(f"[Await Approval] Connecting to SM: {url}")

    async def _sse_generator():
        loop = asyncio.get_event_loop()
        event_lines: list[str] = []
        resp = None

        async def _apply_terminal_event(result: dict) -> bool:
            approved = bool(result.get("approved"))
            if approved:
                sid = result.get("server_id", "")
                tok = result.get("token", "")
                log.info(f"[Await Approval] APPROVED server_id={sid}")
                config_saved = save_config({
                    "server_id": sid,
                    "token": tok,
                    "manager_url": state.manager_url,
                    "node_name": state.node_name,
                    "region": state.region,
                    "public_ip": result.get("public_ip") or state.public_ip,
                    "assigned_domain": result.get("assigned_domain", ""),
                    "public_endpoint": result.get("public_endpoint", ""),
                })
                if config_saved:
                    clear_register_state()
                else:
                    log.error("[Await Approval] Approved credentials could not be persisted")
                state.server_id = sid
                state.token = tok
                state.public_ip = result.get("public_ip") or state.public_ip
                state.assigned_domain = result.get("assigned_domain", "")
                state.public_endpoint = result.get("public_endpoint", "")
                state.status = "approved"
                if state.assigned_domain:
                    try:
                        from .services.caddy_manager import configure_and_start_caddy
                        caddy_result = await asyncio.to_thread(
                            configure_and_start_caddy,
                            state.assigned_domain,
                        )
                        if caddy_result.get("ok"):
                            log.info("[Await Approval] Caddy setup: %s", caddy_result)
                        else:
                            log.error("[Await Approval] Caddy setup failed: %s", caddy_result)
                            if TS_CADDY_REQUIRED:
                                raise RuntimeError(
                                    f"Required Caddy setup failed: {caddy_result.get('reason', 'unknown error')}"
                                )
                    except Exception as caddy_exc:
                        if TS_CADDY_REQUIRED:
                            raise
                        log.warning("[Await Approval] Caddy setup failed: %s", caddy_exc)
                global _heartbeat
                if not _heartbeat:
                    from .services.heartbeat import HeartbeatSender
                    _heartbeat = HeartbeatSender(interval=DEFAULT_HEARTBEAT_INTERVAL)
                if not _heartbeat.stats.get("running", False):
                    await _heartbeat.start()
                state.status = "running"

                from .services.config_sync import init_broker, start_config_event_listener
                try:
                    broker_ok = await init_broker()
                    if broker_ok:
                        log.info("[Await Approval] Broker initialized OK")
                    else:
                        log.warning("[Await Approval] Broker init failed (will retry via heartbeat)")
                    start_config_event_listener()
                except Exception as be:
                    log.error(f"[Await Approval] Broker init exception: {be}")
                return True

            reason = result.get("reason", "")
            log.warning(f"[Await Approval] REJECTED: {reason}")
            clear_register_state()
            state.status = "rejected"
            return True

        async def _handle_event_block(lines: list[str]) -> bool:
            if not lines:
                return False
            data_lines: list[str] = []
            for raw_line in lines:
                if raw_line.startswith("data:"):
                    data_lines.append(raw_line[5:].strip())
            if not data_lines:
                return False
            data_str = "\\n".join(data_lines).strip()
            if not data_str:
                return False
            try:
                result = _json_mod.loads(data_str)
            except (ValueError, KeyError):
                return False
            if "approved" in result or result.get("reason"):
                return await _apply_terminal_event(result)
            return False

        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            resp = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=3600)
            )
            while True:
                raw_line = await loop.run_in_executor(None, resp.readline)
                if not raw_line:
                    if await _handle_event_block(event_lines):
                        return
                    break

                yield raw_line
                text_line = raw_line.decode("utf-8", errors="replace").rstrip("\\r\\n")
                if text_line == "":
                    if await _handle_event_block(event_lines):
                        return
                    event_lines = []
                else:
                    event_lines.append(text_line)

        except Exception as e:
            log.error(f"[Await Approval] Stream error: {e}", exc_info=True)
            error_data = _json_mod.dumps({
                "approved": False,
                "reason": f"SSE stream error: {e}",
                "message": "????",
            })
            yield f"data: {error_data}\\n\\n".encode()
        finally:
            if resp is not None:
                await loop.run_in_executor(None, resp.close)

    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/register/clear")
async def api_register_clear():
    """清除已保存的注册凭证"""
    from .config import clear_register_state as _clr, CONFIG_FILE

    await force_disconnect_all_clients(reason="ts_credentials_cleared")
    global _heartbeat
    if _heartbeat:
        _heartbeat.stop()
        await _heartbeat.wait_stopped()
        _heartbeat = None
    try:
        from .services.config_sync import logout_current_broker
        await logout_current_broker()
    except Exception as exc:
        logging.getLogger("trader_server.main").warning(
            "Broker cleanup before credential clear failed: %s",
            exc,
        )

    cleared = []
    if CONFIG_FILE.exists():
        try:
            CONFIG_FILE.unlink()
            cleared.append("config.json")
        except OSError:
            pass
    _clr()
    if _clear_register_state_file():
        cleared.append(".register_state.json")

    # 重置运行时状态
    state.server_id = ""
    state.token = ""
    state.public_ip = ""
    state.assigned_domain = ""
    state.public_endpoint = ""
    state.status = "uninitialized"
    state.heartbeat_ok = False

    logging.getLogger("trader_server.main").info(f"Credentials cleared: {cleared}")
    return {"ok": True, "cleared": cleared, "message": "凭证已清除"}


def _clear_register_state_file() -> bool:
    """辅助：清除 .register_state.json"""
    from .config import REGISTER_STATE_FILE
    if REGISTER_STATE_FILE.exists():
        try:
            REGISTER_STATE_FILE.unlink()
            return True
        except OSError:
            pass
    return False


@app.get("/health")
async def health_check():
    """健康检查端点"""
    return {
        "status": "ok",
        "service": "trader_server",
        "node_name": state.node_name,
        "server_id": state.server_id,
        "registration_status": state.status,
        "heartbeat_ok": state.heartbeat_ok,
        "uptime_s": int(asyncio.get_event_loop().time()),
    }


@app.get("/api/status")
async def api_status():
    """节点状态详情（供前端仪表盘轮询）"""
    from .network.ws_server import get_connection_count

    from .services.config_sync import get_broker_status
    broker_detail = get_broker_status()

    hb = _heartbeat.stats if _heartbeat else {}
    # 统一 heartbeat 字段格式，适配前端
    hb_data = {
        "ok": state.heartbeat_ok,
        "total": hb.get("total", 0),
        "ok_count": hb.get("ok", 0),
        "fail": hb.get("fail", 0),
        "backoff": hb.get("backoff", 1),
        "running": hb.get("running", False),
        "interval": getattr(_heartbeat, 'interval', DEFAULT_HEARTBEAT_INTERVAL) if _heartbeat else DEFAULT_HEARTBEAT_INTERVAL,
    }

    error_lines = read_recent_error_lines(1000)
    today_log = LOG_DIR / f"ts_{datetime.now().strftime('%Y%m%d')}.log"

    return {
        "service": "trader_server",
        "version": __import__("Trader_Server").__dict__.get("__version__", "1.0.0"),
        "registration": {
            "status": state.status,
            "server_id": state.server_id,
            "node_name": state.node_name,
            "region": state.region,
            "manager_url": state.manager_url,
            "public_ip": state.public_ip,
            "assigned_domain": state.assigned_domain,
            "public_endpoint": state.public_endpoint,
            "has_credentials": bool(state.token and state.server_id),
        },
        "heartbeat": hb_data,
        "connections": get_connection_count(),
        "indicators": list(get_all_indicators().keys()),
        "broker_status": ("connected" if state.broker_connected
                           else (f"{state.broker_type}" if state.broker_type else "-")),
        "broker_detail": broker_detail,
        "runtime": {
            "started_at": datetime.fromtimestamp(_STARTED_AT, tz=timezone.utc).isoformat(),
            "uptime_seconds": int(time.time() - _STARTED_AT),
            "shutting_down": state.is_shutting_down,
        },
        "log_health": {
            "runtime_log_exists": today_log.exists(),
            "runtime_log_bytes": today_log.stat().st_size if today_log.exists() else 0,
            "error_log_exists": ERROR_LOG_FILE.exists(),
            "error_log_bytes": ERROR_LOG_FILE.stat().st_size if ERROR_LOG_FILE.exists() else 0,
            "recent_error_lines": len(error_lines),
        },
    }


@app.get("/api/economic-data")
async def api_economic_data(indicator: str | None = None):
    """经济数据查询 API"""
    from .services.economic_data import get_indicator, get_all_indicators
    if indicator:
        data = get_indicator(indicator)
        if not data:
            return {"ok": False, "error": f"Unknown indicator: {indicator}"}
        return {"ok": True, "data": data}
    return {"ok": True, "data": get_all_indicators()}


@app.get("/api/summary")
async def api_summary():
    """经济数据摘要报告"""
    report = generate_summary_report()
    return {"ok": True, "report": report}


@app.get("/api/logs")
async def api_logs(limit: int = Query(100, ge=1, le=500)):
    """消息日志查询（供 GUI 运行日志面板使用）"""
    from .services.message_log import get_recent, get_stats
    return {
        "ok": True,
        "logs": get_recent(limit),
        "stats": get_stats(),
    }


@app.get("/api/logs/errors")
async def api_error_logs(limit: int = Query(200, ge=1, le=1000)):
    """Return recent TS runtime error logs for local troubleshooting."""
    return {"ok": True, "lines": read_recent_error_lines(limit)}


@app.post("/api/logs/clear")
async def api_logs_clear():
    """清空消息日志"""
    from .services.message_log import clear as clear_logs
    clear_logs()
    return {"ok": True}


@app.post("/api/admin/force-disconnect")
async def api_admin_force_disconnect(request: Request):
    """仅供 SM 调用：强制断开当前节点上的 Client WS 连接"""
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    if not token or token != (state.token or ""):
        return JSONResponse(status_code=401, content={"ok": False, "error": "Unauthorized"})

    body = await request.json() if await request.body() else {}
    reason = str(body.get("reason") or "admin_force_release")
    result = await force_disconnect_all_clients(reason=reason)
    return {"ok": True, **result}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """Client WebSocket 连接入口"""
    await handle_client_connection(ws)



# ── 启动事件 ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def on_startup():
    """应用启动时执行初始化（UI 模式：不自动注册，由用户通过界面操作）"""
    global _heartbeat

    init_logging("INFO")
    log = logging.getLogger("trader_server.main")

    _ts_pkg = __import__("Trader_Server")
    _ver = getattr(_ts_pkg, "__version__", "1.0.0")

    # 解析启动参数
    args = parse_args_from_env_or_default()

    if args.manager_url:
        state.manager_url = args.manager_url

    print()
    print("=" * 60)
    print("   Trader_Server v%s — Trading Service Sub-server" % _ver)
    print("=" * 60)

    # 1) 尝试恢复已有凭证（不自动注册）
    has_creds = check_and_restore_session()

    if has_creds:
        print("  [OK] 已加载保存的凭证:")
        print("       server_id : %s" % state.server_id)
        print("       node_name : %s" % state.node_name)
        print("       broker_type: %s" % state.region)
        print()

        # 验证 SM 连通性
        ok, msg = test_connection()
        if not ok:
            print("  [WARN] SM 暂时不可达 (%s)，稍后重试" % msg)
        else:
            print("  [OK] SM 连通正常")
        if state.assigned_domain:
            try:
                from .services.caddy_manager import configure_and_start_caddy
                caddy_result = await asyncio.to_thread(
                    configure_and_start_caddy,
                    state.assigned_domain,
                )
                if caddy_result.get("ok"):
                    log.info("[Startup] Caddy setup: %s", caddy_result)
                else:
                    log.error("[Startup] Caddy setup failed: %s", caddy_result)
                    if TS_CADDY_REQUIRED:
                        raise RuntimeError(
                            f"Required Caddy setup failed: {caddy_result.get('reason', 'unknown error')}"
                        )
            except Exception as caddy_exc:
                if TS_CADDY_REQUIRED:
                    raise
                log.warning("[Startup] Caddy setup failed: %s", caddy_exc)
    else:
        print("  [*] 未发现已保存的凭证")
        print("      请通过桌面控制面板完成注册")

    # 2) 有凭证则启动心跳循环
    if state.token and state.status in ("approved", "online", "running"):
        _heartbeat = HeartbeatSender(interval=DEFAULT_HEARTBEAT_INTERVAL)
        await _heartbeat.start()

        await asyncio.sleep(1.5)
        ok, msg = _heartbeat.send_once_sync()
        label = "OK" if ok else "FAIL (%s)" % msg
        print("  [%s] 首次心跳: %s" % (">" if ok else "!", label))

        # 启动配置事件监听（配置快速生效）
        try:
            from .services.config_sync import start_config_event_listener
            start_config_event_listener()
        except Exception as ce:
            log.warning(f"start config event listener failed: {ce}")

        state.status = "running"
    elif not has_creds:
        state.status = "uninitialized"


    # 3) 输出启动信息
    ws_port = args.ws_port or DEFAULT_WS_PORT
    print()
    print("-" * 60)
    print("  状态     : %s" % state.status.upper())
    print("  节点名称 : %s" % (state.node_name or "(未设置)"))
    print("  券商类型 : %s" % (state.region or "(未设置)"))
    print("  SM 地址  : %s" % state.manager_url)
    print("")
    print("  桌面控制台 : 已自动启动 (PySide6 GUI)")
    bind_host = args.bind_host or DEFAULT_BIND_HOST
    print("  监听地址   : %s:%d" % (bind_host, ws_port))
    print("  WS 端点    : ws://%s:%d/ws" % (bind_host, ws_port))
    print("  API 状态   : http://%s:%d/api/status" % (bind_host, ws_port))
    print("-" * 60)
    print()


@app.on_event("shutdown")
async def on_shutdown():
    """Clean up resources during application shutdown."""
    log = logging.getLogger("trader_server.main")
    log.info("Shutting down...")

    state.request_shutdown()

    global _heartbeat
    if _heartbeat:
        _heartbeat.stop()
        await _heartbeat.wait_stopped()
        _heartbeat = None

    # Stop broker connection and config_sync services.
    try:
        from .services.config_sync import shutdown as shutdown_config
        await shutdown_config()
    except Exception as e:
        log.error(f"Config sync shutdown error: {e}")

    log.info("Shutdown complete.")



def _build_arg_parser():
    """构建命令行参数解析器"""
    p = argparse.ArgumentParser(description="Trader_Server 子服务端")
    p.add_argument("--manager-url", default=DEFAULT_MANAGER_URL,
                   help=f"SM 地址 (默认: {DEFAULT_MANAGER_URL})")
    p.add_argument("--node-name", default=DEFAULT_NODE_NAME,
                   help=f"节点名称 (默认: {DEFAULT_NODE_NAME})")
    p.add_argument("--broker-type", default=DEFAULT_REGION,
                   help=f"券商类型 (默认: {DEFAULT_REGION})")
    p.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT,
                   help=f"WS 端口 (默认: {DEFAULT_WS_PORT})")
    p.add_argument("--bind-host", default=DEFAULT_BIND_HOST,
                   help=f"本机监听地址 (默认: {DEFAULT_BIND_HOST})")
    p.add_argument("--skip-register", action="store_true",
                   help="跳过自动注册（需已有 config.json）")
    p.add_argument("--auto-approve", action="store_true",
                   help="非交互模式")
    return p


def parse_args_from_env_or_default():
    """
    从环境变量或默认值解析配置
    支持 uvicorn 启动时的环境变量传递
    """
    import os
    return argparse.Namespace(
        manager_url=os.environ.get("TS_MANAGER_URL", DEFAULT_MANAGER_URL),
        node_name=os.environ.get("TS_NODE_NAME", DEFAULT_NODE_NAME),
        broker_type=os.environ.get("TS_BROKER_TYPE", DEFAULT_REGION),
        bind_host=os.environ.get("TS_BIND_HOST", DEFAULT_BIND_HOST),
        ws_port=int(os.environ.get("TS_WS_PORT", str(DEFAULT_WS_PORT))),
        skip_register=os.environ.get("TS_SKIP_REGISTER", "").lower() in ("1", "true"),
        auto_approve=False,
    )


# ── 直接运行 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = _build_arg_parser()
    args = parser.parse_args()

    # 将命令行参数写入环境变量供 FastAPI startup 使用
    os.environ["TS_MANAGER_URL"] = args.manager_url
    os.environ["TS_NODE_NAME"] = args.node_name
    os.environ["TS_BROKER_TYPE"] = args.broker_type
    os.environ["TS_BIND_HOST"] = args.bind_host
    os.environ["TS_WS_PORT"] = str(args.ws_port)
    if args.skip_register:
        os.environ["TS_SKIP_REGISTER"] = "1"

    import uvicorn

    # 后台线程启动 FastAPI 服务（不阻塞 GUI）
    _server_thread = threading.Thread(
        target=uvicorn.run,
        args=("Trader_Server.main:app",),
        kwargs=dict(
            host=args.bind_host,
            port=args.ws_port,
            reload=False,
            log_level="warning",  # 降低日志噪音，GUI 中查看即可
        ),
        daemon=True,
    )
    _server_thread.start()

    # 等待服务就绪后启动桌面窗口
    print("[*] Starting server in background thread...")
    time.sleep(1.5)

    # 导入并启动桌面 GUI
    from .ui_qt.main_window import run as run_qt_ui
    raise SystemExit(run_qt_ui())
