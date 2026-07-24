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
    SERVER_HOST       服务监听地址   (默认: 127.0.0.1；临时直连可设 0.0.0.0)
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
import re
import uuid


# SSL 证书处理（解决 Windows 证书链不完整问题）
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
except ImportError:
    pass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from config import (
    SERVER_HOST, SERVER_PORT, session_store,
    quote_clients, subscribed_syms, log, read_recent_error_lines, read_error_log_text, LOG_FILE, ERROR_LOG_FILE,
    SM_ENABLE_LEGACY_QUOTES,
    SM_ALLOWED_HOSTS, SM_COOKIE_SAMESITE, SM_COOKIE_SECURE, SM_CORS_ORIGINS,
    SM_DNSPOD_MODE, SM_DOMAIN_POOL_REQUIRED, SM_PUBLIC_BASE_URL, SM_TS_DOMAIN_SUFFIX,
    SM_CADDY_REQUIRED,
)

import database
import domain_pool
import node_state
from address_utils import address_candidates, endpoint_matches_node, ts_api_url
from database import init_db
from routers.auth_router import router as auth_router
from routers.position_router import router as position_router



# ── FastAPI App ───────────────────────────────────────────────────────────

_cors_origins = SM_CORS_ORIGINS
_cookie_secure = SM_COOKIE_SECURE
_cookie_samesite = SM_COOKIE_SAMESITE

app = FastAPI(
    title="Trading Server Manager",
    description=(
        "### 交易系统 Server Manager（控制面）\n\n"
        "- **认证管理**：用户登录/登出，支持 JSON 文件 / 配置文件 / 数据库多级认证\n"
        "- **节点管理**：注册、审核、占用、状态与心跳管理\n"
        "- **配置管理**：TS 券商配置下发与版本控制\n"
        "- **健康检查**：服务状态监控\n\n"
        "---\n\n"
        "**职责边界**：SM 不直接执行券商交易，交易请求统一由 TS 执行"
    ),

    version="1.0.0",
    # 禁用内置 /docs，使用下方自定义版本（带语言切换）
    docs_url=None,
)

# CORS（开发环境允许跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=SM_ALLOWED_HOSTS or ["*"],
)

# 注册 API 路由
app.include_router(auth_router)
app.include_router(position_router)


# ── Web 管理后台（Jinja2 模板）───────────────────────────────────────

_TEMPLATES_DIR = os.path.join(_SCRIPT_DIR, "templates")
templates = Jinja2Templates(directory=_TEMPLATES_DIR)

# 管理员会话存储（内存，重启失效；后续可迁移到 Redis）
_admin_sessions: dict[str, dict] = {}  # {session_id: {id, username, role, created_at}}


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


def _get_admin_session(request: Request) -> dict | None:
    if not _is_admin_logged_in(request):
        return None
    sid = _get_session_id(request)
    return _admin_sessions.get(sid)


def _is_super_admin(request: Request) -> bool:
    s = _get_admin_session(request) or {}
    return (s.get("role") or "") == "super_admin"


def _request_ip(request: Request) -> str:
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    if forwarded_for:
        return forwarded_for
    if request.client:
        return request.client.host or ""
    return ""


def _record_admin_event(request: Request, action: str, resource: str, detail: str) -> None:
    sess = _get_admin_session(request) or {}
    try:
        database.record_audit_log(
            username=sess.get("username") or "system",
            action=action,
            resource=resource,
            detail=detail,
            ip=_request_ip(request),
        )
    except Exception as e:
        log.warning(f"record admin event failed: {e}")


def _sync_node_state_to_db(server_id: str | None = None) -> int:
    states = node_state.manager.prepare_db_sync_data()
    if server_id:
        states = [s for s in states if s.get("server_id") == server_id]
    if not states:
        return 0
    return database.sync_node_states_to_db(states)


def _get_realtime_nodes_for_display() -> list[dict]:
    """Return node display rows from the in-memory runtime state."""
    nodes = node_state.manager.get_all_for_display()
    server_ids = [(n.get("server_id") or "").strip() for n in nodes]
    configs = database.get_node_broker_configs(server_ids)
    for n in nodes:
        sid = (n.get("server_id") or "").strip()
        cfg = configs.get(sid)
        if cfg:
            n["broker_config"] = cfg.get("_raw_config", {})
    return nodes



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
    return templates.TemplateResponse(request, "login.html", {
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
        return templates.TemplateResponse(request, "login.html", {
            "error": "请输入用户名和密码",
            "last_user": username,
        })

    admin = database.verify_web_admin(username, password)
    if admin:
        import secrets
        sid = secrets.token_urlsafe(32)
        _admin_sessions[sid] = {
            "id": admin.get("id"),
            "username": admin.get("username"),
            "role": admin.get("role"),
            "created_at": __import__("time").time(),
        }
        log.info(f"Admin logged in: {username} role={admin.get('role')}")
        resp = RedirectResponse(url="/admin/dashboard", status_code=302)
        resp.set_cookie(key="admin_sid", value=sid, max_age=_ADMIN_SESSION_MAX_AGE, httponly=True, secure=_cookie_secure, samesite=_cookie_samesite)
        resp.set_cookie(key="admin_last_user", value=username, max_age=86400 * 30, secure=_cookie_secure, samesite=_cookie_samesite)
        return resp

    # 登录失败：直接返回登录页并显示错误（不跳转）
    return templates.TemplateResponse(request, "login.html", {
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
    resp.delete_cookie("admin_sid", samesite=_cookie_samesite, secure=_cookie_secure)
    return resp


@app.get("/admin/dashboard")
async def admin_dashboard(request: Request):
    """管理后台主页（需登录）"""
    if not _is_admin_logged_in(request):
        return RedirectResponse(url="/admin/login", status_code=302)

    sid = _get_session_id(request)
    sess = _admin_sessions.get(sid, {})
    admin_username = sess.get("username", "Unknown")
    admin_role = sess.get("role", "admin")


    # 服务状态信息（控制面不直接承载交易 SDK）
    server_mode = "CONTROL_PLANE"
    sdk_status = "N/A"
    ib_connected = False

    if SM_ENABLE_LEGACY_QUOTES:
        try:
            from services.quote_service import get_ib_app
            ib_app = get_ib_app()
            if ib_app:
                ib_connected = ib_app.isConnected()
        except Exception:
            pass
    ib_status = "\u5DF2\u8FDE\u63A5" if ib_connected else ("未启用" if not SM_ENABLE_LEGACY_QUOTES else "\u672A\u8FDE\u63A5")
    ib_color = "green" if ib_connected else "orange"
    active_count = len(quote_clients)

    return templates.TemplateResponse(request, "dashboard.html", {
        "admin_username": admin_username,
        "admin_role": admin_role,
        "is_super_admin": admin_role == "super_admin",
        "server_mode": server_mode,
        "sdk_status": sdk_status,
        "ib_status": ib_status,
        "ib_color": ib_color,
        "active_clients": str(active_count),
        "public_base_url": SM_PUBLIC_BASE_URL,
        "ts_domain_suffix": SM_TS_DOMAIN_SUFFIX,
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
    if not SM_ENABLE_LEGACY_QUOTES:
        await ws.close(code=4004, reason="Legacy quote service disabled")
        return

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


# ── 节点注册与连接管理（Trader_Server → Server_manager）────────────────

import secrets
import json as _json
import urllib.parse
import urllib.request
import urllib.error
import socket


# SSE 等待队列：{request_id: [asyncio.Queue, ...]}
_node_sse_queues: dict[str, list] = {}
# 配置变更通知 SSE 队列：{server_id: [asyncio.Queue, ...]}
_config_sse_queues: dict[str, list] = {}


# 注册请求过期清理间隔（每小时扫描一次）
_NODE_EXPIRE_CHECK_INTERVAL = 3600


def _discard_pending_request_by_probe(request_id: str, reason: str) -> dict:
    """审批前问询失败时，废弃 pending 申请。"""
    result = database.cancel_node_request(
        request_id=request_id,
        reason=reason,
        reviewer="sm_probe",
        force_discard_approved=False,
    )
    data = _json.dumps({
        "approved": False,
        "reason": reason,
        "message": "注册已废弃",
    })
    _push_sse_result(request_id, data)
    return result


def _probe_ts_request_alive(req: dict, request_id: str, timeout_s: int = 10) -> tuple[str, str]:
    """审批前向 SE 问询 request 是否仍在等待。

    返回:
      - ("ok", "")：可正常审批
      - ("abandoned", reason)：明确判定为已废弃/异常申请（应废弃）
      - ("unknown", reason)：网络不可达等不确定情况（不应误废弃）
    """
    host = (req.get("host") or "").strip()
    if not host:
        return "unknown", "SE网络异常（缺少主机地址）"

    params = urllib.parse.urlencode({"request_id": request_id})
    base_url = ts_api_url(host, "/api/register/pre-approve-check")
    if not base_url:
        return "unknown", "SE网络异常（无法解析主机地址）"
    url = f"{base_url}?{params}"

    try:
        req_obj = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req_obj, timeout=timeout_s) as resp:
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            if payload.get("ok") and payload.get("can_approve"):
                return "ok", ""

            reason = str(payload.get("reason") or "").strip()
            # 仅当 SE 明确回报“已废弃/不等待当前请求”时才按废弃处理
            if reason in (
                "request_abandoned_or_not_waiting",
                "request_mismatch_or_abandoned",
            ):
                return "abandoned", f"SE网络异常或废弃申请（{reason}）"

            # 其他场景不在此处误判废弃
            return "unknown", f"SE问询返回不可审批（{reason or 'unknown'}）"
    except socket.timeout:
        return "unknown", "SE网络异常（问询超时>10s）"
    except urllib.error.URLError as e:
        return "unknown", f"SE网络异常（{e}）"
    except Exception as e:
        return "unknown", f"SE网络异常（{e}）"


def _force_disconnect_ts_clients(ts_host: str, node_token: str, reason: str, timeout_s: int = 8) -> tuple[bool, dict]:
    """调用 SE 内部接口，强制断开当前节点上的 Client WS 连接。"""
    host = (ts_host or "").strip()
    if not host:
        return False, {"error": "ts_host_empty"}
    if not node_token:
        return False, {"error": "node_token_empty"}

    url = ts_api_url(host, "/api/admin/force-disconnect")
    if not url:
        return False, {"error": "ts_endpoint_invalid"}
    body = _json.dumps({"reason": reason}).encode("utf-8")

    req_obj = urllib.request.Request(url, data=body, method="POST")
    req_obj.add_header("Content-Type", "application/json")
    req_obj.add_header("Authorization", f"Bearer {node_token}")

    try:
        with urllib.request.urlopen(req_obj, timeout=timeout_s) as resp:
            payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
            return bool(payload.get("ok")), payload
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(e)
        return False, {"error": f"http_{e.code}", "detail": detail}
    except Exception as e:
        return False, {"error": "network_error", "detail": str(e)}


async def _disconnect_user_from_occupied_nodes(username: str, reason: str) -> list[dict]:
    """Disconnect and release nodes currently occupied by one managed account."""
    target_user = (username or "").strip()
    results: list[dict] = []
    if not target_user:
        return results
    for sid, state in list(node_state.manager._states.items()):
        if (state.occupied_by or "").strip() != target_user:
            continue
        endpoint = (
            state._public_endpoint
            or state._assigned_domain
            or state._host
            or state.current_ip
            or state._public_ip
            or ""
        ).strip()
        node_token = (state._token or "").strip()
        disconnected = False
        detail: dict = {}
        if endpoint and node_token and state.is_online:
            disconnected, detail = await asyncio.to_thread(
                _force_disconnect_ts_clients,
                endpoint,
                node_token,
                reason,
                8,
            )
        if disconnected or not state.is_online:
            node_state.manager.release(sid, check_offline=False)
            _sync_node_state_to_db(sid)
        results.append({
            "server_id": sid,
            "disconnected": disconnected,
            "released": disconnected or not state.is_online,
            "detail": detail,
        })
    return results


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
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    source_ip = forwarded_for or ((request.client.host if request.client else "") or "").strip()
    reported_public_ip = (body.get("public_ip") or "").strip()

    if not node_name:
        return {"ok": False, "error": "node_name is required"}
    try:
        public_ip = domain_pool.normalize_public_ipv4(reported_public_ip or source_ip)
    except domain_pool.DomainPoolError as exc:
        if SM_DOMAIN_POOL_REQUIRED:
            return {
                "ok": False,
                "error": str(exc),
                "source_ip": source_ip,
            }
        public_ip = reported_public_ip or source_ip

    # 生成唯一 request_id
    request_id = f"req_{secrets.token_hex(12)}"

    result = database.create_node_request(
        request_id=request_id,
        node_name=node_name,
        region=region,
        host=(body.get("host") or public_ip).strip(),
        public_ip=public_ip,
        source_ip=source_ip,
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
                "token": req["token"],
                "public_ip": req.get("public_ip", ""),
                "assigned_domain": req.get("assigned_domain", ""),
                "public_endpoint": req.get("public_endpoint", ""),
                "message": "\u6ce8\u518c\u5df2\u901a\u8fc7",
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


@app.get("/nodes/config-events")
async def node_config_events(request: Request, server_id: str = ""):
    """SE 订阅配置变更事件（SSE）"""
    from fastapi.responses import StreamingResponse
    import asyncio

    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    if not token or not database.verify_node_token_for_config(token, server_id):
        return StreamingResponse(
            iter(["event: error\ndata: {\"ok\": false, \"error\": \"Unauthorized\"}\n\n"]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    queue = asyncio.Queue(maxsize=20)
    if server_id not in _config_sse_queues:
        _config_sse_queues[server_id] = []
    _config_sse_queues[server_id].append(queue)

    async def event_stream():
        try:
            hello = _json.dumps({"type": "HELLO", "server_id": server_id})
            yield f"event: config\ndata: {hello}\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield f"event: config\ndata: {msg}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            if server_id in _config_sse_queues:
                queues = _config_sse_queues[server_id]
                if queue in queues:
                    queues.remove(queue)
                if not queues:
                    del _config_sse_queues[server_id]

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/nodes/heartbeat")
async def node_heartbeat(request: Request):

    """
    已注册节点的心跳保活（需 Bearer Token）

    心跳直接更新内存状态，不写数据库。
    状态守卫：suspended 节点心跳不改变暂停状态。
    
    改进（占用感知）：
      - 如果节点被占用，返回更短的心跳间隔（5秒 vs 20秒）
      - 这样 SE 会在占用状态下更频繁地发送心跳，
        结合 SM 端的短超时阈值（15秒），能更快检测到掉线
    """
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

    sid = node_info["server_id"]
    ok, msg = node_state.manager.update_heartbeat(sid, current_ip)
    if not ok:
        log.warning(f"Hebeat from unknown/removed node: {sid}")

    # ★ 占用感知：被占用的节点使用更短的心跳间隔
    state = node_state.manager.get(sid)
    is_occupied = bool(state and state.occupied_by)
    next_interval = 5 if is_occupied else 20  # 占用状态下5秒一跳，否则20秒

    response = {
        "status": "ok",
        "next_interval": next_interval,
    }
    
    # 如果被占用，额外告知 SE 当前占用信息（用于诊断和快速恢复）
    if is_occupied:
        response["occupied"] = True
        response["occupied_by"] = state.occupied_by
        response["occupied_timeout"] = node_state.OCCUPIED_HEARTBEAT_TIMEOUT

    # ★ 券商配置版本通知：SE 据此判断是否需要拉取最新配置
    from database import get_node_broker_config
    broker_cfg = get_node_broker_config(sid)
    if broker_cfg:
        response["config_version"] = broker_cfg["config_version"]
    else:
        response["config_version"] = 0

    return response


@app.post("/nodes/release-occupation")
async def release_node_occupation_from_ts(request: Request):
    """Allow an authenticated TS to release the exact Client session it closed."""
    auth_header = request.headers.get("authorization", "")
    token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    node_info = database.verify_node_token(token) if token else None
    if not node_info:
        return {"ok": False, "released": False, "error": "Unauthorized"}

    body = await request.json() if await request.body() else {}
    server_id = str(body.get("server_id") or "").strip()
    username = str(body.get("username") or "").strip()
    client_token = str(body.get("client_token") or "").strip()
    connection_id = str(body.get("connection_id") or "").strip()
    node_server_id = str(node_info.get("server_id") or "")
    if server_id != node_server_id:
        return {
            "ok": False,
            "released": False,
            "error": "node_server_mismatch",
        }
    if not username or not client_token or not connection_id:
        return {
            "ok": False,
            "released": False,
            "error": "session_identity_required",
        }

    released = node_state.manager.release_session(
        server_id,
        username,
        client_token,
        connection_id,
        check_offline=False,
    )
    if released:
        log.info(
            "Released Client occupation after TS connection closed: node=%s user=%s",
            server_id,
            username,
        )
    return {
        "ok": True,
        "released": bool(released),
        "reason": "" if released else "occupation_changed",
    }


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
    node_state.manager.check_offline_nodes()
    nodes = _get_realtime_nodes_for_display()
    return {"ok": True, "data": nodes}


@app.get("/api/domain-pool/list")
async def list_domain_pool(request: Request, page: int = 1, status: str = ""):
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    data = database.list_ts_domain_pool(page=page, page_size=20, status=status)
    return {
        "ok": True,
        "data": data,
        "dns_mode": SM_DNSPOD_MODE,
        "domain_suffix": SM_TS_DOMAIN_SUFFIX,
    }


@app.get("/api/domain-pool/options")
async def list_domain_pool_options(request: Request):
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    return {"ok": True, "data": database.list_ts_domain_options()}


@app.post("/api/domain-pool/import")
async def import_domain_pool(request: Request):
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return {"ok": False, "error": "request body must be a JSON object"}
    raw_domains = body.get("domains") or []
    if isinstance(raw_domains, str):
        raw_domains = re.split(r"[,，\r\n]+", raw_domains)
    elif not isinstance(raw_domains, list):
        return {"ok": False, "error": "domains must be a string or a list"}
    domains = [str(item).strip() for item in raw_domains if str(item).strip()]
    if not domains:
        return {
            "ok": False,
            "error": "domains is required; automatic initialization is not supported",
        }
    if len(domains) > 200:
        return {"ok": False, "error": "at most 200 domains can be imported at once"}
    result = domain_pool.import_domains(domains)
    if result.get("ok"):
        _record_admin_event(
            request,
            "IMPORT_TS_DOMAINS",
            "domain_pool",
            f"导入域名：{result.get('accepted', 0)} 条",
        )
    return result


@app.post("/api/domain-pool/{domain_id}/delete")
async def delete_domain_pool_entry(request: Request, domain_id: int):
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: domain_pool.delete_domain(domain_id),
        )
    except domain_pool.DomainPoolError as exc:
        return {"ok": False, "error": str(exc)}

    _record_admin_event(
        request,
        "DELETE_TS_DOMAIN",
        "domain_pool",
        f"删除域名：{result.get('domain', '')}",
    )
    return result


@app.post("/api/domain-pool/{domain_id}/refresh-dns")
async def refresh_domain_pool_dns(request: Request, domain_id: int):
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: domain_pool.refresh_domain_dns(domain_id),
        )
        return result
    except domain_pool.DomainPoolError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/domain-pool/{domain_id}/release")
async def release_domain_pool_entry(request: Request, domain_id: int):
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: domain_pool.release_orphan_domain(domain_id),
        )
        return result
    except domain_pool.DomainPoolError as exc:
        return {"ok": False, "error": str(exc)}


@app.post("/api/nodes/refresh-status")
async def refresh_nodes_status(request: Request):
    """Refresh in-memory node status for dashboard display."""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    offline_ids = node_state.manager.check_offline_nodes()
    if offline_ids:
        log.info(f"[refresh-status] Passive check detected {len(offline_ids)} offline node(s) before display")
        _sync_node_state_to_db()

    nodes = _get_realtime_nodes_for_display()
    online = sum(1 for n in nodes if n["real_status"] == "online")
    offline = sum(1 for n in nodes if n["real_status"] == "offline")
    occupied = sum(1 for n in nodes if n["real_status"] == "occupied")
    suspended = sum(1 for n in nodes if n["real_status"] == "suspended")

    return {
        "ok": True,
        "checked": len(nodes),
        "online": online,
        "offline": offline,
        "occupied": occupied,
        "suspended": suspended,
        "nodes": nodes,
    }


@app.post("/api/nodes/{request_id}/approve")
async def approve_node(request: Request, request_id: str):
    """管理员通过节点的注册请求（同时录入券商配置）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    # 读取请求体（含券商配置）
    try:
        body = await request.json() if await request.body() else {}
    except Exception:
        body = {}
    broker_type = body.get("broker_type", "TT")
    broker_credentials = body.get("credentials", {})

    req = database.get_node_request_by_id(request_id)
    if not req:
        return {"ok": False, "error": "Request not found"}
    if (req.get("status") or "").strip() != "pending":
        return {"ok": False, "error": f"Request already processed: {req.get('status', '-')}", "status": req.get("status", "")}

    # 审批前问询 SE：仅在“明确废弃/异常申请”时才自动废弃
    loop = asyncio.get_event_loop()
    if _node_sse_queues.get(request_id):
        probe_state, probe_msg = "ok", ""
    else:
        probe_state, probe_msg = await loop.run_in_executor(
            None,
            lambda: _probe_ts_request_alive(req, request_id, 10),
        )
    if probe_state == "abandoned":
        discard_result = await loop.run_in_executor(
            None,
            lambda: _discard_pending_request_by_probe(request_id, probe_msg),
        )
        log.warning(f"[approve] probe says abandoned, request discarded: {request_id}, reason={probe_msg}, result={discard_result}")
        return {
            "ok": False,
            "error": probe_msg,
            "discarded": True,
            "request_id": request_id,
        }
    if probe_state == "unknown":
        # 网络不可达等不确定情况，不误判废弃，继续人工审批通过流程
        log.warning(f"[approve] probe unknown, continue approval: {request_id}, detail={probe_msg}")


    assignment = None
    if SM_DOMAIN_POOL_REQUIRED:
        try:
            assignment = await loop.run_in_executor(
                None,
                lambda: domain_pool.allocate_domain(
                    req.get("node_name", ""),
                    req.get("public_ip", ""),
                ),
            )
        except domain_pool.DomainPoolError as exc:
            log.warning(f"[approve] domain allocation failed: {request_id}, detail={exc}")
            return {
                "ok": False,
                "error": str(exc),
                "request_id": request_id,
            }

    result = database.approve_node_request(request_id, domain_assignment=assignment)
    if not result:
        if assignment:
            await loop.run_in_executor(
                None,
                lambda: domain_pool.abort_allocation(assignment, "node approval failed"),
            )
        return {"ok": False, "error": "Request not found or already processed"}


    # 将券商配置写入 brokers.config
    if broker_type or broker_credentials:
        database.set_node_broker_config(
            server_id=result["server_id"],
            broker_type=broker_type,
            credentials=broker_credentials,
        )
        cfg = database.get_node_broker_config(result["server_id"])
        if cfg:
            _push_config_change(result["server_id"], int(cfg.get("config_version", 0) or 0))


    # 审批通过后，将节点加载到内存状态管理器
    approved_rows = database.get_approved_nodes_for_memory_load()
    newly_approved = [r for r in approved_rows if r.get("server_id") == result["server_id"]]
    if newly_approved:
        node_state.manager.register(newly_approved[0])
    else:
        node_state.manager.register({
            "server_id": result["server_id"],
            "token": result["token"],
            "node_name": "",
            "status": "approved",
            "broker_status": "online",
            "public_ip": result.get("public_ip", ""),
            "assigned_domain": result.get("assigned_domain", ""),
            "public_endpoint": result.get("public_endpoint", ""),
        })

    # 向 SSE 等待队列推送结果
    data = _json.dumps({
        "approved": True,
        "server_id": result["server_id"],
        "token": result["token"],
        "public_ip": result.get("public_ip", ""),
        "assigned_domain": result.get("assigned_domain", ""),
        "public_endpoint": result.get("public_endpoint", ""),
        "message": "\u6ce8\u518c\u5df2\u901a\u8fc7",
    })
    _push_sse_result(request_id, data)
    _record_admin_event(request, "APPROVE_NODE", "node", f"通过节点注册：{result.get('server_id', '')}，请求：{request_id}")

    return {
        "ok": True,
        "message": f"\u5df2\u901a\u8fc7\uff1a{result['server_id']}",
        "server_id": result["server_id"],
        "assigned_domain": result.get("assigned_domain", ""),
        "public_endpoint": result.get("public_endpoint", ""),
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
    _record_admin_event(request, "REJECT_NODE", "node", f"拒绝节点注册：{request_id}，原因：{reason or '-'}")

    return {"ok": True, "message": "\u5df2\u62d2\u7edd"}


@app.post("/nodes/cancel-request")
async def cancel_node_request_by_se(request: Request):
    """SE 主动取消/废弃注册请求（用于等待审批期间点击取消）。"""
    body = await request.json() if await request.body() else {}
    request_id = (body.get("request_id") or "").strip()
    reason = (body.get("reason") or "node_cancelled").strip()
    force_discard_approved = bool(body.get("force_discard_approved", True))

    if not request_id:
        return {"ok": False, "error": "request_id is required"}

    result = database.cancel_node_request(
        request_id=request_id,
        reason=reason,
        reviewer="se_node",
        force_discard_approved=force_discard_approved,
    )
    if not result.get("ok"):
        return result

    action = result.get("action", "")
    if action == "cancelled_pending":
        data = _json.dumps({
            "approved": False,
            "reason": "节点已取消本次注册申请",
            "message": "注册已取消",
        })
        _push_sse_result(request_id, data)
    elif action == "discarded_approved":
        sid = result.get("server_id", "")
        if sid:
            node_state.manager.set_deleted(sid)
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: domain_pool.release_server_domain(sid),
            )
        data = _json.dumps({
            "approved": False,
            "reason": "该申请已被节点端废弃（审批结果已丢弃）",
            "message": "注册已废弃",
        })
        _push_sse_result(request_id, data)

    return result


@app.post("/api/nodes/{server_id}/delete")
async def delete_node(request: Request, server_id: str):

    """管理员彻底删除已批准节点"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok = database.delete_node(server_id)
    if not ok:
        return {"ok": False, "error": "Node not found"}
    node_state.manager.set_deleted(server_id)
    release_result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: domain_pool.release_server_domain(server_id),
    )
    if release_result and release_result.get("error"):
        log.warning(
            "Domain release failed after node deletion: %s (%s)",
            server_id,
            release_result.get("error"),
        )
    _record_admin_event(request, "DELETE_NODE", "node", f"删除节点：{server_id}")
    return {
        "ok": True,
        "message": f"\u5df2\u5220\u9664\uff1a{server_id}",
        "domain_release": release_result,
    }


@app.post("/api/nodes/{server_id}/suspend")
async def suspend_node(request: Request, server_id: str):
    """管理员暂停节点（停止访问，标黄）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok, msg = node_state.manager.set_suspended(server_id)
    if not ok:
        return {"ok": False, "error": msg}
    # 同步到 DB（管理员操作需要持久化）
    database.suspend_node(server_id)
    _sync_node_state_to_db(server_id)
    _record_admin_event(request, "SUSPEND_NODE", "node", f"暂停节点：{server_id}")
    return {"ok": True, "message": f"\u5df2\u6682\u505c\uff1a{server_id}"}


@app.post("/api/nodes/{server_id}/resume")
async def resume_node(request: Request, server_id: str):
    """管理员恢复被暂停的节点"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    ok, msg = node_state.manager.set_resumed(server_id)
    if not ok:
        return {"ok": False, "error": msg}
    # 同步到 DB
    database.resume_node(server_id)
    _sync_node_state_to_db(server_id)
    _record_admin_event(request, "RESUME_NODE", "node", f"恢复节点：{server_id}")
    return {"ok": True, "message": f"\u5df2\u6062\u590d\uff1a{server_id}"}


@app.post("/api/nodes/{server_id}/occupy")
async def occupy_node(request: Request, server_id: str):
    """客户端连接 SE 前，标记节点被占用（需登录 token，且 token 用户与 username 一致）"""
    from auth import get_client_username

    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    token_user = get_client_username(token)
    if not token_user:
        return {"ok": False, "error": "Unauthorized"}

    body = await request.json() if await request.body() else {}
    username = (body.get("username") or "").strip()
    connection_id = (body.get("connection_id") or "").strip()
    if not username:
        return {"ok": False, "error": "username_required"}
    if not connection_id:
        return {"ok": False, "error": "connection_id_required"}
    if username != token_user:
        return {
            "ok": False,
            "error": "username_token_mismatch",
            "message": "请求用户名与登录会话不一致",
        }

    target_state = node_state.manager.get(server_id)
    account = database.get_account_by_username(token_user)
    bound_endpoint = database.resolve_trade_server_address(account)
    if not account or account.get("status") != "active":
        return {"ok": False, "error": "account_inactive_or_missing"}
    if not endpoint_matches_node(bound_endpoint, target_state):
        return {
            "ok": False,
            "error": "node_not_bound_to_account",
            "message": "该交易账号未绑定当前交易服务器",
        }

    # 检查是否已被其他账户占用（从内存读取）
    occ = node_state.manager.get_occupation_info(server_id)
    if occ and occ.get("occupied_by") and occ["occupied_by"] != token_user:
        return {
            "ok": False,
            "error": "occupied",
            "message": f"\u8282\u70b9\u5df2\u88ab\u8d26\u6237 '{occ['occupied_by']}' \u5360\u7528",
        }

    ok, err_msg = node_state.manager.occupy(
        server_id,
        token_user,
        token,
        connection_id,
    )
    if not ok:
        return {"ok": False, "error": err_msg}
    return {"ok": True, "message": f"\u8282\u70b9\u5df2\u88ab '{token_user}' \u5360\u7528"}



@app.post("/api/nodes/{server_id}/release")
async def release_node(request: Request, server_id: str):
    """客户端断开 SE 连接后，释放节点占用（需登录 token，且只能释放本人占用）"""
    from auth import get_client_username

    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    token_user = get_client_username(token)
    if not token_user:
        return {"ok": False, "error": "Unauthorized"}

    body = await request.json() if await request.body() else {}
    connection_id = str(body.get("connection_id") or "").strip()
    if not connection_id:
        return {"ok": False, "error": "connection_id_required"}

    occ = node_state.manager.get_occupation_info(server_id)
    if occ and occ.get("occupied_by") and occ["occupied_by"] != token_user:
        return {
            "ok": False,
            "error": "forbidden",
            "message": f"节点当前由 '{occ['occupied_by']}' 占用，不能由 '{token_user}' 释放",
        }

    released = node_state.manager.release_session(
        server_id,
        token_user,
        token,
        connection_id,
        check_offline=False,
    )
    return {
        "ok": True,
        "released": bool(released),
        "message": "\u5df2\u91ca\u653e\u8282\u70b9" if released else "\u8282\u70b9\u5360\u7528\u5df2\u53d8\u66f4",
    }


@app.post("/api/nodes/{server_id}/force-release")
async def force_release_node(request: Request, server_id: str):
    """管理员强制解除占用：先踢掉 TS 上客户端连接，再释放节点占用。"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    occ = node_state.manager.get_occupation_info(server_id)
    occupied_by = (occ or {}).get("occupied_by", "") if isinstance(occ, dict) else ""
    if not occupied_by:
        return {"ok": False, "error": "not_occupied", "message": "节点当前未被占用"}

    st = node_state.manager.get(server_id)
    if not st:
        return {"ok": False, "error": "node_not_found"}

    ts_host = (st._public_endpoint or st._assigned_domain or st._host or st.current_ip or st._public_ip or "").strip()

    node_token = (st._token or "").strip()
    if not ts_host or not node_token:
        return {"ok": False, "error": "ts_endpoint_missing", "message": "缺少 TS 地址或节点令牌"}

    admin = _get_admin_session(request) or {}
    operator = (admin.get("username") or "admin").strip()
    reason = f"force_release_by_admin:{operator}"

    loop = asyncio.get_event_loop()
    ok, payload = await loop.run_in_executor(
        None,
        lambda: _force_disconnect_ts_clients(ts_host, node_token, reason, 8),
    )
    if not ok:
        return {
            "ok": False,
            "error": "ts_force_disconnect_failed",
            "message": "强制断开客户端失败，节点占用未释放",
            "detail": payload,
        }

    node_state.manager.release(server_id, check_offline=False)
    _sync_node_state_to_db(server_id)
    _record_admin_event(request, "FORCE_RELEASE_NODE", "node", f"强制释放节点：{server_id}，原占用账户：{occupied_by or '-'}")
    log.warning(f"[force-release] node={server_id}, occupied_by={occupied_by}, operator={operator}, ts={ts_host}")
    return {
        "ok": True,
        "message": f"已强制解除占用：{server_id}",
        "server_id": server_id,
        "occupied_by": occupied_by,
        "kicked": int((payload or {}).get("kicked", 0) or 0),
    }


# ── 券商类型列表 ───────────────────────────────────────────────────────


@app.get("/api/broker-types")
async def list_broker_types(request: Request):
    """返回支持的券商类型列表（前端下拉选项用，需管理员登录）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    from database import BROKER_TYPES
    return {"ok": True, "data": BROKER_TYPES}


# ── 节点券商配置管理 ─────────────────────────────────────────────────

@app.get("/api/nodes/config")
async def get_node_config(request: Request, server_id: str = "", token: str = ""):
    """
    SE 拉取自身券商配置
    
    鉴权方式：通过 query param 传递 node_token
    """
    if not token:
        # 也支持 Bearer header
        auth_header = request.headers.get("authorization", "")
        token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""

    if not token or not database.verify_node_token_for_config(token, server_id):
        return {"ok": False, "error": "Unauthorized", "config_version": -1}

    cfg = database.get_node_broker_config(server_id)
    if not cfg:
        return {"ok": False, "error": "Node not found", "config_version": -1}

    trace_id = request.headers.get("x-trace-id", "") or f"trc_{uuid.uuid4().hex[:16]}"
    return {
        "ok": True,
        "server_id": server_id,
        "config_version": cfg["config_version"],
        "broker_type": cfg["broker_type"],
        "credentials": cfg["credentials"],
        "enabled": cfg["enabled"],
        "trace_id": trace_id,
    }



@app.put("/api/nodes/{server_id}/config")
async def update_node_config(request: Request, server_id: str):
    """管理员修改节点的券商配置。"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    body = await request.json()
    broker_type = body.get("broker_type", "")
    credentials = body.get("credentials", {})
    enabled = body.get("enabled", True)

    if not broker_type:
        return {"ok": False, "error": "broker_type is required"}

    ok = database.set_node_broker_config(
        server_id=server_id,
        broker_type=broker_type,
        credentials=credentials,
        enabled=enabled,
    )
    if ok:
        cfg = database.get_node_broker_config(server_id)
        if cfg:
            _push_config_change(server_id, int(cfg.get("config_version", 0) or 0))
        _record_admin_event(request, "UPDATE_NODE_CONFIG", "node", f"修改节点配置：{server_id}，券商：{broker_type}")
        return {"ok": True, "message": f"\u914d\u7f6e\u5df2\u66f4\u65b0 ({server_id})"}

    return {"ok": False, "error": "\u66f4\u65b0\u5931\u8d25"}


@app.post("/api/nodes/{server_id}/reload")
async def reload_node_config(request: Request, server_id: str):
    """管理员触发 SE 重载券商配置（递增版本号通知SE拉取新配置）"""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    new_ver = database.increment_reload_flag(server_id)
    if new_ver > 0:
        _push_config_change(server_id, int(new_ver))
        _record_admin_event(request, "RELOAD_NODE_CONFIG", "node", f"触发节点配置重载：{server_id}，版本：{new_ver}")
        return {"ok": True, "message": f"\u5df2\u89e6\u53d1\u91cd\u8f7d\uff0c\u7248\u672c={new_ver}", "config_version": new_ver}

    return {"ok": False, "error": "\u8282\u70b9\u4e0d\u5b58\u5728"}




@app.post("/api/admin/profile/update-name")
async def update_super_admin_name(request: Request):
    """超级管理员修改自己的账户名称（当前不包含密码修改）。"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}
    if (sess.get("role") or "") != "super_admin":
        return {"ok": False, "error": "仅超级管理员可操作"}

    body = await request.json() if await request.body() else {}
    new_username = (body.get("username") or "").strip()
    if not new_username:
        return {"ok": False, "error": "用户名不能为空"}

    ok, msg = database.rename_super_admin_username(sess.get("id", 0), new_username)
    if not ok:
        return {"ok": False, "error": msg}

    sid = _get_session_id(request)
    if sid in _admin_sessions:
        _admin_sessions[sid]["username"] = new_username

    _record_admin_event(request, "UPDATE_ADMIN_NAME", "account", f"修改超级管理员账户名称：{new_username}")
    return {"ok": True, "message": "超级管理员账户名称已更新", "username": new_username}


@app.post("/api/admin/profile/update-password")
async def update_super_admin_password(request: Request):
    """超级管理员修改自己的登录密码，成功后强制退出当前 Web 管理会话。"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}
    if (sess.get("role") or "") != "super_admin":
        return {"ok": False, "error": "仅超级管理员可操作"}

    body = await request.json() if await request.body() else {}
    current_password = (body.get("current_password") or "").strip()
    new_password = (body.get("new_password") or "").strip()

    ok, msg = database.update_super_admin_password(
        account_id=sess.get("id", 0),
        current_password=current_password,
        new_password=new_password,
    )
    if not ok:
        return {"ok": False, "error": msg}

    sid = _get_session_id(request)
    if sid in _admin_sessions:
        del _admin_sessions[sid]

    _record_admin_event(request, "UPDATE_ADMIN_PASSWORD", "account", f"修改超级管理员密码：{sess.get('username', '')}")
    return {
        "ok": True,
        "message": "密码已更新，请重新登录",
        "logout_required": True,
        "redirect": "/admin/login",
    }


# ── 账户管理 API（管理员用）────────────────────────────────────────────


@app.get("/api/accounts/list")
async def list_accounts(request: Request):
    """获取账户列表（按管理角色裁剪）"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}

    viewer_role = sess.get("role", "admin")
    viewer_name = sess.get("username", "")
    accounts = database.get_all_accounts()

    # 一般管理员：可见交易员 + 其他管理员（不含超级管理员）
    if viewer_role != "super_admin":
        accounts = [
            a for a in accounts
            if (a.get("role") in ("trader", "admin")) and not (a.get("role") == "admin" and a.get("username") == viewer_name)
        ]

    return {"ok": True, "data": accounts, "viewer": {"username": viewer_name, "role": viewer_role}}



@app.get("/api/system/health")
async def get_system_health(request: Request):
    """Return SM runtime health summary for production monitoring."""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}

    offline_ids = node_state.manager.check_offline_nodes()
    if offline_ids:
        _sync_node_state_to_db()
    nodes = _get_realtime_nodes_for_display()
    node_counts = {
        "total": len(nodes),
        "online": sum(1 for n in nodes if n.get("real_status") == "online"),
        "occupied": sum(1 for n in nodes if n.get("real_status") == "occupied"),
        "offline": sum(1 for n in nodes if n.get("real_status") == "offline"),
        "suspended": sum(1 for n in nodes if n.get("real_status") == "suspended"),
    }
    accounts = database.get_all_accounts()
    error_lines = read_recent_error_lines(1000)
    return {
        "ok": True,
        "service": "server_manager",
        "nodes": node_counts,
        "accounts": {
            "total": len(accounts),
            "active": sum(1 for a in accounts if (a.get("status") or "active") == "active"),
            "suspended": sum(1 for a in accounts if (a.get("status") or "") == "suspended"),
        },
        "audit": {
            "recent_7d": database.count_audit_logs(days=7),
        },
        "logs": {
            "runtime_log_exists": LOG_FILE.exists(),
            "runtime_log_bytes": LOG_FILE.stat().st_size if LOG_FILE.exists() else 0,
            "error_log_exists": ERROR_LOG_FILE.exists(),
            "error_log_bytes": ERROR_LOG_FILE.stat().st_size if ERROR_LOG_FILE.exists() else 0,
            "recent_error_lines": len(error_lines),
        },
    }


@app.get("/api/system/error-logs")
async def get_system_error_logs(request: Request, limit: int = 200):
    """Return recent SM runtime error logs for production troubleshooting."""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    safe_limit = max(1, min(int(limit or 200), 1000))
    return {"ok": True, "lines": read_recent_error_lines(safe_limit)}


@app.get("/api/system/error-logs/export")
async def export_system_error_logs(request: Request, limit: int = 2000):
    """Export recent SM runtime error logs as plain text."""
    if not _is_admin_logged_in(request):
        return PlainTextResponse("Unauthorized", status_code=401)
    safe_limit = max(1, min(int(limit or 2000), 5000))
    content = read_error_log_text(safe_limit) or "No SM error logs.\n"
    return PlainTextResponse(
        content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=sm_error.log"},
    )


@app.get("/api/audit/recent")
async def get_recent_audit_events(request: Request, limit: int = 10):
    """Return the newest SM operation events for overview."""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    safe_limit = max(1, min(int(limit or 10), 50))
    return {"ok": True, "data": database.get_audit_logs(limit=safe_limit, days=7)}


@app.get("/api/audit/logs")
async def get_audit_logs(request: Request, days: int = 7, page: int = 1, limit: int = 50):
    """Return paged SM operation logs within the recent days window."""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    safe_days = max(1, min(int(days or 7), 30))
    safe_page = max(1, int(page or 1))
    safe_limit = max(1, min(int(limit or 50), 200))
    total = database.count_audit_logs(days=safe_days)
    max_page = max(1, (total + safe_limit - 1) // safe_limit)
    safe_page = min(safe_page, max_page)
    offset = (safe_page - 1) * safe_limit
    data = database.get_audit_logs(limit=safe_limit, days=safe_days, offset=offset)
    return {
        "ok": True,
        "data": data,
        "page": safe_page,
        "limit": safe_limit,
        "total": total,
        "has_more": offset + len(data) < total,
    }


@app.post("/api/audit/cleanup")
async def cleanup_audit_logs(request: Request, retention_days: int = 30):
    """Manually clean old SM operation logs."""
    if not _is_admin_logged_in(request):
        return {"ok": False, "error": "Unauthorized"}
    deleted = database.cleanup_audit_logs(retention_days=retention_days)
    _record_admin_event(request, "CLEAN_AUDIT_LOG", "audit", f"清理 {retention_days} 天前日志，删除 {deleted} 条")
    return {"ok": True, "deleted": deleted}


@app.post("/api/accounts/create")
async def create_account(request: Request):
    """创建新账户（超级管理员可建 admin/trader，一般管理员仅可建 trader）"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}

    body = await request.json()
    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()
    role = (body.get("role") or "trader").strip().lower()
    se_address = (body.get("se_address") or "").strip()
    broker_tag = (body.get("broker_tag") or "").strip()
    description = (body.get("description") or "").strip()

    if not username:
        return {"ok": False, "error": "用户名不能为空"}
    if not password:
        return {"ok": False, "error": "密码不能为空"}
    if role not in ("trader", "admin"):
        return {"ok": False, "error": "角色不合法"}

    viewer_role = sess.get("role", "admin")
    if viewer_role != "super_admin" and role != "trader":
        return {"ok": False, "error": "仅超级管理员可创建管理员账户"}

    # 交易员保持现有注册约束；管理员无需 SE/券商
    if role == "trader":
        if not se_address:
            return {"ok": False, "error": "SE 地址不能为空"}
        if not broker_tag:
            return {"ok": False, "error": "券商不能为空"}
    else:
        se_address = ""
        broker_tag = ""

    result = database.create_account(
        username=username,
        password=password,
        role=role,
        se_address=se_address,
        broker_tag=broker_tag,
        description=description,
    )
    if not result:
        return {"ok": False, "error": f"创建失败（用户名 '{username}' 可能已存在）"}

    result.pop("password_hash", None)
    role_text = "管理员" if role == "admin" else "交易员"
    _record_admin_event(request, "CREATE_ACCOUNT", "account", f"创建{role_text}账户：{username}")
    return {"ok": True, "data": result, "message": f"{role_text}账户 {username} 创建成功"}



@app.post("/api/accounts/{account_id}/delete")
async def delete_account(request: Request, account_id: int):
    """删除账户"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}

    target = database.get_account_by_id(account_id)
    if not target:
        return {"ok": False, "error": "账户不存在"}

    viewer_role = sess.get("role", "admin")
    target_role = target.get("role", "trader")

    if target_role == "super_admin":
        return {"ok": False, "error": "超级管理员账户不支持删除"}
    if viewer_role != "super_admin" and target_role != "trader":
        return {"ok": False, "error": "无权限删除管理员账户"}

    ok = database.delete_account(account_id)
    if not ok:
        return {"ok": False, "error": "删除失败（可能不存在或受保护账号）"}
    from auth import invalidate_client_tokens_by_username
    invalidate_client_tokens_by_username(target.get("username", ""))
    await _disconnect_user_from_occupied_nodes(
        target.get("username", ""),
        "account_deleted",
    )
    _record_admin_event(request, "DELETE_ACCOUNT", "account", f"删除账户：{target.get('username', account_id)}")
    return {"ok": True, "message": f"账户已删除 (id={account_id})"}



@app.post("/api/accounts/{account_id}/suspend")
async def suspend_account(request: Request, account_id: int):
    """暂停账户"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}

    target = database.get_account_by_id(account_id)
    if not target:
        return {"ok": False, "error": "账户不存在"}

    viewer_role = sess.get("role", "admin")
    target_role = target.get("role", "trader")

    if target_role == "super_admin":
        return {"ok": False, "error": "超级管理员账户不支持暂停"}
    if viewer_role != "super_admin" and target_role != "trader":
        return {"ok": False, "error": "无权限暂停管理员账户"}

    ok = database.suspend_account(account_id)
    if not ok:
        return {"ok": False, "error": "暂停失败（可能已被暂停或不存在）"}
    from auth import invalidate_client_tokens_by_username
    invalidate_client_tokens_by_username(target.get("username", ""))
    await _disconnect_user_from_occupied_nodes(
        target.get("username", ""),
        "account_suspended",
    )
    _record_admin_event(request, "SUSPEND_ACCOUNT", "account", f"暂停账户：{target.get('username', account_id)}")
    return {"ok": True, "message": f"账户已暂停 (id={account_id})"}



@app.post("/api/accounts/{account_id}/resume")
async def resume_account(request: Request, account_id: int):
    """恢复被暂停的账户"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}

    target = database.get_account_by_id(account_id)
    if not target:
        return {"ok": False, "error": "账户不存在"}

    viewer_role = sess.get("role", "admin")
    target_role = target.get("role", "trader")

    if target_role == "super_admin":
        return {"ok": False, "error": "超级管理员账户不支持此操作"}
    if viewer_role != "super_admin" and target_role != "trader":
        return {"ok": False, "error": "无权限恢复管理员账户"}

    ok = database.resume_account(account_id)
    if not ok:
        return {"ok": False, "error": "恢复失败（可能未被暂停或不存在）"}
    _record_admin_event(request, "RESUME_ACCOUNT", "account", f"恢复账户：{target.get('username', account_id)}")
    return {"ok": True, "message": f"账户已恢复 (id={account_id})"}



@app.get("/api/accounts/{account_id}/detail")
async def get_account_detail(request: Request, account_id: int):
    """获取单个账户的详细信息"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}
    acct = database.get_account_by_id(account_id)
    if not acct:
        return {"ok": False, "error": "账户不存在"}

    viewer_role = sess.get("role", "admin")
    if viewer_role != "super_admin" and acct.get("role") == "super_admin":
        return {"ok": False, "error": "无权限查看该账户"}

    return {"ok": True, "data": acct}



@app.post("/api/accounts/{account_id}/update")
async def update_account(request: Request, account_id: int):
    """修改账户信息（当前仅支持交易员账户）"""
    sess = _get_admin_session(request)
    if not sess:
        return {"ok": False, "error": "Unauthorized"}

    target = database.get_account_by_id(account_id)
    if not target:
        return {"ok": False, "error": "账户不存在"}

    viewer_role = sess.get("role", "admin")
    target_role = target.get("role", "trader")

    if target_role != "trader":
        return {"ok": False, "error": "当前仅支持编辑交易员账户"}
    if viewer_role not in ("super_admin", "admin"):
        return {"ok": False, "error": "无权限编辑该账户"}

    body = await request.json()
    se_address = (body.get("se_address") or "").strip()
    broker_tag = (body.get("broker_tag") or "").strip()
    description = (body.get("description") or "").strip()
    password = (body.get("password") or "").strip()
    revoke_active_session = bool(
        password
        or se_address != database.resolve_trade_server_address(target)
        or broker_tag != (target.get("broker_tag") or "").strip()
    )

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
    if revoke_active_session:
        from auth import invalidate_client_tokens_by_username
        invalidate_client_tokens_by_username(target.get("username", ""))
        await _disconnect_user_from_occupied_nodes(
            target.get("username", ""),
            "account_updated",
        )
    _record_admin_event(request, "UPDATE_ACCOUNT", "account", f"更新账户：{target.get('username', account_id)}")
    return {"ok": True, "message": f"账户信息已更新 (id={account_id})"}



@app.get("/api/accounts/se-status")
async def check_se_status(request: Request, address: str = ""):
    """Return TS online/occupation state for an authenticated Client call."""
    from auth import get_client_username

    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""
    token_user = get_client_username(token)
    if not token_user:
        return {"ok": False, "error": "Unauthorized", "online": False}

    account = database.get_account_by_username(token_user)
    bound_endpoint = database.resolve_trade_server_address(account)
    if not account or account.get("status") != "active" or not bound_endpoint:
        return {
            "ok": False,
            "error": "account_binding_missing",
            "online": False,
        }

    supplied = address_candidates(address)
    bound = address_candidates(bound_endpoint)
    if supplied and not (supplied & bound):
        return {
            "ok": False,
            "error": "address_not_bound_to_account",
            "online": False,
        }

    requested = bound
    if not requested:
        return {
            "ok": True,
            "online": False,
            "reason": "empty address",
            "address": address,
            "occupied_by": "",
            "occupied_at": "",
        }

    for state in node_state.manager._states.values():
        candidates = set()
        candidates.update(address_candidates(getattr(state, "current_ip", "")))
        candidates.update(address_candidates(getattr(state, "_host", "")))
        candidates.update(address_candidates(getattr(state, "_public_ip", "")))
        candidates.update(address_candidates(getattr(state, "_assigned_domain", "")))
        candidates.update(address_candidates(getattr(state, "_public_endpoint", "")))
        if not (requested & candidates):
            continue
        if state.is_online:
            occ_info = node_state.manager.get_occupation_info(state.server_id)
            return {
                "ok": True,
                "online": True,
                "node_name": state._node_name,
                "server_id": state.server_id,
                "match_field": "memory",
                "address": address,
                "occupied_by": (occ_info or {}).get("occupied_by", ""),
                "occupied_at": (occ_info or {}).get("occupied_at", ""),
            }

    return {
        "ok": True,
        "online": False,
        "reason": f"未找到与地址 '{address}' 匹配的在线子服务器",
        "address": address,
        "occupied_by": "",
        "occupied_at": "",
    }

def _push_sse_result(request_id: str, data_json: str):
    """向指定 request_id 的所有 SSE 连接推送消息"""
    import asyncio
    queues = _node_sse_queues.get(request_id, [])
    for q in queues[:]:
        try:
            q.put_nowait(data_json)
        except asyncio.QueueFull:
            pass


def _push_config_change(server_id: str, version: int):
    """向指定 server_id 的配置事件 SSE 连接推送 config_changed"""
    import asyncio
    data = _json.dumps({"type": "CONFIG_CHANGED", "server_id": server_id, "config_version": version})
    queues = _config_sse_queues.get(server_id, [])
    for q in queues[:]:
        try:
            q.put_nowait(data)
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


# 纯内存巡检，5 秒一次可让 15 秒占用心跳阈值及时生效。
_HEARTBEAT_CHECK_INTERVAL = 5


async def _heartbeat_monitor_loop():
    """
    后台定期检测心跳超时的节点，自动标记为离线（内存操作）
    
    改进：
      - 被占用节点使用短超时（15秒），能更快检测掉线
      - 对接近超时的占用节点主动发起探活请求
      - 掉线时自动释放占用，防止节点被永久锁定
    """
    await asyncio.sleep(10)
    log.info("Heartbeat monitor started (check_interval=%ds, timeout=%ds/occupied=%ds)" %
             (_HEARTBEAT_CHECK_INTERVAL, node_state.HEARTBEAT_TIMEOUT, node_state.OCCUPIED_HEARTBEAT_TIMEOUT))
    while True:
        try:
            # 1. 检测离线节点（占用节点使用15秒超时）
            offline_ids = node_state.manager.check_offline_nodes()
            if offline_ids:
                log.info(f"Heartbeat monitor: {len(offline_ids)} node(s) marked as OFFLINE")

            expired_reservations = node_state.manager.expire_unconfirmed_occupations()
            if expired_reservations:
                log.info(
                    "Heartbeat monitor: %s unconfirmed occupation reservation(s) released",
                    len(expired_reservations),
                )
            
            # 2. 对接近超时的占用节点发起主动探活
            probe_targets = node_state.manager.get_nodes_need_probe()
            for target in probe_targets:
                sid = target["server_id"]
                log.info(
                    f"[Probe] Active probing occupied node {sid} "
                    f"(occupied by '{target['occupied_by']}', "
                    f"{target['seconds_until_timeout']:.1f}s until timeout)"
                )
                node_state.manager.mark_node_probing(sid)
                # 注意：实际探活可通过 HTTP 请求 SE 的 /health 端点实现，
                # 此处标记后由下次心跳或外部探活机制处理
                
        except Exception as e:
            log.error(f"Heartbeat monitor error: {e}")
        await asyncio.sleep(_HEARTBEAT_CHECK_INTERVAL)


# 定期将内存状态同步到数据库（用于崩溃恢复），间隔 5 分钟
_DB_SYNC_INTERVAL = 300


async def _db_sync_loop():
    """后台定期将内存状态同步到 SQLite"""
    await asyncio.sleep(60)  # 启动后等1分钟再开始首次同步
    while True:
        try:
            states = node_state.manager.prepare_db_sync_data()
            if states:
                database.sync_node_states_to_db(states)
        except Exception as e:
            log.error(f"DB sync error: {e}")
        await asyncio.sleep(_DB_SYNC_INTERVAL)


@app.on_event("startup")
async def startup():
    """应用启动时执行初始化"""
    from services.caddy_manager import configure_and_start_caddy

    caddy_result = await asyncio.to_thread(configure_and_start_caddy)
    if caddy_result.get("ok"):
        log.info("[Startup] Caddy setup: %s", caddy_result)
    else:
        log.warning("[Startup] Caddy setup unavailable: %s", caddy_result)
        if SM_CADDY_REQUIRED:
            raise RuntimeError(f"Required Caddy setup failed: {caddy_result.get('reason', 'unknown error')}")

    # 启动会话过期清理任务
    asyncio.create_task(_session_cleanup_loop())
    # 启动节点注册请求过期清理任务
    asyncio.create_task(_node_expire_cleanup_loop())
    # 启动心跳超时检测任务（内存操作）
    asyncio.create_task(_heartbeat_monitor_loop())
    # 初始化数据库
    try:
        init_db()
    except Exception as e:
        log.warning(f"Database init skipped (non-critical): {e}")

    # ── 加载已批准节点到内存状态管理器 ──
    try:
        db_rows = database.get_approved_nodes_for_memory_load()
        loaded = node_state.manager.load_from_db_rows(db_rows)
        log.info(f"[Startup] loaded {loaded} approved nodes into memory state")
        # Client Token 与 connection_id 都是进程内状态，清除 DB 中的历史占用快照。
        _sync_node_state_to_db()
        
        # ★ 启动后立即执行一次离线检测
        # 防止 DB 中残留的 online/occupied 状态被错误展示
        offline_on_boot = node_state.manager.check_offline_nodes()
        if offline_on_boot:
            log.info(f"[Startup] corrected {len(offline_on_boot)} node(s) to OFFLINE (stale DB data)")
    except Exception as e:
        log.warning(f"[Startup] failed to load nodes into memory: {e}")

    # 启动定期 DB 同步任务
    asyncio.create_task(_db_sync_loop())

    # 旧 SM 行情链路默认关闭；生产行情走 Client -> TS -> Broker
    if SM_ENABLE_LEGACY_QUOTES:
        from services.quote_service import ib_preconnect, quote_stream_loop
        asyncio.create_task(ib_preconnect())
        asyncio.create_task(quote_stream_loop())

    log.info("=" * 60)
    log.info(f"Server Manager started on {SERVER_HOST}:{SERVER_PORT}")
    log.info("Mode: CONTROL_PLANE (trading execution is handled by SE)")
    log.info(f"Legacy quote service enabled: {SM_ENABLE_LEGACY_QUOTES}")
    log.info("=" * 60)



@app.on_event("shutdown")
async def shutdown():
    """应用关闭时清理资源"""
    # 将当前内存状态同步回数据库（用于下次启动恢复）
    try:
        states = node_state.manager.prepare_db_sync_data()
        if states:
            database.sync_node_states_to_db(states)
            log.info(f"[Shutdown] synced {len(states)} node states to DB")
    except Exception as e:
        log.warning(f"[Shutdown] DB sync failed (non-critical): {e}")

    session_store["session"] = None
    session_store["account"] = None
    session_store["connected"] = False
    quote_clients.clear()
    subscribed_syms.clear()
    log.info("Server Manager shut down cleanly")


# ── 直接运行 ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=SERVER_HOST,
        port=SERVER_PORT,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
