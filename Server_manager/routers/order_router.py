"""
Order Router（控制面）

SM 不再直接执行交易请求。
Client 的交易请求应统一走：Client -> SE -> Broker。
"""

from fastapi import APIRouter, HTTPException, Depends

from models import PlaceOrderRequest, PlaceOrderResponse, CancelOrderResponse
from auth import verify_token

router = APIRouter(prefix="/orders", tags=["订单管理"])


def _control_plane_only() -> None:
    raise HTTPException(
        status_code=410,
        detail="SM no longer executes trading requests. Please route orders via SE.",
    )



@router.post("/place", response_model=PlaceOrderResponse)
async def place_order(req: PlaceOrderRequest, _=Depends(verify_token)):
    """已废弃：交易执行迁移到 SE"""
    _control_plane_only()



@router.delete("/{order_id}", response_model=CancelOrderResponse)
async def cancel_order(order_id: str, _=Depends(verify_token)):
    """已废弃：交易执行迁移到 SE"""
    _control_plane_only()



@router.get("/live")
async def get_live_orders(_=Depends(verify_token)):
    """已废弃：交易执行迁移到 SE"""
    _control_plane_only()



@router.get("/history")
async def get_order_history(_=Depends(verify_token)):
    """已废弃：交易执行迁移到 SE"""
    _control_plane_only()

