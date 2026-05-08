"""
Server_economic — 经济数据子服务端 主入口

启动方式:
    python Server_economic/main.py
    python -m Server_economic.main
    uvicorn Server_economic.main:app --host 0.0.0.0 --port 8900

命令行参数:
    --manager-url   Server_manager 地址 (默认 http://127.0.0.1:8800)
    --node-name     节点名称 (默认 economic-node-01)
    --region        区域 (默认 CN)
    --ws-port       WebSocket 监听端口 (默认 8900)
    --skip-register 跳过自动注册（使用已保存的凭证）
    --auto-approve  非交互模式（注册后不等待审批，需已有凭证或配合 SM 自动审批）

启动流程:
  1. 初始化日志和配置
  2. 检查本地凭证（config.json / .register_state.json）
  3. 若无凭证 → 执行完整注册流程（ping → register → SSE wait）
  4. 注册成功 → 启动心跳循环
  5. 启动 FastAPI + WebSocket 服务
  6. 进入运行状态，接受 Client 连接
"""

import argparse
import asyncio
import logging
import os
import signal
import sys

# ── 包路径修正（兼容 python main.py 和 python -m 两种启动方式）──
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

# 项目根目录必须可搜索（uvicorn -m 启动需要）
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# 直接执行 main.py 时，Python 不设 __package__，导致子模块的相对导入（from ..config）越界
# 这里手动补齐，使 from .xxx / from .services.xxx 均可正常解析
if __name__ == "__main__" and __package__ is None:
    __package__ = "Server_economic"

from fastapi import FastAPI, WebSocket, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 全部使用相对导入，与子模块风格一致（依赖上面的 __package__ 修正）
from .config import (
    state,
    DEFAULT_MANAGER_URL,
    DEFAULT_NODE_NAME,
    DEFAULT_REGION,
    DEFAULT_WS_PORT,
    init_logging,
)
from .services.registration import (
    run_full_registration,
    submit_registration,
    await_approval,
    check_and_restore_session,
    test_connection,
)
from .services.heartbeat import HeartbeatSender
from .services.economic_data import get_all_indicators, generate_summary_report
from .network.ws_server import handle_client_connection, broadcast_message


# ── FastAPI App ───────────────────────────────────────────────────────────

app = FastAPI(
    title="Server_economic",
    description="经济数据子服务端 — 业务执行侧组件",
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


# ── HTTP API 端点 ────────────────────────────────────────────────────────

# 模板文件路径
_TEMPLATE_PATH = os.path.join(_SCRIPT_DIR, "templates", "dashboard.html")


@app.get("/", response_class=HTMLResponse)
async def root():
    """根路径 — 操作控制面板（注册 + 状态仪表盘）"""
    if os.path.exists(_TEMPLATE_PATH):
        with open(_TEMPLATE_PATH, "r", encoding="utf-8") as f:
            return f.read()
    # 降级：模板不存在时返回简单状态
    return HTMLResponse(
        content="<h1>Server_economic</h1><p>Template not found.</p>",
        status_code=200,
    )


# ── 注册 API（供前端 UI 调用）──

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
    return {"ok": False, "error": msg}


@app.post("/api/register/submit")
async def api_register_submit(body: dict):
    """
    Step B: 提交注册请求
    接收前端表单数据并转发给 SM
    """
    log = logging.getLogger("server_economic.main")

    # 打印收到的完整 payload，方便调试
    log.info(f"[Register Submit] Received body: {body}")
    log.info(f"[Register Submit] Current state.manager_url = {state.manager_url}")

    try:
        result = submit_registration(
            node_name=body.get("node_name"),
            region=body.get("region"),
            host=body.get("host"),
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
            }
        return {"ok": False, "error": "Registration submission failed (SM returned no result)"}

    except Exception as e:
        log.error(f"[Register Submit] Exception: {e}", exc_info=True)
        return {"ok": False, "error": f"Internal error: {e}"}


@app.get("/api/register/await-approval")
async def api_await_approval(request_id: str = Query(...)):
    """
    Step C: SSE 等待审核结果
    代理 SM 的 SSE 流到前端，同时在后端解析 approved 事件并保存凭证/更新状态
    """
    from fastapi.responses import StreamingResponse
    import json as _json_mod
    import urllib.parse
    import urllib.request
    from .config import save_config, clear_register_state

    params = urllib.parse.urlencode({"request_id": request_id})
    url = f"{state.manager_url.rstrip('/')}/nodes/await-approval?{params}"

    log = logging.getLogger("server_economic.main")
    log.info(f"[Await Approval] Connecting to SM: {url}")

    async def _sse_generator():
        loop = asyncio.get_event_loop()
        buffer = b""
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            resp = await loop.run_in_executor(
                None, lambda: urllib.request.urlopen(req, timeout=3600)
            )
            while True:
                raw = await loop.run_in_executor(None, resp.read, 4096)
                if not raw:
                    break
                # 先原样转发给前端（保持流式实时性）
                yield raw
                # 再加入缓冲区用于后端解析
                buffer += raw

                # 按双换行分割 SSE 事件，尝试提取 data
                while b"\n\n" in buffer:
                    idx = buffer.index(b"\n\n")
                    event_block = buffer[:idx]
                    buffer = buffer[idx + 2:]

                    try:
                        text = event_block.decode("utf-8", errors="replace")
                        for line in text.split("\n"):
                            if line.startswith("data:"):
                                data_str = line[5:].strip()
                                if data_str:
                                    result = _json_mod.loads(data_str)
                                    if result.get("approved"):
                                        sid = result.get("server_id", "")
                                        tok = result.get("token", "")
                                        log.info(f"[Await Approval] ★ APPROVED! server_id={sid}")
                                        save_config({
                                            "server_id": sid,
                                            "token": tok,
                                            "manager_url": state.manager_url,
                                            "node_name": state.node_name,
                                            "region": state.region,
                                        })
                                        clear_register_state()
                                        state.server_id = sid
                                        state.token = tok
                                        state.status = "approved"
                                        # 启动心跳
                                        global _heartbeat
                                        if not _heartbeat:
                                            from .services.heartbeat import HeartbeatSender
                                            _heartbeat = HeartbeatSender(interval=30)
                                            await _heartbeat.start()
                                    else:
                                        reason = result.get("reason", "")
                                        log.warning(f"[Await Approval] REJECTED: {reason}")
                                        clear_register_state()
                                        state.status = "rejected"
                                break
                    except (ValueError, KeyError):
                        pass  # 非 JSON 事件（如心跳注释），忽略

        except Exception as e:
            log.error(f"[Await Approval] Stream error: {e}", exc_info=True)
            error_data = _json_mod.dumps({
                "approved": False,
                "reason": f"SSE stream error: {e}",
                "message": "连接中断",
            })
            yield f"data: {error_data}\n\n".encode()

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
    state.status = "uninitialized"
    state.heartbeat_ok = False

    log.info(f"Credentials cleared: {cleared}")
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
        "service": "server_economic",
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

    hb = _heartbeat.stats if _heartbeat else {}
    # 统一 heartbeat 字段格式，适配前端
    hb_data = {
        "ok": state.heartbeat_ok,
        "total": hb.get("total", 0),
        "ok_count": hb.get("ok", 0),
        "fail": hb.get("fail", 0),
        "backoff": hb.get("backoff", 1),
        "running": hb.get("running", False),
        "interval": getattr(_heartbeat, 'interval', 30) if _heartbeat else 30,
    }

    return {
        "service": "server_economic",
        "version": __import__("Server_economic").__dict__.get("__version__", "1.0.0"),
        "registration": {
            "status": state.status,
            "server_id": state.server_id,
            "node_name": state.node_name,
            "region": state.region,
            "manager_url": state.manager_url,
            "has_credentials": bool(state.token and state.server_id),
        },
        "heartbeat": hb_data,
        "connections": get_connection_count(),
        "indicators": list(get_all_indicators().keys()),
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
    log = logging.getLogger("server_economic.main")

    _se_pkg = __import__("Server_economic")
    _ver = getattr(_se_pkg, "__version__", "1.0.0")

    # 解析启动参数
    args = parse_args_from_env_or_default()

    if args.manager_url:
        state.manager_url = args.manager_url

    print()
    print("=" * 60)
    print("   Server_economic v%s — Economic Data Sub-server" % _ver)
    print("=" * 60)

    # 1) 尝试恢复已有凭证（不自动注册）
    has_creds = check_and_restore_session()

    if has_creds:
        print("  [OK] 已加载保存的凭证:")
        print("       server_id : %s" % state.server_id)
        print("       node_name : %s" % state.node_name)
        print("       region    : %s" % state.region)
        print()

        # 验证 SM 连通性
        ok, msg = test_connection()
        if not ok:
            print("  [WARN] SM 暂时不可达 (%s)，稍后重试" % msg)
        else:
            print("  [OK] SM 连通正常")
    else:
        print("  [*] 未发现已保存的凭证")
        print("      请通过 Web 界面完成注册: http://0.0.0.0:%d/" % args.ws_port)

    # 2) 有凭证则启动心跳循环
    if state.token and state.status in ("approved", "online", "running"):
        _heartbeat = HeartbeatSender(interval=30)
        await _heartbeat.start()

        await asyncio.sleep(1.5)
        ok, msg = _heartbeat.send_once_sync()
        label = "OK" if ok else "FAIL (%s)" % msg
        print("  [%s] 首次心跳: %s" % (">" if ok else "!", label))

        state.status = "running"
    elif not has_creds:
        state.status = "uninitialized"

    # 3) 输出启动信息
    ws_port = args.ws_port or DEFAULT_WS_PORT
    print()
    print("-" * 60)
    print("  状态     : %s" % state.status.upper())
    print("  节点名称 : %s" % (state.node_name or "(未设置)"))
    print("  区域     : %s" % (state.region or "(未设置)"))
    print("  SM 地址  : %s" % state.manager_url)
    print("")
    print("  Web 控制台 : http://0.0.0.0:%d/" % ws_port)
    print("  WS 端点   : ws://0.0.0.0:%d/ws" % ws_port)
    print("  API 状态   : http://0.0.0.0:%d/api/status" % ws_port)
    print("-" * 60)
    print()


@app.on_event("shutdown")
async def on_shutdown():
    """应用关闭时清理资源"""
    log = logging.getLogger("server_economic.main")
    log.info("Shutting down...")

    global _heartbeat
    if _heartbeat:
        _heartbeat.stop()
        await _heartbeat.wait_stopped()

    state.request_shutdown()
    log.info("Shutdown complete.")


# ── 命令行工具 ────────────────────────────────────────────────────────

def _build_arg_parser():
    """构建命令行参数解析器"""
    p = argparse.ArgumentParser(description="Server_economic 子服务端")
    p.add_argument("--manager-url", default=DEFAULT_MANAGER_URL,
                   help=f"SM 地址 (默认: {DEFAULT_MANAGER_URL})")
    p.add_argument("--node-name", default=DEFAULT_NODE_NAME,
                   help=f"节点名称 (默认: {DEFAULT_NODE_NAME})")
    p.add_argument("--region", default=DEFAULT_REGION,
                   help=f"区域 (默认: {DEFAULT_REGION})")
    p.add_argument("--ws-port", type=int, default=DEFAULT_WS_PORT,
                   help=f"WS 端口 (默认: {DEFAULT_WS_PORT})")
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
        manager_url=os.environ.get("SE_MANAGER_URL", DEFAULT_MANAGER_URL),
        node_name=os.environ.get("SE_NODE_NAME", DEFAULT_NODE_NAME),
        region=os.environ.get("SE_REGION", DEFAULT_REGION),
        ws_port=int(os.environ.get("SE_WS_PORT", str(DEFAULT_WS_PORT))),
        skip_register=os.environ.get("SE_SKIP_REGISTER", "").lower() in ("1", "true"),
        auto_approve=False,
    )


# ── 直接运行 ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = _build_arg_parser()
    args = parser.parse_args()

    # 将命令行参数写入环境变量供 FastAPI startup 使用
    os.environ["SE_MANAGER_URL"] = args.manager_url
    os.environ["SE_NODE_NAME"] = args.node_name
    os.environ["SE_REGION"] = args.region
    os.environ["SE_WS_PORT"] = str(args.ws_port)
    if args.skip_register:
        os.environ["SE_SKIP_REGISTER"] = "1"

    import uvicorn
    uvicorn.run(
        "Server_economic.main:app",
        host="0.0.0.0",
        port=args.ws_port,
        reload=False,
        log_level="info",
    )
