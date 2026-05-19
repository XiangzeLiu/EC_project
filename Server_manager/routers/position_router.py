"""
Position Router & Health Check（控制面）

SM 保留健康检查；持仓查询已迁移到 SE。
"""

from fastapi import APIRouter, HTTPException, Depends

from models import HealthResponse
from config import quote_clients
from auth import verify_token

router = APIRouter(tags=["持仓与健康检查"])


def _control_plane_only() -> None:
    raise HTTPException(
        status_code=410,
        detail="SM no longer executes trading requests. Please query positions via SE.",
    )



@router.get("/positions")
async def get_positions(_=Depends(verify_token)):
    """已废弃：持仓查询迁移到 SE"""
    _control_plane_only()



@router.get("/health", response_model=HealthResponse)
async def health_check():
    """SM 控制面健康检查端点（无需认证）"""
    return HealthResponse(
        status="ok",
        connected=False,
        ib_connected=False,
        active_clients=len(quote_clients),
    )

