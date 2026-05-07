"""
Order Router
订单操作端点：下单、撤单、查询订单（活跃/历史）

支持两种模式：
- 已连接 Tastytrade: 真实订单执行
- 未连接 (Demo 模式): 返回模拟数据
"""

import logging
import time
import uuid
from fastapi import APIRouter, HTTPException, Depends

from models import PlaceOrderRequest, PlaceOrderResponse, CancelOrderResponse
from config import session_store, log, is_configured
from services.tastytrade_svc import get_session_account, serialize_order, SDK_OK
from auth import verify_token

router = APIRouter(prefix="/orders", tags=["订单管理"])


def _demo_order_response(req: PlaceOrderRequest) -> PlaceOrderResponse:
    """Demo 模拟下单响应"""
    oid = f"DEMO-{uuid.uuid4().hex[:10].upper()}"
    price_str = "MKT" if req.order_type == "market" else f"${req.price}"
    log.info(
        f"[DEMO] Order placed: {req.action} {req.qty} {req.symbol} @ {price_str} | {req.tif}"
    )
    return PlaceOrderResponse(success=True, order_id=oid)


@router.post("/place", response_model=PlaceOrderResponse)
async def place_order(req: PlaceOrderRequest, _=Depends(verify_token)):
    """
    下单
    通过 Tastytrade SDK 将订单发送到券商（已连接时）
    Demo 模式下返回模拟成功响应
    """
    # Demo 模式：未配置 Tastytrade 凭据或未连接
    if not is_configured() or not session_store.get("connected") or not SDK_OK:
        return _demo_order_response(req)

    try:
        s, a = await get_session_account()

        from tastytrade.order import (
            NewOrder, OrderAction, OrderTimeInForce, OrderType,
        )
        from tastytrade.instruments import Equity
        from decimal import Decimal

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

        act = ACTION_MAP.get(req.action, OrderAction.BUY_TO_OPEN)
        tif_enum = TIF_MAP.get(req.tif, OrderTimeInForce.DAY)
        is_buy = "Buy" in req.action

        equity = await Equity.get(s, req.symbol)
        leg = equity.build_leg(Decimal(str(req.qty)), act)

        if req.order_type == "market":
            order = NewOrder(time_in_force=tif_enum,
                             order_type=OrderType.MARKET, legs=[leg])
        else:
            signed = Decimal(str(req.price)) * (-1 if is_buy else 1)
            order = NewOrder(time_in_force=tif_enum,
                             order_type=OrderType.LIMIT, legs=[leg], price=signed)

        resp = await a.place_order(s, order, dry_run=False)
        log.info(
            f"Order placed: {req.action} {req.qty} {req.symbol} "
            f"@ {req.price if req.order_type == 'limit' else 'MKT'}"
        )
        return PlaceOrderResponse(
            success=True,
            order_id=str(resp.order.id),
        )
    except Exception as e:
        log.error(f"Place order failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{order_id}", response_model=CancelOrderResponse)
async def cancel_order(order_id: str, _=Depends(verify_token)):
    """撤销订单"""
    if not is_configured() or not session_store.get("connected") or not SDK_OK:
        log.info(f"[DEMO] Order cancelled: {order_id}")
        return CancelOrderResponse(success=True)

    try:
        s, a = await get_session_account()
        await a.delete_order(s, order_id)
        log.info(f"Order cancelled: {order_id}")
        return CancelOrderResponse(success=True)
    except Exception as e:
        log.error(f"Cancel order failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/live")
async def get_live_orders(_=Depends(verify_token)):
    """获取活动订单列表"""
    if not is_configured() or not session_store.get("connected") or not SDK_OK:
        # Demo 模式：返回空列表
        return {"orders": []}

    try:
        s, a = await get_session_account()
        raw = await a.get_live_orders(s)
        return {"orders": [serialize_order(o) for o in raw]}
    except Exception as e:
        log.error(f"Get live orders failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/history")
async def get_order_history(_=Depends(verify_token)):
    """获取历史订单列表"""
    if not is_configured() or not session_store.get("connected") or not SDK_OK:
        # Demo 模式：返回空列表
        return {"orders": []}

    try:
        s, a = await get_session_account()
        raw = await a.get_order_history(s)
        return {"orders": [serialize_order(o) for o in raw]}
    except Exception as e:
        log.error(f"Get order history failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
