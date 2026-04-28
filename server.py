"""
Trading Proxy Server
====================
阿里云中间代理服务器
- HTTP REST：登录、下单、撤单、持仓、订单
- WebSocket：行情流中转
"""

import asyncio
import json
import hashlib
import os

# 设置 SSL 证书路径，解决 Windows Server 证书链不完整问题
try:
    import certifi
    os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    os.environ.setdefault("REQUESTS_CA_BUNDLE", certifi.where())
    os.environ.setdefault("HTTPX_CA_BUNDLE", certifi.where())
except ImportError:
    pass

# 全局跳过 SSL 验证（最终兜底方案）
import ssl as _ssl_global
_ssl_global._create_default_https_context = _ssl_global._create_unverified_context
import datetime
import logging
from typing import Optional

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

# ── 配置 ──────────────────────────────────────────────────────────────────────
# 自定义账号密码（客户端连服务器用）
SERVER_USERNAME = os.environ.get("SERVER_USERNAME", "admin")
SERVER_PASSWORD = os.environ.get("SERVER_PASSWORD", "changeme123")
SERVER_TOKEN    = hashlib.sha256(f"{SERVER_USERNAME}:{SERVER_PASSWORD}".encode()).hexdigest()

SERVER_HOST = "0.0.0.0"
SERVER_PORT = int(os.environ.get("SERVER_PORT", "8800"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("proxy")

# 屏蔽 SDK 内部 HTTP 日志（防止暴露 API 地址）
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("tastytrade").setLevel(logging.WARNING)

# ── SDK ───────────────────────────────────────────────────────────────────────
try:
    from tastytrade import Session, DXLinkStreamer
    from tastytrade.account import Account
    from tastytrade.instruments import Equity
    from tastytrade.order import (
        NewOrder, OrderAction, OrderTimeInForce, OrderType, Leg, InstrumentType
    )
    try:
        from tastytrade.dxfeed import Quote as DXQ
    except ImportError:
        DXQ = None
    from decimal import Decimal
    SDK_OK = True
except Exception as e:
    SDK_OK = False
    log.warning(f"SDK not available: {e}")

# ── IB TWS 行情 ───────────────────────────────────────────────────────────────
IB_HOST      = "8.210.169.202"
IB_PORT      = 7496
IB_CLIENT_ID = 19

try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    IB_OK = True
except ImportError:
    IB_OK = False
    log.warning("ibapi not installed, IB quotes unavailable")

# ── 全局状态 ──────────────────────────────────────────────────────────────────
# 直接从环境变量读取 session token，无需登录流程
_TASTY_SECRET = os.environ.get("TASTY_SECRET", "")
_TASTY_TOKEN  = os.environ.get("TASTY_TOKEN", "")

session_store = {
    "session":   None,   # 复用的 Session 对象
    "account":   None,   # 复用的 Account 对象
    "secret":    _TASTY_SECRET,
    "token":     _TASTY_TOKEN,
    "acct_num":  "",
    "connected": False,
}

if _TASTY_SECRET and _TASTY_TOKEN:
    log.info("Credentials loaded, will fetch account on startup")
else:
    log.warning("TASTY_SECRET / TASTY_TOKEN not set!")

# 行情 WebSocket 客户端列表
quote_clients: list[WebSocket] = []
# 当前订阅的标的
subscribed_syms: set = set()
# 行情流任务
stream_task: Optional[asyncio.Task] = None

app = FastAPI(title="Trading Proxy")
security = HTTPBearer()

# ── 鉴权 ──────────────────────────────────────────────────────────────────────
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials.credentials != SERVER_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid token")
    return True

# ── Models ────────────────────────────────────────────────────────────────────
class LoginReq(BaseModel):
    username: str
    password: str

class OrderReq(BaseModel):
    symbol:     str
    qty:        int
    price:      float = 0.0
    action:     str   # "Buy to Open" / "Sell to Close" / "Sell to Open" / "Buy to Close"
    order_type: str   = "limit"  # "limit" / "market"
    tif:        str   = "Day"

class SubscribeReq(BaseModel):
    symbols: list[str]

# ── 辅助：新建 event loop 运行 async ──────────────────────────────────────────
def run_sync(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()

async def get_fresh():
    """复用已有 Session 和 Account，减少 API 调用延迟"""
    if session_store["session"] and session_store["account"]:
        return session_store["session"], session_store["account"]
    # fallback：重新创建
    s = Session(session_store["secret"], session_store["token"])
    accts = await Account.get(s)
    a = next((x for x in accts if str(x.account_number) == session_store["acct_num"]), accts[0])
    session_store["session"] = s
    session_store["account"] = a
    return s, a

# ── 路由 ──────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "connected": session_store["connected"]}

@app.post("/auth/login")
async def login(req: LoginReq):
    """验证本地插件的 admin 账号，返回 server token"""
    if req.username != SERVER_USERNAME or req.password != SERVER_PASSWORD:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not session_store["connected"]:
        raise HTTPException(status_code=503, detail="Server session token not configured")
    log.info(f"Client logged in: {req.username}")
    return {
        "success":  True,
        "token":    SERVER_TOKEN,
        "acct_num": session_store["acct_num"]
    }

@app.post("/auth/logout")
def logout(_=Depends(verify_token)):
    # 只做客户端级别断开，不影响服务器的券商连接
    return {"success": True}

@app.get("/positions", dependencies=[Depends(verify_token)])
async def get_positions():
    if not session_store["connected"]:
        raise HTTPException(status_code=401, detail="Not connected")
    try:
        s, a = await get_fresh()
        rows = await a.get_positions(s)
        result = []
        for p in rows:
            result.append({
                "symbol":            p.symbol,
                "quantity":          float(p.quantity),
                "direction":         getattr(p, "quantity_direction", "Long"),
                "average_open_price": float(p.average_open_price or 0),
                "close_price":       float(p.close_price or 0),
                "realized_today":    float(getattr(p, "realized_today", 0) or 0),
            })
        return {"positions": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/orders/live", dependencies=[Depends(verify_token)])
async def get_live_orders():
    if not session_store["connected"]:
        raise HTTPException(status_code=401, detail="Not connected")
    try:
        s, a = await get_fresh()
        raw = await a.get_live_orders(s)
        return {"orders": [_serialize_order(o) for o in raw]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/orders/history", dependencies=[Depends(verify_token)])
async def get_order_history():
    if not session_store["connected"]:
        raise HTTPException(status_code=401, detail="Not connected")
    try:
        s, a = await get_fresh()
        raw = await a.get_order_history(s)
        return {"orders": [_serialize_order(o) for o in raw]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/orders/place", dependencies=[Depends(verify_token)])
async def place_order(req: OrderReq):
    if not session_store["connected"]:
        raise HTTPException(status_code=401, detail="Not connected")
    try:
        s, a = await get_fresh()
        ACTION_MAP = {
            "Buy to Open":   OrderAction.BUY_TO_OPEN,
            "Sell to Close": OrderAction.SELL_TO_CLOSE,
            "Sell to Open":  OrderAction.SELL_TO_OPEN,
            "Buy to Close":  OrderAction.BUY_TO_CLOSE,
        }
        TIF_MAP = {
            "Day":     OrderTimeInForce.DAY,
            "GTC":     OrderTimeInForce.GTC,
            "IOC":     OrderTimeInForce.IOC,
            "EXT":     OrderTimeInForce.EXT,
            "GTC_EXT": OrderTimeInForce.GTC_EXT,
        }
        act      = ACTION_MAP.get(req.action, OrderAction.BUY_TO_OPEN)
        tif_enum = TIF_MAP.get(req.tif, OrderTimeInForce.DAY)
        is_buy   = "Buy" in req.action
        equity   = await Equity.get(s, req.symbol)
        leg      = equity.build_leg(Decimal(str(req.qty)), act)

        if req.order_type == "market":
            order = NewOrder(time_in_force=tif_enum,
                             order_type=OrderType.MARKET, legs=[leg])
        else:
            signed = Decimal(str(req.price)) * (-1 if is_buy else 1)
            order  = NewOrder(time_in_force=tif_enum,
                              order_type=OrderType.LIMIT, legs=[leg], price=signed)

        resp = await a.place_order(s, order, dry_run=False)
        log.info(f"Order placed: {req.action} {req.qty} {req.symbol} @ {req.price}")
        return {"success": True, "order_id": str(resp.order.id)}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.delete("/orders/{order_id}", dependencies=[Depends(verify_token)])
async def cancel_order(order_id: str):
    if not session_store["connected"]:
        raise HTTPException(status_code=401, detail="Not connected")
    try:
        s, a = await get_fresh()
        await a.delete_order(s, order_id)
        log.info(f"Order cancelled: {order_id}")
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

# ── 行情 WebSocket ─────────────────────────────────────────────────────────────
@app.websocket("/quotes")
async def quote_ws(ws: WebSocket):
    # 验证 token
    token = ws.query_params.get("token")
    if token != SERVER_TOKEN:
        await ws.close(code=4001)
        return

    await ws.accept()
    quote_clients.append(ws)
    log.info(f"Quote client connected, total: {len(quote_clients)}")

    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            log.info(f"WS message received: {data}")
            if data.get("action") == "subscribe":
                syms = data.get("symbols", [])
                for sym in syms:
                    if sym not in subscribed_syms:
                        subscribed_syms.add(sym)
                        log.info(f"Subscribed: {sym}, total: {subscribed_syms}")
            elif data.get("action") == "unsubscribe":
                syms = data.get("symbols", [])
                for sym in syms:
                    subscribed_syms.discard(sym)
                    log.info(f"Unsubscribed: {sym}, total: {subscribed_syms}")
                # 如果行情流已在运行，发送新订阅请求
                # （流会自动处理 subscribed_syms 变化）
    except WebSocketDisconnect:
        quote_clients.remove(ws)
        log.info(f"Quote client disconnected, total: {len(quote_clients)}")
    except Exception as e:
        if ws in quote_clients:
            quote_clients.remove(ws)

async def broadcast_quote(q: dict):
    """推送行情给所有连接的客户端"""
    msg = json.dumps(q)
    dead = []
    if quote_clients:
        log.debug(f"Broadcasting {q.get('symbol')} bid={q.get('bid')} ask={q.get('ask')} to {len(quote_clients)} clients")
    for ws in quote_clients:
        try:
            await ws.send_text(msg)
        except:
            dead.append(ws)
    for ws in dead:
        if ws in quote_clients:
            quote_clients.remove(ws)

# ── IB TWS 行情封装 ───────────────────────────────────────────────────────────
class IBQuoteApp(EWrapper, EClient):
    def __init__(self, loop, queue):
        EWrapper.__init__(self)
        EClient.__init__(self, self)
        self._loop  = loop
        self._queue = queue           # asyncio.Queue，推行情
        self._reqid = 1000
        self._sym_reqid = {}          # sym -> reqId
        self._reqid_sym = {}          # reqId -> sym
        self._quotes = {}             # sym -> {bid, ask, last, volume}

    def nextReqId(self):
        self._reqid += 1
        return self._reqid

    def subscribe(self, sym):
        if sym in self._sym_reqid:
            return
        contract = Contract()
        contract.symbol   = sym
        contract.secType  = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        rid = self.nextReqId()
        self._sym_reqid[sym] = rid
        self._reqid_sym[rid] = sym
        self.reqMktData(rid, contract, "", False, False, [])
        log.info(f"IB subscribed: {sym} reqId={rid}")

    def unsubscribe(self, sym):
        rid = self._sym_reqid.pop(sym, None)
        if rid:
            self.cancelMktData(rid)
            self._reqid_sym.pop(rid, None)
            self._quotes.pop(sym, None)
            log.info(f"IB unsubscribed: {sym}")

    def tickPrice(self, reqId, tickType, price, attrib):
        sym = self._reqid_sym.get(reqId)
        if not sym or price <= 0: return
        q = self._quotes.setdefault(sym, {"bid":0,"ask":0,"last":0,"volume":0})
        # tickType: 1=bid 2=ask 4=last 6=high 7=low
        if   tickType == 1: q["bid"]  = price
        elif tickType == 2: q["ask"]  = price
        elif tickType == 4: q["last"] = price
        else: return
        # 推到 asyncio queue
        asyncio.run_coroutine_threadsafe(
            self._queue.put({"symbol":sym, **q,
                             "ts": datetime.datetime.now().strftime("%H:%M:%S")}),
            self._loop)

    def tickSize(self, reqId, tickType, size):
        sym = self._reqid_sym.get(reqId)
        if not sym: return
        q = self._quotes.setdefault(sym, {"bid":0,"ask":0,"last":0,"volume":0})
        if tickType == 8:  # volume
            q["volume"] = int(size)

    def error(self, reqId, errorCode, errorString, advancedOrderRejectJson=""):
        if errorCode in (2104, 2106, 2158, 2119):
            return  # 正常连接提示，忽略
        log.warning(f"IB error reqId={reqId} code={errorCode}: {errorString}")

    def connectionClosed(self):
        log.warning("IB connection closed")


# 全局 IB app 实例
_ib_app: IBQuoteApp = None

async def ib_preconnect():
    """启动时预连接 IB，保持常驻"""
    global _ib_app
    if not IB_OK:
        return
    import threading as _t
    while True:
        try:
            loop     = asyncio.get_event_loop()
            ib_queue = asyncio.Queue()
            app      = IBQuoteApp(loop, ib_queue)

            def _connect():
                app.connect(IB_HOST, IB_PORT, IB_CLIENT_ID)
                app.run()
            _t.Thread(target=_connect, daemon=True).start()

            await asyncio.sleep(2)
            if app.isConnected():
                _ib_app = app
                log.info(f"IB pre-connected to {IB_HOST}:{IB_PORT}")
                # 保持连接，监控断线
                while app.isConnected():
                    await asyncio.sleep(1)
                _ib_app = None
                log.warning("IB disconnected, reconnecting in 5s...")
            else:
                log.warning("IB pre-connect failed, retrying in 5s...")
        except Exception as e:
            log.warning(f"IB pre-connect error: {e}, retrying in 5s...")
        await asyncio.sleep(5)

async def quote_stream_loop():
    """复用 _ib_app 推送行情给客户端"""
    already_subbed = set()
    while True:
        if _ib_app is None or not _ib_app.isConnected():
            await asyncio.sleep(0.5)
            continue
        try:
            # 同步订阅
            new     = subscribed_syms - already_subbed
            removed = already_subbed  - subscribed_syms
            for sym in new:
                _ib_app.subscribe(sym)
                already_subbed.add(sym)
            for sym in removed:
                _ib_app.unsubscribe(sym)
                already_subbed.discard(sym)

            # 推送队列里的行情
            try:
                q = await asyncio.wait_for(_ib_app._queue.get(), timeout=0.3)
                if q.get("bid", 0) == 0 and q.get("ask", 0) == 0:
                    continue
                if q["last"] == 0 and q["bid"] and q["ask"]:
                    q["last"] = round((q["bid"] + q["ask"]) / 2, 2)
                await broadcast_quote(q)
            except asyncio.TimeoutError:
                pass
        except Exception as e:
            log.warning(f"quote_stream_loop error: {e}")
            await asyncio.sleep(0.5)

@app.on_event("startup")
async def startup():
    # 启动时自动用 secret+token 获取账户号
    if session_store["secret"] and session_store["token"]:
        try:
            sess  = Session(session_store["secret"], session_store["token"])
            accts = await Account.get(sess)
            if accts:
                session_store["session"]   = sess
                session_store["account"]   = accts[0]   # 缓存 account 对象
                session_store["acct_num"]  = str(accts[0].account_number)
                session_store["connected"] = True
                log.info(f"Auto-connected, account: {session_store['acct_num']}")
            else:
                log.warning("No accounts found")
        except Exception as e:
            log.error(f"Auto-connect failed: {e}")
    asyncio.create_task(quote_stream_loop())
    asyncio.create_task(ib_preconnect())
    log.info(f"Server started on {SERVER_HOST}:{SERVER_PORT}")

# ── 辅助序列化 ────────────────────────────────────────────────────────────────
def _serialize_order(o):
    try:
        leg = o.legs[0] if o.legs else None
        # 序列化 fills
        legs_data = []
        for l in (o.legs or []):
            fills_data = []
            for f in (getattr(l, "fills", []) or []):
                fills_data.append({
                    "fill_price": str(getattr(f, "fill_price", 0) or 0),
                    "quantity":   str(getattr(f, "quantity", 0) or 0),
                    "filled_at":  str(getattr(f, "filled_at", "") or ""),
                })
            legs_data.append({
                "symbol":   str(l.symbol),
                "action":   str(l.action),
                "quantity": str(l.quantity),
                "fills":    fills_data,
            })
        return {
            "id":         str(o.id),
            "symbol":     leg.symbol if leg else "—",
            "action":     str(leg.action) if leg else "—",
            "qty":        str(leg.quantity) if leg else "—",
            "price":      f"{abs(float(o.price)):.2f}" if o.price else "MKT",
            "type":       str(o.order_type).split(".")[-1] if o.order_type else "—",
            "tif":        str(o.time_in_force).split(".")[-1] if hasattr(o, "time_in_force") else "—",
            "status":     str(o.status).split(".")[-1] if o.status else "—",
            "updated_at": str(getattr(o, "updated_at", "") or ""),
            "legs":       legs_data,
        }
    except:
        return {}

if __name__ == "__main__":
    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT)
