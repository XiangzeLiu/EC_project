"""
Server Manager - Main Entry Point
证券交易系统管理服务端启动入口

启动方式:
    python Server_manager/main.py          (直接运行)
    python -m Server_manager.main           (模块方式)
    uvicorn Server_manager.main:app --host 0.0.0.0 --port 8800

环境变量:
    SERVER_USERNAME   服务器登录用户名 (默认: admin)
    SERVER_PASSWORD   服务器登录密码 (默认: changeme123)
    SERVER_PORT       服务端口       (默认: 8800)
    TASTY_SECRET      Tastytrade Secret Token
    TASTY_TOKEN       Tastytrade Session Token
    IB_HOST           IB TWS 地址     (默认: 127.0.0.1)
    IB_PORT           IB TWS 端口     (默认: 7496)
"""

import os
import sys

# ── 支持直接运行脚本：将项目根目录和本包目录加入 sys.path ────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_SCRIPT_DIR)

if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import asyncio
import logging

# SSL 证书处理（解决 Windows 证书链不完整问题）
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import (
    SERVER_HOST, SERVER_PORT, session_store,
    quote_clients, subscribed_syms, is_configured, log,
)
import database
from database import init_db
from routers.auth_router import router as auth_router
from routers.order_router import router as order_router
from routers.position_router import router as position_router
from services.tastytrade_svc import SDK_OK
from services.quote_service import ib_preconnect, quote_stream_loop, broadcast_quote


# ── FastAPI App ───────────────────────────────────────────────────────────

app = FastAPI(
    title="Trading Server Manager",
    description=(
        "### 交易系统 Server Manager\n\n"
        "- **认证管理**：用户登录/登出，支持 JSON 文件 / 配置文件 / 数据库多级认证\n"
        "- **订单管理**：下单、撤单、活动订单查询、历史订单查询\n"
        "- **持仓查询**：当前持仓列表，含 Demo 模式模拟数据\n"
        "- **健康检查**：服务状态监控\n"
        "- **行情推送**：WebSocket 实时行情订阅\n\n"
        "---\n\n"
        "**运行模式**：DEMO（模拟数据） | LIVE（真实券商连接）"
    ),
    version="1.0.0",
    # 禁用内置 /docs，使用下方自定义版本（带语言切换）
    docs_url=None,
)

# CORS（开发环境允许跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册 API 路由
app.include_router(auth_router)
app.include_router(order_router)
app.include_router(position_router)


# ── Web 管理后台（Jinja2 模板）───────────────────────────────────────

_TEMPLATES_DIR = os.path.join(_SCRIPT_DIR, "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# 管理员会话存储（内存，重启失效；后续可迁移到 Redis）
_admin_sessions: dict[str, dict] = {}  # {session_id: {username, created_at}}

# 管理会话有效期：2 小时（与 Cookie max_age 保持一致）
_ADMIN_SESSION_MAX_AGE = 7200

# 后台清理间隔：每 30 分钟扫描一次过期 session
_ADMIN_CLEANUP_INTERVAL = 1800

_ADMIN_JSON_PATH = os.path.join(_SCRIPT_DIR, "admin.json")


def _load_admins() -> list[dict]:
    """从 admin.json 加载超级管理员列表"""
    try:
        with open(_ADMIN_JSON_PATH, "r", encoding="utf-8") as f:
            return __import__("json").load(f)
    except Exception:
        return []


def _get_session_id(request: Request) -> str | None:
    """从 Cookie 获取管理会话 ID"""
    return request.cookies.get("admin_sid")


def _is_session_expired(session: dict) -> bool:
    """判断单个管理会话是否已过期"""
    age = __import__("time").time() - session.get("created_at", 0)
    return age > _ADMIN_SESSION_MAX_AGE


def _is_admin_logged_in(request: Request) -> bool:
    """检查管理员是否已登录（含过期检查）"""
    sid = _get_session_id(request)
    if not sid or sid not in _admin_sessions:
        return False
    # 懒检查：发现过期立即清除
    if _is_session_expired(_admin_sessions[sid]):
        del _admin_sessions[sid]
        log.info(f"Admin session expired (lazy cleanup): {sid[:8]}...")
        return False
    return True


@app.get("/", response_class=RedirectResponse)
async def root_redirect():
    """根路径重定向到管理登录页"""
    return RedirectResponse(url="/admin/login", status_code=302)


@app.get("/admin/login")
async def admin_login_page(request: Request, error: str = ""):
    """管理员登录页面"""
    # 已登录则跳转仪表盘
    if _is_admin_logged_in(request):
        return RedirectResponse(url="/admin/dashboard", status_code=302)
    last_user = request.cookies.get("admin_last_user", "")
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": error,
        "last_user": last_user,
    })


@app.post("/admin/login")
async def admin_login_submit(request: Request):
    """管理员登录提交"""
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = (form.get("password") or "").strip()

    if not username or not password:
        return templates.TemplateResponse("login.html", {
            "request": request,
            "error": "请输入用户名和密码",
            "last_user": username,
        })

    # 验证 admin.json 中的账号
    admins = _load_admins()
    for a in admins:
        if a.get("username") == username and a.get("password") == password:
            import secrets
            sid = secrets.token_urlsafe(32)
            _admin_sessions[sid] = {"username": username, "created_at": __import__("time").time()}
            log.info(f"Admin logged in: {username}")
            resp = RedirectResponse(url="/admin/dashboard", status_code=302)
            resp.set_cookie(key="admin_sid", value=sid, max_age=_ADMIN_SESSION_MAX_AGE, httponly=True)
            resp.set_cookie(key="admin_last_user", value=username, max_age=86400 * 30)
            return resp

    # 登录失败：直接返回登录页并显示错误（不跳转）
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "用户名或密码错误",
        "last_user": username,
    })


@app.get("/admin/logout")
async def admin_logout(request: Request):
    """管理员登出"""
    sid = _get_session_id(request)
    if sid in _admin_sessions:
        del _admin_sessions[sid]
    resp = RedirectResponse(url="/admin/login", status_code=302)
    resp.delete_cookie("admin_sid")
    return resp


@app.get("/admin/dashboard")
async def admin_dashboard(request: Request):
    """管理后台主页（需登录）"""
    if not _is_admin_logged_in(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    sid = _get_session_id(request)
    admin_username = _admin_sessions.get(sid, {}).get("username", "Unknown")

    # 服务状态信息
    server_mode = "LIVE" if session_store.get("connected") else "DEMO"
    sdk_status = "\u53EF\u7528" if SDK_OK else "\u4E0D\u53EF\u7528"
    ib_connected = False
    try:
        from services.quote_service import get_ib_app
        ib_app = get_ib_app()
        if ib_app:
            ib_connected = ib_app.isConnected()
    except Exception:
        pass
    ib_status = "\u5DF2\u8FDE\u63A5" if ib_connected else "\u672A\u8FDE\u63A5"
    ib_color = "green" if ib_connected else "orange"
    active_count = len(quote_clients)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "admin_username": admin_username,
        "server_mode": server_mode,
        "sdk_status": sdk_status,
        "ib_status": ib_status,
        "ib_color": ib_color,
        "active_clients": str(active_count),
    })


# ── 自定义 /docs（内嵌中英文切换）──────────────────────────────────────

_DOCS_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Trading Server Manager - API Docs</title>
  <link rel="stylesheet"
    href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" />
  <style>
    /* 隐藏默认 topbar */
    .topbar { display: none !important; }
    .swagger-ui .topbar { display: none !important; }
    /* 语言切换按钮栏 */
    #lang-bar {
      position: fixed; top: 0; left: 0; right: 0;
      z-index: 999; text-align: right;
      padding: 6px 20px;
      background: #fff;
      border-bottom: 1px solid #e0e0e0;
    }
    #lang-btn {
      cursor: pointer; font-family: "Segoe UI", sans-serif; font-size: 13px;
      padding: 4px 14px; border-radius: 12px;
      color: #555; border: 1px solid #ccc;
      background: transparent; transition: all 0.15s;
    }
    #lang-btn:hover { color: #333; border-color: #999; background: #f5f5f5; }
    /* 给 Swagger 容器顶部留出空间给语言栏 */
    .swagger-ui .wrapper { margin-top: 36px; }
  </style>
</head>
<body>
  <!-- 语言切换栏 -->
  <div id="lang-bar">
    <button id="lang-btn">中文 / EN</button>
  </div>
  <div id="swagger-ui"></div>

  <script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>
  <script>
    // ── 中英文翻译映射 ───────────────────────────────────────────────
    var _CN_MAP = {
      "Authentication": "\u8BA4\u8BC1\u7BA1\u7406",
      "Orders": "\u8BA2\u5355\u7BA1\u7406",
      "Positions": "\u6301\u4ED3\u4E0E\u5065\u5EB7\u68C0\u67E5",
      "User login authentication": "\u7528\u6237\u767B\u5F55\u8BA4\u8BC1",
      "Verify user credentials and return access token":
          "\u9A8C\u8BC1\u7528\u6237\u51ED\u636E\u5E76\u8FD4\u56DE\u8BBF\u95EE\u4EE4\u724C",
      "User logout": "\u7528\u6237\u767B\u51FA",
      "Place order": "\u63D0\u4EA4\u8BA2\u5355",
      "Cancel order": "\u64A4\u9500\u8BA2\u5355",
      "Get live orders": "\u83B7\u53D6\u6D3B\u52A8\u8BA2\u5355",
      "Get order history": "\u83B7\u53D6\u5386\u53F2\u8BA2\u5355",
      "Get positions": "\u83B7\u53D6\u6301\u4ED5\u5217\u8868",
      "Health check": "\u5065\u5EB7\u68C0\u67E5",
      // ── Swagger UI 内置标签 ──
      "Parameters": "\u8BF7\u6C42\u53C2\u6570",
      "Request body": "\u8BF7\u6C42\u4F53",
      "Responses": "\u54CD\u5E94\u7ED3\u679C",
      "Try it out!": "\u53D1\u9001\u6D4B\u8BD5\u8BF7\u6C42",
      "Clear": "\u6E05\u7A7A",
      "Execute": "\u6267\u884C",
      "Cancel": "\u53D6\u6D88",
      "Loading...": "\u52A0\u8F7D\u4E2D...",
      "Response body": "\u54CD\u5E94\u5185\u5BB9",
      "Response headers": "\u54CD\u5E94\u5934\u4FE1\u606F",
      "Code": "\u72B6\u6001\u7801",
      "Details": "\u8BE6\u60C5",
      "Example Value": "\u793A\u4F8B\u503C",
      "Model": "\u6A21\u578B",
      "Example": "\u793A\u4F8B",
      "Schema": "\u7ED3\u6784\u5B9A\u4E49",
      "Required": "\u5FC5\u586B",
      "Optional": "\u53EF\u9009",
      "Default": "\u9ED8\u8BA4\u503C",
      "string": "\u5B57\u7B26\u4E32",
      "integer": "\u6574\u6570",
      "number": "\u6570\u5B57",
      "boolean": "\u5E03\u5C14\u503C",
      "array": "\u6570\u7EC4",
      "object": "\u5BF9\u8C61",
      "No parameters": "\u65E0\u53C2\u6570",
      "Send request": "\u53D1\u9001\u8BF7\u6C42",
      "Download": "\u4E0B\u8F7D",
      "Expand all": "\u5168\u90E8\u5C55F5\u00500",
      "Collapse all": "\u5168\u90E8\u6298\u53E0",
      "Expand operations": "\u5C55F5\u00500\u64CD\u4F5C",
      "Collapse operations": "\u6298\u53E0\u64CD\u4F5C",
    };

    var _isCN = false;

    function _replaceText(el) {
      if (!el || el.nodeType !== 1) return;
      // 处理元素自身文本内容
      var children = el.childNodes;
      for (var i = 0; i < children.length; i++) {
        var node = children[i];
        if (node.nodeType === 3) { // TEXT_NODE
          var txt = node.textContent.trim();
          if (txt && _CN_MAP[txt]) {
            node.textContent = _CN_MAP[txt];
          }
        } else if (node.nodeType === 1) {
          // 对特殊元素做属性翻译
          if (node.title && _CN_MAP[node.title]) {
            node.title = _CN_MAP[node.title];
          }
          if (node.placeholder && _CN_MAP[node.placeholder]) {
            node.placeholder = _CN_MAP[node.placeholder];
          }
          _replaceText(node); // 递归
        }
      }
      // 额外处理 opblock-tag 和 summary-description（可能有纯子元素）
      if (el.classList) {
        if (el.classList.contains('opblock-tag') || el.classList.contains('opblock-summary-description')) {
          var t = el.textContent.trim();
          if (_CN_MAP[t]) {
            // 找到最后一个非空文本子节点替换
            for (var j = el.childNodes.length - 1; j >= 0; j--) {
              if (el.childNodes[j].nodeType === 3) {
                el.childNodes[j].textContent = _CN_MAP[t];
                break;
              }
            }
          }
        }
      }
    }

    function toggleLang() {
      _isCN = !_isCN;
      document.getElementById('lang-btn').textContent =
        _isCN ? 'EN / \u4E2D\u6587' : '\u4E2D\u6587 / EN';
      var ui = document.getElementById('swagger-ui');
      if (_isCN) {
        _replaceText(ui);
      } else {
        location.reload();
      }
    }

    var ui = SwaggerUIBundle({
      url: "/openapi.json",
      dom_id: '#swagger-ui',
      deepLinking: true,
      presets: [
        SwaggerUIBundle.presets.apis,
        SwaggerUIBundle.SwaggerUIStandalonePreset
      ],
      plugins: [SwaggerUIBundle.plugins.DownloadUrl],
      layout: "BaseLayout",
      defaultModelsExpandDepth: 1,
      defaultModelExpandDepth: 2,
    });

    document.getElementById('lang-btn').addEventListener('click', toggleLang);

    // 监听动态展开的内容
    new MutationObserver(function() {
      if (_isCN) _replaceText(document.getElementById('swagger-ui'));
    }).observe(document.getElementById('swagger-ui'), { childList: true, subtree: true });
  </script>
</body>
</html>"""


@app.get("/docs", response_class=HTMLResponse)
async def docs(request: Request):
    """API 文档页面（支持中英文切换）"""
    return _DOCS_HTML


# ── WebSocket: 行情推送 ───────────────────────────────────────────────────

@app.websocket("/quotes")
async def quote_websocket(ws: WebSocket):
    """
    行情 WebSocket 端点
    支持 subscribe / unsubscribe 动作管理订阅标的
    """
    token = ws.query_params.get("token", "")
    if not token:
        await ws.close(code=4001, reason="Missing token")
        return

    # 验证 token（支持服务端 Token 和客户端登录 Token）
    from config import SERVER_TOKEN, active_client_tokens
    if token != SERVER_TOKEN and token not in active_client_tokens:
        await ws.close(code=4003, reason="Invalid token")
        return

    await ws.accept()
    quote_clients.append(ws)
    log.info(f"Quote WS client connected, total: {len(quote_clients)}")

    try:
        while True:
            raw = await ws.receive_text()
            data = __import__("json").loads(raw)

            action = data.get("action", "")
            symbols = data.get("symbols", [])

            if action == "subscribe":
                for sym in symbols:
                    if isinstance(sym, str) and sym.strip():
                        subscribed_syms.add(sym.strip())
                        log.info(f"Subscribed: {sym}, total symbols: {len(subscribed_syms)}")

            elif action == "unsubscribe":
                for sym in symbols:
                    subscribed_syms.discard(sym)
                    log.info(f"Unsubscribed: {sym}, total symbols: {len(subscribed_syms)}")

    except WebSocketDisconnect:
        log.info(f"Quote WS client disconnected, total: {len(quote_clients) - 1}")
    except Exception as e:
        log.warning(f"Quote WS error: {e}")
    finally:
        if ws in quote_clients:
            quote_clients.remove(ws)


# ── 节点注册与连接管理（Server_economic → Server_manager）────────────────

import secrets
import json as _json

# SSE 等待队列：{request_id: [asyncio.Queue, ...]}
_node_sse_queues: dict[str, list] = {}

# 注册请求过期清理间隔（每小时扫描一次）
_NODE_EXPIRE_CHECK_INTERVAL = 3600


@app.get("/ping")
async def ping():
    """连通性测试 — 子服务端填写注册页面前调用"""
    return {"status": "pong"}


@app.post("/nodes/register-request")
async def register_request(request: Request):
    """
    子服务端提交注册请求
    将信息存入暂存区（node_requests 表），等待管理员审核
    """
    body = await request.json()
    node_name = (body.get("node_name") or "").strip()
    region = (body.get("region") or "").strip()

    if not node_name:
        return {"ok": False, "error": "node_name is required"}

    # 生成唯一 request_id
    request_id = f"req_{secrets.token_hex(12)}"

    result = database.create_node_request(
        request_id=request_id,
        node_name=node_name,
        region=region,
        host=(body.get("host") or "").strip(),
        capabilities=body.get("capabilities"),
        contact=(body.get("contact") or "").strip(),
        description=(body.get("description") or "").strip(),
    )

    if not result:
        return {"ok": False, "error": "Failed to create registration request"}

    log.info(f"Node registration received: {node_name} ({region}) → {request_id}")

    return {
        "ok": True,
        "request_id": request_id,
        "message": "\u63d0\u4ea4\u6210\u529f\uff0c\u8bf7\u7b49\u5f85\u7ba1\u7406\u5458\u5ba1\u6838",
        "expire_at": result["expire_at"],
    }


@app.get("/nodes/await-approval")
async def await_approval(request_id: str):
    """
    SSE 端点：子服务端被动等待管理员审核结果
    连接后保持，管理员操作时通过队列推送事件
    """
    from fastapi.responses import StreamingResponse
    import asyncio

    # 验证请求是否存在且未过期
    req = database.get_node_request_by_id(request_id)
    if not req:
        return StreamingResponse(
            _sse_error_event("\u8bf7\u6c42 ID \u65e0\u6548"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    if req["status"] != "pending":
        # 已有结果，立即返回并关闭
        if req["status"] == "approved":
            data = _json.dumps({
                "approved": True, "server_id": req["server_id"],
                "token": req["token"], "message": "\u6ce8\u518c\u5df2\u901a\u8fc7",
            })
        else:
            data = _json.dumps({
                "approved": False, "reason": req.get("reject_reason", ""),
                "message": f"\u6ce8\u518c{req['status']}",
            })
        return StreamingResponse(
            iter([f"event: register_result\ndata: {data}\n\n"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # 创建 SSE 队列
    queue = asyncio.Queue(maxsize=10)
    if request_id not in _node_sse_queues:
        _node_sse_queues[request_id] = []
    _node_sse_queues[request_id].append(queue)

    log.info(f"SSE await-approval connected: {request_id}")

    async def event_stream():
        try:
            # 定期发送 SSE 心跳（保持连接活跃）
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: register_result\ndata: {msg}\n\n"
                    break  # 收到审核结果，结束流
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"  # SSE 注释行作为心跳
        finally:
            # 清理队列引用
            if request_id in _node_sse_queues:
                queues = _node_sse_queues[request_id]
                if queue in queues:
                    queues.remove(queue)
                if not queues:
                    del _node_sse_queues[request_id]

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/nodes/heartbeat")
async def node_heartbeat(request: Request):
    """已注册节点的心跳保活（需 Bearer Token）"""
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return {"status": "error", "message": "Missing or invalid Authorization header"}

    token = auth_header[7:].strip()
    node_info = database.verify_node_token(token)
    if not node_info:
        return {"status": "error", "message": "Invalid or expired token"}

    # 获取客户端 IP
    client_ip = request.client.host if request.client else ""
    body = await request.json() if await request.body() else {}
    current_ip = body.get("ip") or client_ip

    database.update_node_heartbeat(node_info["server_id"], current_ip)

    return {"status": "ok", "next_interval": 30}


# ── 节点管理 API（管理员用）───────────────────────────────────────────────

@app.get("/api/nodes/pending")
async def list_pending_nodes(request: Request):
    """获取所有待审核节点（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    pending = database.get_pending_node_requests()
    return {"ok": True, "data": pending}


@app.get("/api/nodes/list")
async def list_all_nodes(request: Request):
    """获取所有已批准节点列表（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    nodes = database.get_all_nodes()
    return {"ok": True, "data": nodes}


@app.post("/api/nodes/{request_id}/approve")
async def approve_node(request: Request, request_id: str):
    """管理员通过节点的注册请求"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    result = database.approve_node_request(request_id)
    if not result:
        return {"ok": False, "error": "Request not found or already processed"}

    # 向 SSE 等待队列推送结果
    data = _json.dumps({
        "approved": True,
        "server_id": result["server_id"],
        "token": result["token"],
        "message": "\u6ce8\u518c\u5df2\u901a\u8fc7",
    })
    _push_sse_result(request_id, data)

    return {
        "ok": True,
        "message": f"\u5df2\u901a\u8fc7\uff1a{result['server_id']}",
        "server_id": result["server_id"],
    }


@app.post("/api/nodes/{request_id}/reject")
async def reject_node(request: Request, request_id: str, reason: str = ""):
    """管理员拒绝节点的注册请求"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    ok = database.reject_node_request(request_id, reason=reason)
    if not ok:
        return {"ok": False, "error": "Request not found or already processed"}

    # 向 SSE 等待队列推送结果
    data = _json.dumps({
        "approved": False,
        "reason": reason,
        "message": "\u6ce8\u518c\u88ab\u62d2\u7edd",
    })
    _push_sse_result(request_id, data)

    return {"ok": True, "message": "\u5df2\u62d2\u7edd"}


@app.post("/api/nodes/{server_id}/delete")
async def delete_node(request: Request, server_id: str):
    """管理员彻底删除已批准节点"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok = database.delete_node(server_id)
    if not ok:
        return {"ok": False, "error": "Node not found"}
    return {"ok": True, "message": f"\u5df2\u5220\u9664\uff1a{server_id}"}


@app.post("/api/nodes/{server_id}/suspend")
async def suspend_node(request: Request, server_id: str):
    """管理员暂停节点（停止访问，标黄）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok = database.suspend_node(server_id)
    if not ok:
        return {"ok": False, "error": "Node not found or invalid status"}
    return {"ok": True, "message": f"\u5df2\u6682\u505c\uff1a{server_id}"}


@app.post("/api/nodes/{server_id}/resume")
async def resume_node(request: Request, server_id: str):
    """管理员恢复被暂停的节点"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok = database.resume_node(server_id)
    if not ok:
        return {"ok": False, "error": "Node not found or not suspended"}
    return {"ok": True, "message": f"\u5df2\u6062\u590d\uff1a{server_id}"}


@app.post("/api/nodes/{server_id}/occupy")
async def occupy_node(request: Request, server_id: str):
    """客户端连接 SE 成功后，标记节点被占用（需登录 token）"""
    from auth import active_client_tokens
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    if not token or token not in active_client_tokens:
        return {"ok": False, "error": "Unauthorized"}

    body = await request.json() if await request.body() else {}
    username = body.get("username", "")

    # 检查是否已被其他账户占用
    occ = database.get_occupation_info(server_id)
    if occ and occ.get("occupied_by") and occ["occupied_by"] != username:
        return {"ok": False, "error": "occupied",
                "message": f"\u8282\u70b9\u5df2\u88ab\u8d26\u6237 '{occ['occupied_by']}' \u5360\u7528"}

    ok = database.occupy_node(server_id, username)
    if not ok:
        return {"ok": False, "error": "\u5360\u7528\u5931\u8d25"}
    return {"ok": True, "message": f"\u8282\u70b9\u5df2\u88ab '{username}' \u5360\u7528"}


@app.post("/api/nodes/{server_id}/release")
async def release_node(request: Request, server_id: str):
    """客户端断开 SE 连接后，释放节点占用（需登录 token）"""
    from auth import active_client_tokens
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    if not token or token not in active_client_tokens:
        return {"ok": False, "error": "Unauthorized"}

    database.release_node(server_id)
    return {"ok": True, "message": f"\u5df2\u91ca\u653e\u8282\u70b9"}


# ── 账户管理 API（管理员用）────────────────────────────────────────────

@app.get("/api/accounts/list")
async def list_accounts(request: Request):
    """获取所有账户列表（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    accounts = database.get_all_accounts()
    return {"ok": True, "data": accounts}


@app.post("/api/accounts/create")
async def create_account(request: Request):
    """创建新账户（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    se_address = (body.get("se_address") or "").strip()
    broker_tag = (body.get("broker_tag") or "").strip()
    description = (body.get("description") or "").strip()

    # 四项必填校验
    if not username:
        return {"ok": False, "error": "用户名不能为空"}
    if not password:
        return {"ok": False, "error": "密码不能为空"}
    if not se_address:
        return {"ok": False, "error": "SE 地址不能为空"}
    if not broker_tag:
        return {"ok": False, "error": "券商不能为空"}

    result = database.create_account(
        username=username,
        password=password,
        se_address=se_address,
        broker_tag=broker_tag,
        description=description,
    )
    if not result:
        return {"ok": False, "error": f"创建失败（用户名 '{username}' 可能已存在）"}
    # 隐藏密码哈希
    result.pop("password_hash", None)
    return {"ok": True, "data": result, "message": f"账户 {username} 创建成功"}


@app.post("/api/accounts/{account_id}/delete")
async def delete_account(request: Request, account_id: int):
    """删除账户（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok = database.delete_account(account_id)
    if not ok:
        return {"ok": False, "error": "删除失败（可能不存在或受保护账号）"}
    return {"ok": True, "message": f"账户已删除 (id={account_id})"}


@app.post("/api/accounts/{account_id}/suspend")
async def suspend_account(request: Request, account_id: int):
    """暂停账户（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok = database.suspend_account(account_id)
    if not ok:
        return {"ok": False, "error": "暂停失败（可能已被暂停或不存在）"}
    return {"ok": True, "message": f"账户已暂停 (id={account_id})"}


@app.post("/api/accounts/{account_id}/resume")
async def resume_account(request: Request, account_id: int):
    """恢复被暂停的账户（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok = database.resume_account(account_id)
    if not ok:
        return {"ok": False, "error": "恢复失败（可能未被暂停或不存在）"}
    return {"ok": True, "message": f"账户已恢复 (id={account_id})"}


@app.get("/api/accounts/{account_id}/detail")
async def get_account_detail(request: Request, account_id: int):
    """获取单个账户的详细信息（需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    acct = database.get_account_by_id(account_id)
    if not acct:
        return {"ok": False, "error": "账户不存在"}
    return {"ok": True, "data": acct}


@app.post("/api/accounts/{account_id}/update")
async def update_account(request: Request, account_id: int):
    """修改账户信息（需管理员登录，可修改所有注册信息）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    body = await request.json()
    se_address = (body.get("se_address") or "").strip()
    broker_tag = (body.get("broker_tag") or "").strip()
    description = (body.get("description") or "").strip()
    password = (body.get("password") or "").strip()

    # 修改时四项信息也必填
    if not se_address:
        return {"ok": False, "error": "SE 地址不能为空"}
    if not broker_tag:
        return {"ok": False, "error": "券商不能为空"}

    ok = database.update_account(
        account_id=account_id,
        se_address=se_address,
        broker_tag=broker_tag,
        description=description,
        password=password,
    )
    if not ok:
        return {"ok": False, "error": "更新失败（可能账户不存在）"}
    return {"ok": True, "message": f"账户信息已更新 (id={account_id})"}


@app.get("/api/accounts/se-status")
async def check_se_status(request: Request, address: str = ""):
    """检查 SE 地址是否有对应的在线子服务器（需登录）"""
    from auth import active_client_tokens
    from fastapi import HTTPException

    # 验证请求携带有效 token
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    if not token or token not in active_client_tokens:
        return {"ok": False, "error": "Unauthorized", "online": False}

    result = database.check_se_online(address)
    return {"ok": True, **result}


def _push_sse_result(request_id: str, data_json: str):
    """向指定 request_id 的所有 SSE 连接推送消息"""
    import asyncio
    queues = _node_sse_queues.get(request_id, [])
    for q in queues[:]:
        try:
            q.put_nowait(data_json)
        except asyncio.QueueFull:
            pass


def _sse_error_event(msg: str):
    """生成一条错误 SSE 事件后关闭"""
    data = _json.dumps({"approved": False, "reason": msg, "message": msg})
    yield f"event: register_result\ndata: {data}\n\n"


# ── 启动事件 ─────────────────────────────────────────────────────────────

async def _session_cleanup_loop():
    """后台定期清理过期的管理会话"""
    while True:
        await asyncio.sleep(_ADMIN_CLEANUP_INTERVAL)
        now = __import__("time").time()
        expired = [
            sid for sid, data in _admin_sessions.items()
            if (now - data.get("created_at", 0)) > _ADMIN_SESSION_MAX_AGE
        ]
        for s in expired:
            del _admin_sessions[s]
        if expired:
            log.info(f"Cleaned {len(expired)} expired admin session(s)")


async def _node_expire_cleanup_loop():
    """后台定期清理过期的节点注册请求"""
    while True:
        await asyncio.sleep(_NODE_EXPIRE_CHECK_INTERVAL)
        count = database.cleanup_expired_requests()
        # 同时向过期请求的 SSE 队列推送超时通知
        import json as _json_mod
        pending = database.get_pending_node_requests()  # 清理后应无，但双重保障
        for req in pending:
            data = _json_mod.dumps({
                "approved": False,
                "reason": "\u5ba1\u6838\u8d85\u65f6(24h)",
                "message": "\u8bf7\u91cd\u65b0\u63d0\u4ea4\u6ce8\u518c",
            })
            _push_sse_result(req["request_id"], data)


# 心跳超时检测间隔（每30秒巡检一次）
_HEARTBEAT_CHECK_INTERVAL = 30


async def _heartbeat_monitor_loop():
    """
    后台定期检测心跳超时的节点，自动标记为离线
    """
    await asyncio.sleep(10)
    log.info("Heartbeat monitor started (interval=%ds, timeout=%ds)" %
             (_HEARTBEAT_CHECK_INTERVAL, database.HEARTBEAT_TIMEOUT_SECONDS))
    while True:
        try:
            count = database.check_offline_nodes()
            if count > 0:
                log.info(f"Heartbeat monitor: {count} node(s) marked as OFFLINE")
        except Exception as e:
            log.error(f"Heartbeat monitor error: {e}")
        await asyncio.sleep(_HEARTBEAT_CHECK_INTERVAL)


@app.on_event("startup")
async def startup():
    """应用启动时执行初始化"""
    # 启动会话过期清理任务
    asyncio.create_task(_session_cleanup_loop())
    # 启动节点注册请求过期清理任务
    asyncio.create_task(_node_expire_cleanup_loop())
    # 启动心跳超时检测任务
    asyncio.create_task(_heartbeat_monitor_loop())
    # 初始化数据库（保留表结构，Demo 模式主要使用 JSON 用户）
    try:
        init_db()
    except Exception as e:
        log.warning(f"Database init skipped (non-critical): {e}")

    # 检查并连接 Tastytrade（可选，不阻塞启动）
    if is_configured() and SDK_OK:
        try:
            from services.tastytrade_svc import _create_session_account
            s, a = await _create_session_account()
            session_store["session"] = s
            session_store["account"] = a
            session_store["acct_num"] = str(a.account_number)
            session_store["connected"] = True
            log.info(f"Tastytrade auto-connected, account: {session_store['acct_num']}")
        except Exception as e:
            log.warning(f"Tastytrade auto-connect failed, running in DEMO mode: {e}")
    else:
        mode_reason = "SDK not installed" if not SDK_OK else "credentials not configured"
        log.info(
            f"Tastytrade not available ({mode_reason}). "
            f"Server running in DEMO mode — login, orders & positions will use mock data."
        )

    # 启动行情相关后台任务（IB 可选）
    asyncio.create_task(ib_preconnect())
    asyncio.create_task(quote_stream_loop())

    log.info("=" * 60)
    log.info(f"Server Manager started on {SERVER_HOST}:{SERVER_PORT}")
    log.info(f"Mode: {'LIVE (Tastytrade connected)' if session_store.get('connected') else 'DEMO'}")
    log.info(f"SDK available: {SDK_OK}, IB available: {'ibapi' in __import__('sys').modules or False}")
    log.info("=" * 60)


@app.on_event("shutdown")
async def shutdown():
    """应用关闭时清理资源"""
    session_store["session"] = None
    session_store["account"] = None
    session_store["connected"] = False
    quote_clients.clear()
    subscribed_syms.clear()
    log.info("Server Manager shut down cleanly")


# ── 直接运行 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
