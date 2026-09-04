"""
Configuration Management
环境变量加载、全局配置、运行时状态存储
"""

import os
import logging
import sqlite3
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

from data_layout import (
    DataLayoutError,
    ensure_deployment_config,
    is_within,
    load_data_manifest,
    load_deployment_config,
    relative_path,
    resolve_relative_path,
    validate_data_paths,
    write_data_manifest,
)


_IS_FROZEN = bool(getattr(sys, "frozen", False))
_DEFAULT_ENVIRONMENT = "production" if _IS_FROZEN else "development"
SM_ENVIRONMENT = os.environ.get("SM_ENVIRONMENT", _DEFAULT_ENVIRONMENT).strip().lower()
if SM_ENVIRONMENT not in {"development", "production", "selftest"}:
    SM_ENVIRONMENT = _DEFAULT_ENVIRONMENT


def _default_data_dir() -> Path:
    configured = os.environ.get("SM_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if _IS_FROZEN:
        program_data = os.environ.get("PROGRAMDATA", "").strip()
        if program_data:
            return (Path(program_data) / "SC" / "ServerManager" / "data").resolve()
    return Path(__file__).resolve().parent / "data"


# ── 日志配置 ──────────────────────────────────────────────────────────────
DATA_DIR = _default_data_dir()
RUNTIME_DIR = DATA_DIR.parent
try:
    DEPLOYMENT_CONFIG = load_deployment_config(DATA_DIR)
    DATA_MANIFEST = load_data_manifest(DATA_DIR)
    DEPLOYMENT_CONFIG_ERROR = ""
except DataLayoutError as exc:
    DEPLOYMENT_CONFIG = {}
    DATA_MANIFEST = None
    DEPLOYMENT_CONFIG_ERROR = str(exc)


def _configured_value(key: str, env_name: str, default):
    """Read persisted deployment values before legacy environment defaults."""
    if key in DEPLOYMENT_CONFIG:
        return DEPLOYMENT_CONFIG[key]
    return os.environ.get(env_name, default)


def _configured_bool(key: str, env_name: str, default: bool) -> bool:
    value = _configured_value(key, env_name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _configured_path(key: str, env_name: str, default: str) -> str:
    if env_name in os.environ:
        return os.environ.get(env_name, default).strip()
    if key in DEPLOYMENT_CONFIG:
        value = str(DEPLOYMENT_CONFIG.get(key) or "").strip()
        if not value:
            return ""
        try:
            return str(resolve_relative_path(DATA_DIR, value))
        except DataLayoutError as exc:
            global DEPLOYMENT_CONFIG_ERROR
            DEPLOYMENT_CONFIG_ERROR = str(exc)
            return ""
    return str(default).strip()


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
SERVER_HOST = str(_configured_value("server_host", "SERVER_HOST", "127.0.0.1")).strip()
SERVER_PORT = int(_configured_value("server_port", "SERVER_PORT", "18800"))
SM_PUBLIC_HTTP_PORT = int(
    _configured_value("public_http_port", "SM_PUBLIC_HTTP_PORT", "8800")
)
SM_PUBLIC_HTTPS_PORT = int(
    _configured_value("public_https_port", "SM_PUBLIC_HTTPS_PORT", "4430")
)

# ── Production public entry ──────────────────────────────────────────────
SM_PUBLIC_BASE_URL = str(
    _configured_value(
        "public_base_url",
        "SM_PUBLIC_BASE_URL",
        "https://scjrdomain.com:4430",
    )
).strip().rstrip("/")
SM_ALLOWED_HOSTS = _env_csv(
    "SM_ALLOWED_HOSTS",
    "scjrdomain.com,127.0.0.1,localhost,testserver",
)
SM_CORS_ORIGINS = _env_csv(
    "SM_CORS_ORIGINS",
    "https://scjrdomain.com:4430,http://127.0.0.1:18800,http://localhost:18800",
)
SM_COOKIE_SECURE = _configured_bool("cookie_secure", "SM_COOKIE_SECURE", False)
SM_COOKIE_SAMESITE = str(
    _configured_value("cookie_samesite", "SM_COOKIE_SAMESITE", "lax")
).strip().lower() or "lax"
# Production client authentication lifetime. Keep overridable for controlled tests.
CLIENT_TOKEN_TTL_SECONDS = max(
    60,
    int(_configured_value("client_token_ttl_seconds", "CLIENT_TOKEN_TTL_SECONDS", "86400")),
)

# Software center artifacts are stored outside the packaged application when
# configured for production. The data-directory fallback is kept for local use.
SM_SOFTWARE_STORAGE_DIR = Path(
    _configured_path("software_directory", "SM_SOFTWARE_STORAGE_DIR", str(DATA_DIR / "software"))
).expanduser()
SM_SOFTWARE_MAX_UPLOAD_BYTES = max(
    1 * 1024 * 1024,
    int(_configured_value("software_max_upload_bytes", "SM_SOFTWARE_MAX_UPLOAD_BYTES", str(1024 * 1024 * 1024))),
)
SM_SOFTWARE_RETENTION_COUNT = max(
    1,
    int(_configured_value("software_retention_count", "SM_SOFTWARE_RETENTION_COUNT", "3")),
)
SM_SOFTWARE_SESSION_MAX_AGE = max(
    300,
    int(_configured_value("software_session_max_age", "SM_SOFTWARE_SESSION_MAX_AGE", "7200")),
)
SM_SOFTWARE_ALLOWED_EXTENSIONS = frozenset({".exe", ".zip", ".msi"})

# Super-admin finance overview. Collection remains a separate read-only path;
# this flag can disable the page and ingest APIs without touching trading.
SM_FINANCE_ENABLED = _configured_bool("finance_enabled", "SM_FINANCE_ENABLED", True)
SM_FINANCE_RETENTION_MONTHS = max(
    1,
    min(3, int(_configured_value("finance_retention_months", "SM_FINANCE_RETENTION_MONTHS", "3"))),
)
SM_FINANCE_CLEANUP_INTERVAL_SECONDS = max(
    3600,
    int(_configured_value("finance_cleanup_interval_seconds", "SM_FINANCE_CLEANUP_INTERVAL_SECONDS", "21600")),
)

# Used only when a fresh database has no super administrator. Existing accounts
# are never reset from these values during startup.
SM_BOOTSTRAP_ADMIN_USERNAME = (
    os.environ.get("SM_BOOTSTRAP_ADMIN_USERNAME", "admin").strip() or "admin"
)
SM_BOOTSTRAP_ADMIN_PASSWORD = os.environ.get(
    "SM_BOOTSTRAP_ADMIN_PASSWORD",
    "admin123" if SM_ENVIRONMENT == "development" else "",
)

# ── Local Caddy process management ──────────────────────────────────────
SM_CADDY_AUTO_MANAGE = _configured_bool("caddy_auto_manage", "SM_CADDY_AUTO_MANAGE", True)
SM_CADDY_REQUIRED = _configured_bool("caddy_required", "SM_CADDY_REQUIRED", False)
SM_CADDY_EXE = os.environ.get("SM_CADDY_EXE", "").strip()
SM_CADDY_DIR = os.environ.get(
    "SM_CADDY_DIR",
    str(RUNTIME_DIR / "caddy") if _IS_FROZEN else "",
).strip()
SM_CADDY_ADMIN = str(
    _configured_value("caddy_admin", "SM_CADDY_ADMIN", "127.0.0.1:2019")
).strip() or "127.0.0.1:2019"
SM_CADDY_CERT_FILE = _configured_path("certificate_file", "SM_CADDY_CERT_FILE", "")
SM_CADDY_KEY_FILE = _configured_path("key_file", "SM_CADDY_KEY_FILE", "")
SM_CADDY_START_TIMEOUT = max(
    1.0,
    float(_configured_value("caddy_start_timeout", "SM_CADDY_START_TIMEOUT", "10")),
)

# ── TS domain pool and Tencent Cloud DNSPod ─────────────────────────────
SM_DOMAIN_ROOT = "scjrdomain.com"
SM_TS_WS_PATH = os.environ.get("SM_TS_WS_PATH", "/ws").strip() or "/ws"
if not SM_TS_WS_PATH.startswith("/"):
    SM_TS_WS_PATH = f"/{SM_TS_WS_PATH}"

SM_DOMAIN_POOL_REQUIRED = _configured_bool(
    "domain_pool_required", "SM_DOMAIN_POOL_REQUIRED", True
)
_DEFAULT_DOMAIN_COOLDOWN_SECONDS = 1800 if SM_ENVIRONMENT == "production" else 0
SM_DOMAIN_COOLDOWN_SECONDS = max(
    0,
    int(_configured_value("domain_cooldown_seconds", "SM_DOMAIN_COOLDOWN_SECONDS", str(_DEFAULT_DOMAIN_COOLDOWN_SECONDS))),
)

# Production is always real mode. The environment override remains available to
# automated tests so mock DNS never reaches a production deployment by default.
SM_DNSPOD_MODE = str(
    _configured_value("dnspod_mode", "SM_DNSPOD_MODE", "real")
).strip().lower()
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


def _database_has_dns_credentials(database_path: str | Path) -> bool:
    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        return False
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT secret_id, secret_key FROM dns_provider_config WHERE id=1"
            ).fetchone()
            return bool(row and str(row[0] or "").strip() and str(row[1] or "").strip())
        finally:
            connection.close()
    except sqlite3.Error:
        return False


def production_config_errors(database_path: str | Path) -> list[str]:
    """Return fail-closed production configuration errors without secret values."""
    if SM_ENVIRONMENT != "production":
        return []

    errors: list[str] = []
    if DEPLOYMENT_CONFIG_ERROR:
        errors.append(DEPLOYMENT_CONFIG_ERROR)
    if SERVER_HOST != "127.0.0.1":
        errors.append("SERVER_HOST must be 127.0.0.1 in production")
    for name, port in (
        ("SERVER_PORT", SERVER_PORT),
        ("SM_PUBLIC_HTTP_PORT", SM_PUBLIC_HTTP_PORT),
        ("SM_PUBLIC_HTTPS_PORT", SM_PUBLIC_HTTPS_PORT),
    ):
        if not 1 <= port <= 65535:
            errors.append(f"{name} must be between 1 and 65535")
    if SM_PUBLIC_HTTP_PORT == SM_PUBLIC_HTTPS_PORT:
        errors.append("SM_PUBLIC_HTTP_PORT and SM_PUBLIC_HTTPS_PORT must be different")
    if SM_PUBLIC_HTTPS_PORT != 4430:
        errors.append("SM_PUBLIC_HTTPS_PORT must be 4430 in production")
    if SERVER_PORT in {SM_PUBLIC_HTTP_PORT, SM_PUBLIC_HTTPS_PORT}:
        errors.append("SERVER_PORT must not overlap a public Caddy port")
    if SM_PUBLIC_BASE_URL != "https://scjrdomain.com:4430":
        errors.append(
            "SM_PUBLIC_BASE_URL must be https://scjrdomain.com:4430 in production"
        )
    try:
        public_url = urlsplit(SM_PUBLIC_BASE_URL)
        if (public_url.port or 443) != SM_PUBLIC_HTTPS_PORT:
            errors.append("SM_PUBLIC_BASE_URL port must match SM_PUBLIC_HTTPS_PORT in production")
    except ValueError:
        errors.append("SM_PUBLIC_BASE_URL contains an invalid port")
    if not SM_COOKIE_SECURE:
        errors.append("SM_COOKIE_SECURE must be enabled in production")
    if not SM_CADDY_AUTO_MANAGE or not SM_CADDY_REQUIRED:
        errors.append("Caddy auto-management and required mode must be enabled in production")
    if bool(SM_CADDY_CERT_FILE) != bool(SM_CADDY_KEY_FILE):
        errors.append("SM_CADDY_CERT_FILE and SM_CADDY_KEY_FILE must be configured together")
    if SM_CADDY_CERT_FILE and not Path(SM_CADDY_CERT_FILE).is_file():
        errors.append("SM_CADDY_CERT_FILE does not exist")
    if SM_CADDY_KEY_FILE and not Path(SM_CADDY_KEY_FILE).is_file():
        errors.append("SM_CADDY_KEY_FILE does not exist")
    errors.extend(
        validate_data_paths(
            DATA_DIR,
            database_path,
            SM_SOFTWARE_STORAGE_DIR,
        )
    )
    if Path(database_path).expanduser().resolve() != (DATA_DIR / "server_manager.db").resolve():
        errors.append("database must be stored as data/server_manager.db in production")
    if SM_DOMAIN_COOLDOWN_SECONDS < 1800:
        errors.append("SM_DOMAIN_COOLDOWN_SECONDS must be at least 1800 in production")
    if SM_DOMAIN_POOL_REQUIRED:
        if SM_DNSPOD_MODE != "real":
            errors.append("SM_DNSPOD_MODE must be real when the production domain pool is required")
        has_environment_credentials = bool(
            SM_DNSPOD_SECRET_ID and SM_DNSPOD_SECRET_KEY
        )
        if not has_environment_credentials and not _database_has_dns_credentials(database_path):
            errors.append("DNSPod credentials are required for the production domain pool")

    if not Path(database_path).expanduser().is_file():
        if not SM_BOOTSTRAP_ADMIN_PASSWORD or SM_BOOTSTRAP_ADMIN_PASSWORD == "admin123":
            errors.append("a non-default bootstrap administrator password is required for a new database")
    return errors


def deployment_config_snapshot(data_dir: str | Path = DATA_DIR) -> dict:
    """Return the portable, non-secret runtime configuration."""
    root = Path(data_dir).expanduser().resolve()
    certificate_file = ""
    key_file = ""
    if (
        SM_CADDY_CERT_FILE
        and SM_CADDY_KEY_FILE
        and is_within(root, SM_CADDY_CERT_FILE)
        and is_within(root, SM_CADDY_KEY_FILE)
    ):
        certificate_file = relative_path(root, SM_CADDY_CERT_FILE)
        key_file = relative_path(root, SM_CADDY_KEY_FILE)
    return {
        "server_host": SERVER_HOST,
        "server_port": SERVER_PORT,
        "public_http_port": SM_PUBLIC_HTTP_PORT,
        "public_https_port": SM_PUBLIC_HTTPS_PORT,
        "public_base_url": SM_PUBLIC_BASE_URL,
        "caddy_admin": SM_CADDY_ADMIN,
        "caddy_auto_manage": SM_CADDY_AUTO_MANAGE,
        "caddy_required": SM_CADDY_REQUIRED,
        "caddy_start_timeout": SM_CADDY_START_TIMEOUT,
        "cookie_secure": SM_COOKIE_SECURE,
        "cookie_samesite": SM_COOKIE_SAMESITE,
        "client_token_ttl_seconds": CLIENT_TOKEN_TTL_SECONDS,
        "software_directory": "software",
        "software_max_upload_bytes": SM_SOFTWARE_MAX_UPLOAD_BYTES,
        "software_retention_count": SM_SOFTWARE_RETENTION_COUNT,
        "software_session_max_age": SM_SOFTWARE_SESSION_MAX_AGE,
        "finance_enabled": SM_FINANCE_ENABLED,
        "finance_retention_months": SM_FINANCE_RETENTION_MONTHS,
        "finance_cleanup_interval_seconds": SM_FINANCE_CLEANUP_INTERVAL_SECONDS,
        "domain_cooldown_seconds": SM_DOMAIN_COOLDOWN_SECONDS,
        "domain_pool_required": SM_DOMAIN_POOL_REQUIRED,
        "dnspod_mode": SM_DNSPOD_MODE,
        "certificate_file": certificate_file,
        "key_file": key_file,
    }


def persist_data_metadata(
    database_schema_version: int,
    *,
    data_dir: str | Path = DATA_DIR,
    application_version: str = "",
) -> None:
    """Create portable metadata without overwriting an existing deployment file."""
    root = Path(data_dir).expanduser().resolve()
    for directory in (root, root / "software", root / "certificates", root / "logs"):
        directory.mkdir(parents=True, exist_ok=True)
    ensure_deployment_config(root, deployment_config_snapshot(root))
    write_data_manifest(
        root,
        database_schema_version=database_schema_version,
        application_version=application_version,
    )

