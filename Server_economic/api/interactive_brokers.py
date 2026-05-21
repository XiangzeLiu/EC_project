"""
Interactive Brokers (IB TWS) 券商适配器

从 origin_demo/server.py 移植核心逻辑:
  - IBQuoteApp (EWrapper+EClient 双继承)
  - Contract 构建 (STK/SMART/USD)
  - reqMktData / cancelMktData
  - tickPrice/tickSize 回调映射
  - 断线自动重连
"""

import asyncio
import logging
import threading
from datetime import datetime
from typing import Any, Callable

from .base import BaseBrokerAPI

log = logging.getLogger("server_economic.api.ib")

# SDK 导入标记
_IB_AVAILABLE = False
_IBQuoteApp = None  # type: ignore
try:
    from ibapi.client import EClient
    from ibapi.wrapper import EWrapper
    from ibapi.contract import Contract
    _IB_AVAILABLE = True

    class _IBQuoteApp(EWrapper, EClient):
        """
        IB TWS 行情接收器

        与 origin_demo/server.py 第328-393行 IBQuoteApp 完全一致的逻辑:
          - EWrapper + EClient 双继承
          - symbol ↔ reqId 双向映射
          - tickPrice/tickSize → 推入 asyncio.Queue
        """

        def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
            EWrapper.__init__(self)
            EClient.__init__(self, self)
            self._loop = loop
            self._queue: asyncio.Queue = queue
            self._req_id: int = 1000
            self._sym_reqid: dict[str, int] = {}   # symbol -> reqId
            self._reqid_sym: dict[int, str] = {}     # reqId -> symbol
            self._quotes: dict[str, dict] = {}       # symbol -> {bid, ask, last, volume}

        def nextReqId(self) -> int:
            self._req_id += 1
            return self._req_id

        def subscribe(self, sym: str) -> None:
            """订阅标的行情"""
            if sym in self._sym_reqid:
                return
            contract = Contract()
            contract.symbol = sym
            contract.secType = "STK"
            contract.exchange = "SMART"
            contract.currency = "USD"
            rid = self.nextReqId()
            self._sym_reqid[sym] = rid
            self._reqid_sym[rid] = sym
            self.reqMktData(rid, contract, "", False, False, [])
            log.info(f"IB subscribed: {sym} reqId={rid}")

        def unsubscribe(self, sym: str) -> None:
            """取消订阅"""
            rid = self._sym_reqid.pop(sym, None)
            if rid:
                self.cancelMktData(rid)
                self._reqid_sym.pop(rid, None)
                self._quotes.pop(sym, None)
                log.info(f"IB unsubscribed: {sym}")

        # ── EWrapper 回调 ──────────────────────────────────────

        def tickPrice(
            self, reqId: int, tickType: int, price: float, attrib: Any | None = None,
        ) -> None:
            sym = self._reqid_sym.get(reqId)
            if not sym or price <= 0:
                return
            q = self._quotes.setdefault(sym, {"bid": 0, "ask": 0, "last": 0, "volume": 0})
            # tickType: 1=bid, 2=ask, 4=last
            if tickType == 1:
                q["bid"] = price
            elif tickType == 2:
                q["ask"] = price
            elif tickType == 4:
                q["last"] = price
            else:
                return
            # 推送到主事件循环的 queue
            asyncio.run_coroutine_threadsafe(
                self._queue.put({
                    **q, "symbol": sym,
                    "ts": datetime.now().strftime("%H:%M:%S"),
                }),
                self._loop,
            )

        def tickSize(self, reqId: int, tickType: int, size: float) -> None:
            sym = self._reqid_sym.get(reqId)
            if not sym:
                return
            q = self._quotes.setdefault(sym, {"bid": 0, "ask": 0, "last": 0, "volume": 0})
            if tickType == 8:  # volume
                q["volume"] = int(size)

        def error(
            self, reqId: int, errorCode: int, errorString: str,
            advancedOrderRejectJson: str = "",
        ) -> None:
            # 忽略正常连接提示
            if errorCode in (2104, 2106, 2158, 2119):
                return
            log.warning(f"IB error reqId={reqId} code={errorCode}: {errorString}")

        def connectionClosed(self) -> None:
            log.warning("IB connection closed")

        # ── 连接状态查询 ──────────────────────────────────────

        def is_ib_connected(self) -> bool:
            return self.isConnected()

except ImportError:
    log.warning("ibapi not installed, IBBroker will be non-functional")


class IBBroker(BaseBrokerAPI):
    """
    Interactive Brokers 券商 API 适配器
    
    凭证格式 (credentials dict):
        host (str):      必填 - IB TWS IP 地址，如 "8.210.169.202"
        port (int):      必填 - 端口，通常 7496 (实盘) 或 7497 (模拟盘)
        client_id (int): 可选 - 客户端 ID，默认 19
    
    注意：
      - 本适配器主要提供行情数据能力（通过 IB TWS）
      - 交易操作目前不支持通过 IB API（使用 Tastytrade 进行交易）
      - place_order/cancel_order/get_positions 返回空结果或抛出异常
    """

    def __init__(self):
        super().__init__(broker_type="interactive_brokers")
        
        # IB 内部状态
        self._ib_app: _IBQuoteApp | None = None
        self._ib_thread: threading.Thread | None = None
        self._quote_queue: asyncio.Queue = asyncio.Queue()
        self._host: str = ""
        self._port: int = 7496
        self._client_id: int = 19
        self._reconnect_task: asyncio.Task | None = None

    async def connect(self, credentials: dict) -> bool:
        """连接到 IB TWS 并启动行情接收"""
        if not _IB_AVAILABLE:
            log.error("ibapi SDK not installed")
            return False

        self._credentials = credentials
        self._host = credentials.get("host", "127.0.0.1")
        self._port = int(credentials.get("port", 7496))
        self._client_id = int(credentials.get("client_id", 19))

        try:
            loop = asyncio.get_event_loop()
            self._quote_queue = asyncio.Queue()
            app = _IBQuoteApp(loop, self._quote_queue)

            def _connect():
                app.connect(self._host, self._port, self._client_id)
                app.run()

            self._ib_thread = threading.Thread(target=_connect, daemon=True)
            self._ib_thread.start()

            # 等待连接建立
            await asyncio.sleep(2)
            if app.is_ib_connected():
                self._ib_app = app
                self._connected = True
                # 启动行情数据转发任务
                self._start_quote_forwarder()
                log.info(f"IBBroker connected to {self._host}:{self._port}")
                return True
            else:
                log.error(f"IB pre-connect failed to {self._host}:{self._port}")
                return False

        except Exception as e:
            log.error(f"IBBroker connect failed: {e}")
            return False

    async def disconnect(self) -> None:
        """断开 IB 连接"""
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reconnect_task = None

        if self._ib_app:
            try:
                self._ib_app.disconnect()
            except Exception:
                pass
            self._ib_app = None
        
        self._connected = False
        log.info("IBBroker disconnected")

    async def is_connected(self) -> bool:
        return self._connected and self._ib_app is not None and self._ib_app.is_ib_connected()

    async def reconnect(self) -> bool:
        """重新连接 IB TWS"""
        await self.disconnect()
        # 稍等释放端口
        await asyncio.sleep(2)
        return await self.connect(self._credentials)

    # ── 行情操作 ──────────────────────────────────────────────

    async def subscribe_quotes(self, symbols: list[str]) -> None:
        """订阅标的行情"""
        if self._ib_app:
            for sym in symbols:
                if isinstance(sym, str) and sym.strip():
                    self._ib_app.subscribe(sym.strip())

    async def unsubscribe_quotes(self, symbols: list[str]) -> None:
        """取消订阅"""
        if self._ib_app:
            for sym in symbols:
                self._ib_app.unsubscribe(sym)

    # ── 交易操作 (IB 暂不支持) ───────────────────────────────

    async def place_order(self, order_params: dict) -> dict:
        raise NotImplementedError(
            "IBBroker does not support trading operations via this adapter. "
            "Use TastytradeBroker for order placement."
        )

    async def cancel_order(self, order_id: str) -> dict:
        raise NotImplementedError("IBBroker does not support cancel operations.")

    async def get_positions(self, filters: dict | None = None) -> list[dict]:
        raise NotImplementedError("IBBroker does not support position queries.")

    # ── 内部辅助 ───────────────────────────────────────────────

    def _start_quote_forwarder(self) -> None:
        """
        启动后台任务：从 IB quote_queue 取数据并通过回调推送
        
        这替代了 origin_demo 中 server.py 的 quote_stream_loop() 函数。
        """
        async def _forwarder():
            while True:
                try:
                    if not self._quote_queue or self._connected is False:
                        await asyncio.sleep(0.5)
                        continue
                    
                    q = await asyncio.wait_for(self._quote_queue.get(), timeout=1.0)
                    if q.get("bid", 0) == 0 and q.get("ask", 0) == 0:
                        continue
                    if q["last"] == 0 and q["bid"] and q["ask"]:
                        q["last"] = round((q["bid"] + q["ask"]) / 2, 2)

                    self._on_quote_data(q)

                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.debug(f"Quote forwarder error (non-critical): {e}")
                    await asyncio.sleep(0.5)

        self._reconnect_task = asyncio.create_task(_forwarder())
