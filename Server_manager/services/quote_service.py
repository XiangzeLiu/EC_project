"""
Quote Service - IB TWS 行情源 + WebSocket 广播
封装 IB API 连接管理、行情订阅、广播推送
"""

import asyncio
import datetime
import json
import logging
import threading

from config import (
    IB_HOST, IB_PORT, IB_CLIENT_ID,
    quote_clients, subscribed_syms, log,
)

# IB API 可用性检查
IB_OK = False
try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    IB_OK = True
except ImportError:
    # 提供空基类，使 IBQuoteApp 类定义在无 ibapi 时不会报错
    class EWrapper: pass
    class EClient:
        def __init__(self): pass
    Contract = None
    log.warning("ibapi not installed, IB quotes unavailable")

# 全局 IB 应用实例
_ib_app = None


def get_ib_app():
    """获取当前 IB 连接实例"""
    global _ib_app
    return _ib_app


class IBQuoteApp(EWrapper, EClient):
    """
    IB TWS 行情接收器
    继承 EWrapper(回调) + EClient(请求)
    通过 asyncio.Queue 推送行情数据给主事件循环
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        EWrapper.__init__(self)
        EClient.__init__(self, self)

        self._loop = loop
        self._queue = queue          # asyncio.Queue 推送行情用
        self._req_id = 1000
        self._sym_req_id: dict[str, int] = {}   # symbol -> reqId
        self._req_id_sym: dict[int, str] = {}    # reqId -> symbol
        self._quotes: dict[str, dict] = {}       # symbol -> {bid, ask, last, volume}

    def nextValidId(self, orderId: int):
        """更新 reqId 基数"""
        self._req_id = orderId

    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id

    def subscribe(self, symbol: str):
        """订阅标的行情"""
        if symbol in self._sym_req_id:
            return
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"

        rid = self._next_req_id()
        self._sym_req_id[symbol] = rid
        self._req_id_sym[rid] = symbol
        self.reqMktData(rid, contract, "", False, False, [])
        log.info(f"IB subscribed: {symbol} (reqId={rid})")

    def unsubscribe(self, symbol: str):
        """取消订阅标的行情"""
        rid = self._sym_req_id.pop(symbol, None)
        if rid:
            self.cancelMktData(rid)
            self._req_id_sym.pop(rid, None)
            self._quotes.pop(symbol, None)
            log.info(f"IB unsubscribed: {symbol}")

    # ── IB 回调 ────────────────────────────────────────────────────────

    def tickPrice(self, reqId: int, tickType: int, price: float, attrib):
        sym = self._req_id_sym.get(reqId)
        if not sym or price <= 0:
            return
        q = self._quotes.setdefault(sym, {"bid": 0, "ask": 0, "last": 0, "volume": 0})
        # tickType: 1=bid  2=ask  4=last  6=high  7=low
        if   tickType == 1:
            q["bid"] = price
        elif tickType == 2:
            q["ask"] = price
        elif tickType == 4:
            q["last"] = price
        else:
            return
        # 推送到 asyncio queue
        asyncio.run_coroutine_threadsafe(
            self._queue.put({
                "symbol": sym,
                **q,
                "ts": datetime.datetime.now().strftime("%H:%M:%S"),
            }),
            self._loop,
        )

    def tickSize(self, reqId: int, tickType: int, size: int):
        sym = self._req_id_sym.get(reqId)
        if not sym:
            return
        q = self._quotes.setdefault(sym, {"bid": 0, "ask": 0, "last": 0, "volume": 0})
        if tickType == 8:  # volume
            q["volume"] = int(size)

    def error(self, reqId: int, errorCode: int, errorString: str,
              advancedOrderRejectJson: str = ""):
        # 忽略正常连接提示码
        if errorCode in (2104, 2106, 2158, 2119):
            return
        log.warning(f"IB error reqId={reqId} code={errorCode}: {errorString}")

    def connectionClosed(self):
        log.warning("IB connection closed")


# ── 公开函数 ──────────────────────────────────────────────────────────────

async def broadcast_quote(q: dict):
    """
    推送行情数据给所有已连接的 WebSocket 客户端

    Args:
        q: 行情字典 {symbol, bid, ask, last, volume}
    """
    msg = json.dumps(q)
    dead = []
    for ws in list(quote_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in quote_clients:
            quote_clients.remove(ws)


async def ib_preconnect():
    """
    启动时预连接 IB TWS，保持常驻
    断线后自动重连
    """
    global _ib_app
    if not IB_OK:
        log.warning("IB API not available, skipping preconnect")
        return

    while True:
        try:
            loop = asyncio.get_event_loop()
            ib_queue: asyncio.Queue = asyncio.Queue()

            app = IBQuoteApp(loop, ib_queue)
            _ib_app = app

            def _connect_and_run():
                app.connect(IB_HOST, IB_PORT, IB_CLIENT_ID)
                app.run()

            t = threading.Thread(target=_connect_and_run, daemon=True)
            t.start()

            await asyncio.sleep(2)  # 等待连接建立

            if app.isConnected():
                log.info(f"IB pre-connected to {IB_HOST}:{IB_PORT}")
                # 保持连接监控
                while app.isConnected():
                    await asyncio.sleep(1)
                _ib_app = None
                log.warning("IB disconnected, reconnecting in 5s...")
            else:
                _ib_app = None
                log.warning("IB pre-connect failed, retrying in 5s...")

        except Exception as e:
            log.error(f"IB preconnect error: {e}")
            _ib_app = None

        await asyncio.sleep(5)


async def quote_stream_loop():
    """
    主行情循环：
    同步客户端订阅列表到 IB 订阅
    从 IB queue 取出行情并广播给所有 WS 客户端
    """
    already_subscribed: set[str] = set()

    while True:
        app = get_ib_app()
        if app is None or not app.isConnected():
            await asyncio.sleep(0.5)
            continue

        try:
            # 同步订阅变化
            new_symbols = subscribed_syms - already_subscribed
            removed_symbols = already_subscribed - subscribed_syms

            for sym in new_symbols:
                app.subscribe(sym)
                already_subscribed.add(sym)
            for sym in removed_symbols:
                app.unsubscribe(sym)
                already_subscribed.discard(sym)

            # 从队列取出行情并广播
            try:
                q = await asyncio.wait_for(app._queue.get(), timeout=0.3)
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
