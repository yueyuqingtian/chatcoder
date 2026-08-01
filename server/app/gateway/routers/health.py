"""健康检查与系统信息。"""
from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": "0.1.0",
        "default_model_ready": settings.default_model_ready,
    }
