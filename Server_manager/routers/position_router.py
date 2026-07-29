"""
Position Router & Health Check.

SM keeps only the health endpoint here.
"""

from fastapi import APIRouter

from models import HealthResponse

router = APIRouter(tags=["Health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """SM control-plane health check endpoint."""
    return HealthResponse(
        status="ok",
        connected=False,
        ib_connected=False,
        active_clients=0,
    )
