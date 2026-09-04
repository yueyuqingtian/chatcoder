"""FastAPI 应用工厂（v2）。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.logging import setup_logging
from app.gateway.routers import (
    diagnostics,
    exec_policy,
    hooks,
    memories,
    models,
    profiles,
    projects,
    providers,
    scheduled,
    sessions,
    settings as settings_routes,
    skills_mcp,
    subagents,
    ta3_auth,
    trae_auth,
    turns,
    upload,
    usage,
    workbuddy_auth,
)
from app.gateway.ws import ws_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging(debug=settings.debug)
    # 初始化数据库 + 幂等种子
    try:
        from app.persistence.database import init_db, async_session_factory
        from app.persistence.migrations import run_migrations
        from app.persistence.seed import seed
        await init_db()
        # 幂等补列(与打包入口 run_server.py 保持一致)
        async with async_session_factory() as db:
            await run_migrations(db)
            from sqlalchemy import select
            from app.persistence.models.turn import Turn
            from app.persistence.models.task import Task
            from app.persistence.models.agent import Agent
            from datetime import datetime, timezone
            stale = list((await db.execute(select(Turn).where(Turn.status == "running"))).scalars().all())
            for turn in stale:
                turn.status = "interrupted"
                turn.summary = turn.summary or "应用关闭时任务已停止"
                turn.completed_at = turn.completed_at or datetime.now(timezone.utc).isoformat()
                rows = (await db.execute(select(Task).where(Task.turn_id == turn.id, Task.status.in_(["proposed", "pending", "running", "in_progress"]))))
                for task in rows.scalars().all():
                    task.status = "cancelled"
                    task.note = task.note or "应用关闭时任务已停止"
                agents = (await db.execute(select(Agent).where(Agent.turn_id == turn.id, Agent.status == "running")))
                for agent in agents.scalars().all():
                    agent.status = "terminated"
            await db.commit()
        await seed()
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("数据库初始化失败: %s", e)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="chatcoder API",
        version="0.4.0",
        description="AI 编码代理工作台 - 服务端（v2 项目任务驱动）",
        lifespan=lifespan,
        debug=settings.debug,
    )

    if settings.cors_allow_all:
        app.add_middleware(
            CORSMiddleware,
            allow_origin_regex=".*",
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # 统一错误响应（规范 §4.8）
    @app.exception_handler(StarletteHTTPException)
    async def http_exc_handler(request: Request, exc: StarletteHTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": f"http_{exc.status_code}", "message": str(exc.detail)}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exc_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "validation_error", "message": "请求参数错误", "detail": exc.errors()}},
        )

    @app.exception_handler(Exception)
    async def unhandled_exc_handler(request: Request, exc: Exception):
        import logging
        logging.getLogger("app").exception("未处理异常: %s", exc)
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": str(exc)[:300]}},
        )

    # 路由
    app.include_router(projects.router, prefix="/api", tags=["projects"])
    app.include_router(sessions.router, prefix="/api", tags=["sessions"])
    app.include_router(turns.router, prefix="/api", tags=["turns"])
    app.include_router(models.router, prefix="/api", tags=["models"])
    app.include_router(providers.router, prefix="/api", tags=["providers"])
    app.include_router(scheduled.router, prefix="/api", tags=["scheduled"])
    app.include_router(profiles.router, prefix="/api", tags=["profiles"])
    app.include_router(exec_policy.router, prefix="/api", tags=["exec-policy"])
    app.include_router(hooks.router, prefix="/api", tags=["hooks"])
    app.include_router(memories.router, prefix="/api", tags=["memories"])
    app.include_router(skills_mcp.router, prefix="/api", tags=["skills-mcp"])
    app.include_router(settings_routes.router, prefix="/api", tags=["settings"])
    app.include_router(usage.router, prefix="/api", tags=["usage"])
    app.include_router(subagents.router, prefix="/api", tags=["subagents"])
    app.include_router(ta3_auth.router, prefix="/api", tags=["ta3"])
    app.include_router(workbuddy_auth.router, prefix="/api", tags=["workbuddy"])
    app.include_router(trae_auth.router, prefix="/api", tags=["trae"])
    app.include_router(upload.router, prefix="/api", tags=["upload"])
    app.include_router(diagnostics.router, prefix="/api", tags=["diagnostics"])
    app.include_router(ws_router, tags=["websocket"])

    # 健康检查
    @app.get("/api/health", tags=["health"])
    async def health():
        return {"status": "ok", "version": "0.4.0"}

    return app


app = create_app()
