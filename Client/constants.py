"""
Constants & Theme Definitions
颜色主题、字体配置、全局常量
"""

# ── Colors ──────────────────────────────────────────────────────────────────────
DARK_BG      = "#0d0f14"
PANEL_BG     = "#13161e"
BORDER       = "#1e2330"
TOP_BAR_BG   = "#080a0e"
INPUT_BG     = "#1c2030"
ACCENT_BLUE  = "#4f9eff"
ACCENT_GREEN = "#00d68f"
ACCENT_RED   = "#ff4d6a"
ACCENT_YELLOW = "#e6b422"
GOLD         = "#f5c418"
TEXT_PRIMARY = "#e8ecf4"
TEXT_DIM     = "#6b7590"
TEXT_MUTED   = "#3a3f52"

# ── Fonts ───────────────────────────────────────────────────────────────────────
FONT_MONO    = ("Courier New", 13)
FONT_MONO_SM = ("Courier New", 11)
FONT_UI_SM   = ("Segoe UI", 11)
FONT_UI      = ("Segoe UI", 13)
FONT_BOLD    = ("Segoe UI", 11, "bold")
FONT_TICKER  = ("Courier New", 19, "bold")
FONT_TITLE   = ("Courier New", 15, "bold")

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
POLL_INTERVAL    = 150    # 主轮询间隔
POSITIONS_INTERVAL = 3000  # 持仓刷新间隔 (3s)
ORDERS_INTERVAL  = 30000  # 订单轮询间隔 (30s)
HEARTBEAT_INTERVAL = 10000  # 心跳检测间隔 (10s)
MOCK_QUOTE_INTERVAL = 500  # 模拟行情推送间隔 (ms)

# ── Server defaults ─────────────────────────────────────────────────────────────
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8800

# ── Server_economic (SE) 直连配置 ────────────────────────────────────────────
DEFAULT_SE_HOST = "127.0.0.1"
DEFAULT_SE_PORT = 8900

# ── SE 自动重连配置 ───────────────────────────────────────────────────────────
SE_RECONNECT_ENABLED = True           # 是否启用自动重连
SE_RECONNECT_BASE_INTERVAL = 3       # 首次重连等待时间(秒)
SE_RECONNECT_MAX_INTERVAL = 30       # 最大重连间隔(秒)，指数退避上限
SE_RECONNECT_MAX_ATTEMPTS = 0        # 最大重连次数(0=无限次)
