"""
Configuration Management
环境变量加载、全局配置、运行时状态存储
"""

import os
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


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_csv(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]

# ── 服务监听地址 ──────────────────────────────────────────────────────────
SERVER_HOST = os.environ.get("SERVER_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8800"))

# ── Production public entry ──────────────────────────────────────────────
SM_PUBLIC_BASE_URL = os.environ.get(
    "SM_PUBLIC_BASE_URL",
    "https://scjrdomain.com",
).strip().rstrip("/")
SM_ALLOWED_HOSTS = _env_csv(
    "SM_ALLOWED_HOSTS",
    "scjrdomain.com,127.0.0.1,localhost,testserver",
)
SM_CORS_ORIGINS = _env_csv(
    "SM_CORS_ORIGINS",
    "https://scjrdomain.com,http://127.0.0.1:8800,http://localhost:8800",
)
SM_COOKIE_SECURE = _env_bool("SM_COOKIE_SECURE", False)
SM_COOKIE_SAMESITE = os.environ.get("SM_COOKIE_SAMESITE", "lax").strip().lower() or "lax"
# Production client authentication lifetime. Keep overridable for controlled tests.
CLIENT_TOKEN_TTL_SECONDS = max(60, int(os.environ.get("CLIENT_TOKEN_TTL_SECONDS", "86400")))

# Software center artifacts are stored outside the packaged application when
# configured for production. The data-directory fallback is kept for local use.
SM_SOFTWARE_STORAGE_DIR = Path(
    os.environ.get("SM_SOFTWARE_STORAGE_DIR", str(DATA_DIR / "software"))
).expanduser()
SM_SOFTWARE_MAX_UPLOAD_BYTES = max(
    1 * 1024 * 1024,
    int(os.environ.get("SM_SOFTWARE_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024))),
)
SM_SOFTWARE_RETENTION_COUNT = max(
    1,
    int(os.environ.get("SM_SOFTWARE_RETENTION_COUNT", "3")),
)
SM_SOFTWARE_SESSION_MAX_AGE = max(
    300,
    int(os.environ.get("SM_SOFTWARE_SESSION_MAX_AGE", "7200")),
)
SM_SOFTWARE_ALLOWED_EXTENSIONS = frozenset({".exe", ".zip", ".msi"})

# Used only when a fresh database has no super administrator. Existing accounts
# are never reset from these values during startup.
SM_BOOTSTRAP_ADMIN_USERNAME = (
    os.environ.get("SM_BOOTSTRAP_ADMIN_USERNAME", "admin").strip() or "admin"
)
SM_BOOTSTRAP_ADMIN_PASSWORD = os.environ.get("SM_BOOTSTRAP_ADMIN_PASSWORD", "admin123")

# ── Local Caddy process management ──────────────────────────────────────
SM_CADDY_AUTO_MANAGE = _env_bool("SM_CADDY_AUTO_MANAGE", True)
SM_CADDY_REQUIRED = _env_bool("SM_CADDY_REQUIRED", False)
SM_CADDY_EXE = os.environ.get("SM_CADDY_EXE", "").strip()
SM_CADDY_DIR = os.environ.get("SM_CADDY_DIR", "").strip()
SM_CADDY_ADMIN = os.environ.get("SM_CADDY_ADMIN", "127.0.0.1:2019").strip() or "127.0.0.1:2019"
SM_CADDY_START_TIMEOUT = max(
    1.0,
    float(os.environ.get("SM_CADDY_START_TIMEOUT", "10")),
)

# ── TS domain pool and Tencent Cloud DNSPod ─────────────────────────────
SM_DOMAIN_ROOT = "scjrdomain.com"
SM_TS_WS_PATH = os.environ.get("SM_TS_WS_PATH", "/ws").strip() or "/ws"
if not SM_TS_WS_PATH.startswith("/"):
    SM_TS_WS_PATH = f"/{SM_TS_WS_PATH}"

SM_DOMAIN_POOL_REQUIRED = _env_bool("SM_DOMAIN_POOL_REQUIRED", True)
# 联调阶段默认关闭；正式上线前通过环境变量恢复为 1800 秒。
SM_DOMAIN_COOLDOWN_SECONDS = max(
    0,
    int(os.environ.get("SM_DOMAIN_COOLDOWN_SECONDS", "0")),
)

# Production is always real mode. The environment override remains available to
# automated tests so mock DNS never reaches a production deployment by default.
SM_DNSPOD_MODE = os.environ.get("SM_DNSPOD_MODE", "real").strip().lower()
if SM_DNSPOD_MODE not in {"mock", "manual", "real", "disabled"}:
    SM_DNSPOD_MODE = "real"
SM_DNSPOD_SECRET_ID = os.environ.get("SM_DNSPOD_SECRET_ID", "").strip()
SM_DNSPOD_SECRET_KEY = os.environ.get("SM_DNSPOD_SECRET_KEY", "").strip()
SM_DNSPOD_LINE = "默认"
SM_DNS_TTL = 600

# ── Tastytrade 券商凭据 ──────────────────────────────────────────────────
_TASTY_SECRET = os.environ.get("TASTY_SECRET", "")
_TASTY_TOKEN = os.environ.get("TASTY_TOKEN", "")

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


def is_configured() -> bool:
    """检查 Tastytrade 凭据是否已配置"""
    return bool(_TASTY_SECRET and _TASTY_TOKEN)

