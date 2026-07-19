"""
Configuration Management
环境变量加载、全局配置、运行时状态存储
"""

import os
import hashlib
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── 日志配置 ──────────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).resolve().parent / "data"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "sm.log"
ERROR_LOG_FILE = LOG_DIR / "sm_error.log"
LOG_LEVEL = os.environ.get("SM_LOG_LEVEL", "INFO").upper()
LOG_MAX_BYTES = int(os.environ.get("SM_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("SM_LOG_BACKUP_COUNT", "5"))


def init_logging() -> None:
    """Initialize SM runtime and error log files."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    )

    root = logging.getLogger()
    if getattr(root, "_sm_logging_ready", False):
        return

    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(formatter)

    runtime_file = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    runtime_file.setLevel(level)
    runtime_file.setFormatter(formatter)

    error_file = RotatingFileHandler(
        ERROR_LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(formatter)

    root.addHandler(console)
    root.addHandler(runtime_file)
    root.addHandler(error_file)
    root._sm_logging_ready = True


def read_recent_error_lines(limit: int = 200) -> list[str]:
    """Read recent SM error log lines for admin troubleshooting."""
    safe_limit = max(1, min(int(limit or 200), 1000))
    if not ERROR_LOG_FILE.exists():
        return []
    try:
        with open(ERROR_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-safe_limit:]
    except OSError:
        return []


def read_error_log_text(limit: int = 2000) -> str:
    """Return recent SM error log text for manual export."""
    return "".join(read_recent_error_lines(limit))


init_logging()
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
SERVER_HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8800"))

# ── Tastytrade 券商凭据 ──────────────────────────────────────────────────
_TASTY_SECRET = os.environ.get("TASTY_SECRET", "")
_TASTY_TOKEN = os.environ.get("TASTY_TOKEN", "")

# ── IB TWS 行情源配置 ────────────────────────────────────────────────────
SM_ENABLE_LEGACY_QUOTES = os.environ.get("SM_ENABLE_LEGACY_QUOTES", "0").strip().lower() in {"1", "true", "yes"}
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
