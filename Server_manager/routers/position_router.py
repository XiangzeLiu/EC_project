"""
Position Router & Health Check
持仓查询 + 健康检查端点

支持两种模式：
- 已连接 Tastytrade: 真实持仓数据
- 未连接 (Demo 模式): 返回模拟持仓数据
"""

import logging
from fastapi import APIRouter, HTTPException, Depends

from models import HealthResponse
from config import session_store, quote_clients, subscribed_syms, log, is_configured
from services.tastytrade_svc import get_session_account, SDK_OK
from auth import verify_token

router = APIRouter(tags=["持仓与健康检查"])


# Demo 模式下的模拟持仓数据
_DEMO_POSITIONS = [
    {
        "symbol": "AAPL",
        "quantity": 100.0,
        "direction": "Long",
        "average_open_price": 185.20,
        "close_price": 189.42,
        "realized_today": 155.0,
    },
    {
        "symbol": "BIL",
        "quantity": 0.0,
        "direction": "—",
        "average_open_price": 91.56,
        "close_price": 91.56,
        "realized_today": -8.0,
    },
    {
        "symbol": "NVDA",
        "quantity": 50.0,
        "direction": "Long",
        "average_open_price": 890.00,
        "close_price": 875.20,
        "realized_today": -120.0,
    },
]


@router.get("/positions")
async def get_positions(_=Depends(verify_token)):
    """
    获取当前持仓列表
    返回每个持仓的 symbol, quantity, direction, average_open_price 等

    未连接 Tastytrade 时返回 Demo 模拟数据
    """
    if not is_configured() or not session_store.get("connected") or not SDK_OK:
        log.info("Returning demo positions (broker not connected)")
        return {"positions": _DEMO_POSITIONS}

    try:
        s, a = await get_session_account()
        rows = await a.get_positions(s)
        result = []
        for p in rows:
            result.append({
                "symbol":             p.symbol,
                "quantity":           float(p.quantity),
                "direction":          getattr(p, "quantity_direction", "Long"),
                "average_open_price": float(p.average_open_price or 0),
                "close_price":        float(p.close_price or 0),
                "realized_today":     float(getattr(p, "realized_today", 0) or 0),
            })
        return {"positions": result}
    except Exception as e:
        log.error(f"Get positions failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查端点（无需认证）"""
    ib_connected = False
    from services.quote_service import get_ib_app
    ib_app = get_ib_app()
    if ib_app:
        try:
            ib_connected = ib_app.isConnected()
        except Exception:
            pass

    return HealthResponse(
        status="ok",
        connected=session_store.get("connected", False),
        ib_connected=ib_connected,
        active_clients=len(quote_clients),
    )
