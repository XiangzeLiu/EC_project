"""
Configuration Management
环境变量加载、全局配置、运行时状态存储
"""

import os
import hashlib
import logging

# ── 日志配置 ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("server_manager")

# 屏蔽 SDK 内部 HTTP 日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("tastytrade").setLevel(logging.WARNING)

# ── 服务器认证凭据（客户端连此服务器用）────────────────────────────────
SERVER_USERNAME = os.environ.get("SERVER_USERNAME", "admin")
SERVER_PASSWORD = os.environ.get("SERVER_PASSWORD", "changeme123")
SERVER_TOKEN = hashlib.sha256(
    f"{SERVER_USERNAME}:{SERVER_PASSWORD}".encode()
).hexdigest()

# ── 服务监听地址 ──────────────────────────────────────────────────────────
SERVER_HOST = os.environ.get("SERVER_HOST", "0.0.0.0")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8800"))

# ── Tastytrade 券商凭据 ──────────────────────────────────────────────────
_TASTY_SECRET = os.environ.get("TASTY_SECRET", "")
_TASTY_TOKEN = os.environ.get("TASTY_TOKEN", "")

# ── IB TWS 行情源配置 ────────────────────────────────────────────────────
IB_HOST = os.environ.get("IB_HOST", "127.0.0.1")
IB_PORT = int(os.environ.get("IB_PORT", "7496"))
IB_CLIENT_ID = int(os.environ.get("IB_CLIENT_ID", "19"))

# ── 运行时状态（内存 Session Store）─────────────────────────────────────
session_store = {
    "session": None,      # 复用的 Tastytrade Session 对象
    "account": None,      # 复用的 Tastytrade Account 对象
    "secret": _TASTY_SECRET,
    "token": _TASTY_TOKEN,
    "acct_num": "",
    "connected": False,
}

# 已登录的客户端 Token 集合（用于 verify_token 校验）
active_client_tokens: dict[str, dict] = {}  # {token: {username, created_at}}

# 行情 WebSocket 客户端列表
quote_clients: list = []
# 当前订阅的标的集合
subscribed_syms: set = set()


def is_configured() -> bool:
    """检查 Tastytrade 凭据是否已配置"""
    return bool(_TASTY_SECRET and _TASTY_TOKEN)


def load_users_from_json() -> list[dict]:
    """从 users.json 加载用户列表"""
    import json as _json
    _path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
    try:
        with open(_path, "r", encoding="utf-8") as _f:
            return _json.load(_f)
    except Exception:
        return []
