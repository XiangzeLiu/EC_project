import os

"""
Constants & Theme Definitions
颜色主题、字体配置、全局常量
"""

# ── Colors ──────────────────────────────────────────────────────────────────────
DARK_BG       = "#0f1012"
PANEL_BG      = "#15171a"
PANEL_ALT_BG  = "#1b1d21"
BORDER        = "#2a2d33"
TOP_BAR_BG    = "#0c0d0f"
INPUT_BG      = "#1a1c20"

ACCENT_BLUE   = "#b7bcc6"
ACCENT_GREEN  = "#1a8a5a"
ACCENT_RED    = "#c94a4a"
ACCENT_YELLOW = "#d0ad63"
GOLD          = "#cfae67"

TEXT_PRIMARY  = "#e6e8ec"
TEXT_DIM      = "#a8adb7"
TEXT_MUTED    = "#7d838f"

FOCUS_RING       = "#8f949e"
TREE_SELECT_BG   = "#2b2f36"
BUTTON_NEUTRAL_BG = "#22252b"
BUTTON_HOVER_BG   = "#2b3038"
BUTTON_ACTIVE_BG  = "#353b45"

# ── Fonts ───────────────────────────────────────────────────────────────────────
FONT_MONO      = ("Courier New", 13)
FONT_MONO_SM   = ("Courier New", 11)
FONT_UI_SM     = ("Segoe UI", 11)
FONT_UI        = ("Segoe UI", 13)
FONT_BOLD      = ("Segoe UI", 11, "bold")
FONT_TICKER    = ("Courier New", 19, "bold")
FONT_TITLE     = ("Courier New", 15, "bold")
FONT_ACTION_BTN = ("Segoe UI", 12, "bold")

# ── Order Status Mapping ────────────────────────────────────────────────────────
STATUS_MAP = {
    "Live": "Live", "Received": "Received", "Routing": "Routing",
    "Filled": "Filled", "Cancelled": "Cancelled", "Rejected": "Rejected",
    "Partial": "Partial", "Cancelling": "Cancelling", "Expired": "Expired",
}

LIVE_STATUSES = {"Received", "Routing", "Live", "Cancelling", "Partial"}

# ── Timezone & Session ──────────────────────────────────────────────────────────
TZ_ET_NAME       = "America/New_York"
SESSION_START_H  = 4
SESSION_END_H    = 20

# ── Polling intervals (ms) ─────────────────────────────────────────────────────
POLL_INTERVAL      = 150    # 主轮询间隔
POSITIONS_INTERVAL = 3000   # 持仓刷新间隔 (3s)
ORDERS_INTERVAL    = 30000  # 订单轮询间隔 (30s)
HEARTBEAT_INTERVAL = 10000  # 心跳检测间隔 (10s)
MOCK_QUOTE_INTERVAL = 500   # 模拟行情推送间隔 (ms)

# ── Server defaults ─────────────────────────────────────────────────────────────
DEFAULT_SM_BASE_URL = os.getenv("CLIENT_SM_BASE_URL", "https://scjrdomain.com").strip()
DEFAULT_SERVER_HOST = os.getenv("CLIENT_SM_HOST", "127.0.0.1")
DEFAULT_SERVER_PORT = int(os.getenv("CLIENT_SM_PORT", "8800"))

# ── Trader_Server (TS) 直连配置 ────────────────────────────────────────────
DEFAULT_TS_WS_URL = os.getenv("CLIENT_TS_WS_URL", "").strip()
DEFAULT_TS_HOST = os.getenv("CLIENT_TS_HOST", "127.0.0.1")
DEFAULT_TS_PORT = int(os.getenv("CLIENT_TS_PORT", "8900"))

# ── TS 自动重连配置 ───────────────────────────────────────────────────────────
TS_RECONNECT_ENABLED = os.getenv("CLIENT_TS_RECONNECT_ENABLED", "1").strip().lower() not in {"0", "false", "no"}  # 是否启用自动重连
TS_RECONNECT_BASE_INTERVAL = int(os.getenv("CLIENT_TS_RECONNECT_BASE_INTERVAL", "3"))  # 首次重连等待时间(秒)
TS_RECONNECT_MAX_INTERVAL = int(os.getenv("CLIENT_TS_RECONNECT_MAX_INTERVAL", "30"))  # 最大重连间隔(秒)，指数退避上限
TS_RECONNECT_MAX_ATTEMPTS = int(os.getenv("CLIENT_TS_RECONNECT_MAX_ATTEMPTS", "10"))  # 最大重连次数(0=无限次)
