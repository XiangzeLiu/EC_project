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


@app.on_event("startup")
async def startup():
    """应用启动时执行初始化"""
    # 启动会话过期清理任务
    asyncio.create_task(_session_cleanup_loop())
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
