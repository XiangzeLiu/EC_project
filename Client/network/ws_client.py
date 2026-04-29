"""
WebSocket Client
连接服务器WebSocket端点接收实时行情推送
支持 subscribe/unsubscribe 动态订阅管理
"""

import asyncio
import datetime
import json
import threading
from typing import Callable

import websockets

from .http_client import HttpClient


class QuoteStream:
    """行情WebSocket流处理器"""

    def __init__(self, http_client: HttpClient,
                 on_quote_callback: Callable[[dict], None],
                 on_status_callback: Callable[[str], None] = None):
        self.http = http_client
        self.on_quote = on_quote_callback      # 回调: 接收行情数据
        self.on_status = on_status_callback    # 回调: 状态变化通知
        self._active = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribed: set[str] = set()
        self._pending_subscribe: set[str] = set()
        self._pending_unsubscribe: set[str] = set()

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def subscribed_symbols(self) -> set[str]:
        return self._subscribed.copy()

    def start(self):
        """启动WebSocket连接线程"""
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止WebSocket连接"""
        self._active = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._stop_loop_tasks)

    def request_subscribe(self, symbols: list[str]):
        """请求订阅标的行情"""
        for sym in symbols:
            if isinstance(sym, str) and sym.strip():
                self._pending_subscribe.add(sym.strip())

    def request_unsubscribe(self, symbols: list[str]):
        """请求取消订阅标的行情"""
        for sym in symbols:
            self._pending_subscribe.discard(sym)
            self._pending_unsubscribe.add(sym.strip())

    def _run_loop(self):
        """在独立线程中运行asyncio事件循环"""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._stream())
        except Exception as e:
            if self._active and self.on_status:
                self.on_status(f"Stream error: {e}")
        finally:
            self._active = False
            try:
                self._loop.close()
            except Exception:
                pass

    async def _stream(self):
        """WebSocket主循环"""
        host = self.http.base_url.replace("http://", "").replace("https://", "")
        uri = f"ws://{host}/quotes?token={self.http.token}"

        async with websockets.connect(uri) as ws:
            if self.on_status:
                self.on_status("connected")

            await asyncio.gather(
                self._subscription_watcher(ws),
                self._receive_loop(ws),
            )

    async def _subscription_watcher(self, ws):
        """监控订阅/取消订阅请求并同步到服务器"""
        while self._active:
            active_syms = self._subscribed | self._pending_subscribe
            unsub = self._pending_unsubscribe - active_syms
            sub = self._pending_subscribe - self._subscribed

            if unsub:
                await ws.send(json.dumps({"action": "unsubscribe", "symbols": list(unsub)}))
                self._subscribed.difference_update(unsub)
                self._pending_unsubscribe.difference_update(unsub)

            if sub:
                await ws.send(json.dumps({"action": "subscribe", "symbols": list(sub)}))
                self._subscribed.update(sub)
                self._pending_subscribe.difference_update(sub)

            await asyncio.sleep(0.1)

    async def _receive_loop(self, ws):
        """接收行情消息循环"""
        async for raw_msg in ws:
            if not self._active:
                break
            try:
                q_raw = json.loads(raw_msg)
                sym = q_raw.get("symbol", "")
                bid = float(q_raw.get("bid", 0))
                ask = float(q_raw.get("ask", 0))
                last = float(q_raw.get("last", 0))
                vol = int(q_raw.get("volume", 0))

                if not sym or (bid == 0 and ask == 0):
                    continue

                quote = {
                    "symbol": sym,
                    "bid": bid,
                    "ask": ask,
                    "last": last,
                    "volume": vol,
                    "timestamp": datetime.datetime.now().strftime("%H:%M:%S"),
                }
                if self.on_quote:
                    self.on_quote(quote)
            except Exception:
                continue

    def _stop_loop_tasks(self):
        pass  # loop关闭时会自动cancel tasks
