"""
Trader_Server Configuration
配置管理、本地状态持久化、运行时全局变量

配置文件:
  - config.json        注册成功后的持久化凭证（server_id, token, manager_url）
  - .register_state.json 注册审核期间的临时状态（request_id, expire_at）
"""

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 路径常量 ────────────────────────────────────────────────────────────

# Trader_Server 包所在目录
_PKG_DIR = Path(__file__).resolve().parent
_DATA_DIR = _PKG_DIR / "data"

CONFIG_FILE = _DATA_DIR / "config.json"
REGISTER_STATE_FILE = _DATA_DIR / ".register_state.json"
LOG_DIR = _DATA_DIR / "logs"
ERROR_LOG_FILE = LOG_DIR / "ts_error.log"
LOG_MAX_BYTES = int(os.getenv("TS_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("TS_LOG_BACKUP_COUNT", "5"))

log = logging.getLogger("trader_server")

# ── 默认值 ────────────────────────────────────────────────────────────────

DEFAULT_MANAGER_URL = os.getenv("TS_MANAGER_URL", "https://scjrdomain.com")
DEFAULT_NODE_NAME = os.getenv("TS_NODE_NAME", "trader-node-01")
DEFAULT_REGION = os.getenv("TS_BROKER_TYPE", "TT")  # 已从地理区域改为券商类型，值与 SM BROKER_TYPES 一致
DEFAULT_PUBLIC_ENDPOINT = (
    os.getenv("TS_PUBLIC_ENDPOINT", "")
    or os.getenv("TS_PUBLIC_URL", "")
    or os.getenv("TS_PUBLIC_HOST", "")
).strip()
DEFAULT_HOST = DEFAULT_PUBLIC_ENDPOINT
DEFAULT_CAPABILITIES = ["cpi", "gdp", "interest_rate", "employment", "trade_balance"]
DEFAULT_CONTACT = ""
DEFAULT_DESCRIPTION = ""

# 心跳间隔（秒），与 Server_manager 返回的 next_interval 保持一致
DEFAULT_HEARTBEAT_INTERVAL = int(os.getenv("TS_HEARTBEAT_INTERVAL", "20"))

# WebSocket 服务端口（供 Client 连接）
DEFAULT_BIND_HOST = os.getenv("TS_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"
DEFAULT_WS_PORT = int(os.getenv("TS_WS_PORT", "8900"))
TS_CADDY_AUTO_MANAGE = os.getenv("TS_CADDY_AUTO_MANAGE", "1").strip().lower() not in {"0", "false", "no", "off"}
TS_CADDY_REQUIRED = os.getenv("TS_CADDY_REQUIRED", "0").strip().lower() in {"1", "true", "yes", "on"}
TS_CADDY_EXE = os.getenv("TS_CADDY_EXE", "").strip()
TS_CADDY_DIR = os.getenv("TS_CADDY_DIR", "").strip()
TS_CADDY_ADMIN = os.getenv("TS_CADDY_ADMIN", "127.0.0.1:2020").strip() or "127.0.0.1:2020"
TS_CADDY_START_TIMEOUT = max(
    1.0,
    float(os.getenv("TS_CADDY_START_TIMEOUT", "10")),
)
DEFAULT_TS_LOGIN_USERNAME = os.getenv("TS_LOGIN_USERNAME", "")
DEFAULT_TS_LOGIN_PASSWORD = os.getenv("TS_LOGIN_PASSWORD", "")
DEFAULT_TS_LOGIN_TEST_USERNAME = "test"
DEFAULT_TS_LOGIN_TEST_PASSWORD = "test"
ALLOW_TS_LOGIN_TEST_BACKDOOR = os.getenv("TS_LOGIN_ALLOW_TEST_BACKDOOR", "1").strip().lower() not in {"0", "false", "no"}


def ensure_dirs():
    """纭繚鏁版嵁鐩綍鍜屾棩蹇楃洰褰曞瓨鍦?"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def verify_trade_service_login(username: str, password: str) -> bool:
    user = (username or "").strip()
    pwd = password or ""
    if not user or not pwd:
        return False
    if user == DEFAULT_TS_LOGIN_USERNAME and pwd == DEFAULT_TS_LOGIN_PASSWORD:
        return True
    if ALLOW_TS_LOGIN_TEST_BACKDOOR and user == DEFAULT_TS_LOGIN_TEST_USERNAME and pwd == DEFAULT_TS_LOGIN_TEST_PASSWORD:
        return True
    return False

# ── config.json 操作 ─────────────────────────────────────────────────────

def load_config() -> dict:
    """
    加载已保存的注册凭证（config.json）

    Returns:
        配置字典，或空字典（未注册时）
    """
    if not CONFIG_FILE.exists():
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.info(f"Config loaded from {CONFIG_FILE}")
        return data
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        return {}


def save_config(data: dict) -> bool:
    """
    保存注册凭证到 config.json

    Args:
        data: 包含 server_id, token, manager_url 等字段

    Returns:
        是否成功
    """
    ensure_dirs()
    try:
        data["saved_at"] = datetime.now(timezone.utc).isoformat()
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log.info(f"Config saved to {CONFIG_FILE}")
        return True
    except Exception as e:
        log.error(f"Failed to save config: {e}")
        return False


def is_registered() -> bool:
    """检查是否已完成注册（config.json 存在且包含有效 token）"""
    cfg = load_config()
    return bool(cfg.get("token") and cfg.get("server_id"))


# ── .register_state.json 操作（注册审核期间）──────────────────────────────

def save_register_state(request_id: str, manager_url: str,
                        node_name: str, expire_at: str) -> bool:
    """
    保存注册审核期间的临时状态
    用于重启后恢复 SSE 等待连接

    Returns:
        是否成功
    """
    ensure_dirs()
    state = {
        "request_id": request_id,
        "manager_url": manager_url,
        "node_name": node_name,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "expire_at": expire_at,
    }
    try:
        with open(REGISTER_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        log.info(f"Register state saved: {request_id}")
        return True
    except Exception as e:
        log.error(f"Failed to save register state: {e}")
        return False


def load_register_state() -> dict | None:
    """
    加载注册审核临时状态

    Returns:
        状态字典，文件不存在/过期时返回 None
    """
    if not REGISTER_STATE_FILE.exists():
        return None
    try:
        with open(REGISTER_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        # 检查是否已过期
        expire_at = state.get("expire_at", "")
        if expire_at and datetime.fromisoformat(expire_at) < datetime.now(timezone.utc):
            log.info("Register state expired, cleaning up")
            clear_register_state()
            return None

        return state
    except Exception as e:
        log.error(f"Failed to load register state: {e}")
        return None


def clear_register_state():
    """清除注册临时状态（成功或失败后调用）"""
    if REGISTER_STATE_FILE.exists():
        try:
            REGISTER_STATE_FILE.unlink()
            log.info("Register state cleared")
        except OSError:
            pass


# ── 运行时状态 ──────────────────────────────────────────────────────────

class RuntimeState:
    """
    全局运行时状态单例
    在各模块间共享节点身份信息与运行标志
    """

    def __init__(self):
        self.server_id: str = ""           # SM 分配的唯一 ID
        self.token: str = ""               # Bearer Token
        self.manager_url: str = DEFAULT_MANAGER_URL
        self.node_name: str = DEFAULT_NODE_NAME
        self.region: str = DEFAULT_REGION  # 存储券商类型值 (tastytrade / interactive_brokers)
        self.public_ip: str = ""
        self.assigned_domain: str = ""
        self.public_endpoint: str = ""
        self.status: str = "uninitialized" # uninitialized / registering / approved / running / error
        self.heartbeat_ok: bool = False    # 最近一次心跳是否成功
        self.last_heartbeat_time: float = 0
        self.heartbeat_fail_count: int = 0
        self.ws_clients: list = []         # 已连接的 Client WebSocket 列表
        self._shutdown_flag: bool = False

        # ── 券商相关状态 ──────────────────────────────
        self.broker_connected: bool = False      # 券商是否已连接
        self.broker_type: str = ""               # 当前券商类型
        self.broker_config_version: int = 0      # SM 端配置版本号

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_flag

    def request_shutdown(self):
        self._shutdown_flag = True
        log.info("Shutdown requested")


# 全局单例
state = RuntimeState()


def init_logging(level: str = "INFO"):
    """Initialize TS runtime and error log files."""
    ensure_dirs()

    log_file = LOG_DIR / f"ts_{datetime.now().strftime('%Y%m%d')}.log"
    log_level = getattr(logging, level.upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    if getattr(root, "_ts_logging_ready", False):
        return

    root.setLevel(log_level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(log_level)
    console.setFormatter(formatter)

    runtime_file = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    runtime_file.setLevel(log_level)
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
    root._ts_logging_ready = True
    log.info(f"Logging initialized (level={level}, file={log_file})")


def read_recent_error_lines(limit: int = 200) -> list[str]:
    """Read recent TS error log lines for local troubleshooting."""
    safe_limit = max(1, min(int(limit or 200), 1000))
    if not ERROR_LOG_FILE.exists():
        return []
    try:
        with open(ERROR_LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return f.readlines()[-safe_limit:]
    except OSError:
        return []
