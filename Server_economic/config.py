"""
Server_economic Configuration
配置管理、本地状态持久化、运行时全局变量

配置文件:
  - config.json        注册成功后的持久化凭证（server_id, token, manager_url）
  - .register_state.json 注册审核期间的临时状态（request_id, expire_at）
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── 路径常量 ────────────────────────────────────────────────────────────

# Server_economic 包所在目录
_PKG_DIR = Path(__file__).resolve().parent
_DATA_DIR = _PKG_DIR / "data"

CONFIG_FILE = _DATA_DIR / "config.json"
REGISTER_STATE_FILE = _DATA_DIR / ".register_state.json"
LOG_DIR = _DATA_DIR / "logs"

log = logging.getLogger("server_economic")

# ── 默认值 ────────────────────────────────────────────────────────────────

DEFAULT_MANAGER_URL = "http://127.0.0.1:8800"
DEFAULT_NODE_NAME = "economic-node-01"
DEFAULT_REGION = "CN"
DEFAULT_HOST = ""
DEFAULT_CAPABILITIES = ["cpi", "gdp", "interest_rate", "employment", "trade_balance"]
DEFAULT_CONTACT = ""
DEFAULT_DESCRIPTION = ""

# 心跳间隔（秒），与 Server_manager 返回的 next_interval 保持一致
DEFAULT_HEARTBEAT_INTERVAL = 30

# WebSocket 服务端口（供 Client 连接）
DEFAULT_WS_PORT = 8900


def ensure_dirs():
    """确保数据目录和日志目录存在"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


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
        self.region: str = DEFAULT_REGION
        self.status: str = "uninitialized" # uninitialized / registering / approved / running / error
        self.heartbeat_ok: bool = False    # 最近一次心跳是否成功
        self.last_heartbeat_time: float = 0
        self.heartbeat_fail_count: int = 0
        self.ws_clients: list = []         # 已连接的 Client WebSocket 列表
        self._shutdown_flag: bool = False

    @property
    def is_shutting_down(self) -> bool:
        return self._shutdown_flag

    def request_shutdown(self):
        self._shutdown_flag = True
        log.info("Shutdown requested")


# 全局单例
state = RuntimeState()


def init_logging(level: str = "INFO"):
    """初始化日志系统"""
    ensure_dirs()

    log_file = LOG_DIR / f"se_{datetime.now().strftime('%Y%m%d')}.log"
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )
    log.info(f"Logging initialized (level={level}, file={log_file})")
