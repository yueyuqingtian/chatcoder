"""FastAPI 应用工厂（v2）。"""
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.logging import setup_logging
from app.gateway.routers import (
    debug,
    diagnostics,
    exec_policy,
    hooks,
    memories,
    models,
    permission_profiles,
    plugins,
    profiles,
    projects,
    providers,
    scheduled,
    sessions,
    settings as settings_routes,
    skills_mcp,
    subagents,
    symbol_index,
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
            if stale:
                # 启动修复：running → interrupted / 任务 cancelled / agent terminated
                # 经 WriteEngine 单写线程（无锁单写者；async db 仅读）
                from app.persistence.database import run_write_locked

                def _persist(s):
                    for turn in stale:
                        t = s.get(Turn, turn.id)
                        if t is None:
                            continue
                        t.status = "interrupted"
                        t.summary = t.summary or "应用关闭时任务已停止"
                        t.completed_at = t.completed_at or datetime.now(timezone.utc).isoformat()
                        rows = s.execute(select(Task).where(
                            Task.turn_id == turn.id,
                            Task.status.in_(["proposed", "pending", "running", "in_progress"]),
                        ))
                        for task in rows.scalars().all():
                            if task.status != "cancelled":
                                task.status = "cancelled"
                                task.note = task.note or "应用关闭时任务已停止"
                        agents = s.execute(select(Agent).where(Agent.turn_id == turn.id, Agent.status == "running"))
                        for agent in agents.scalars().all():
                            agent.status = "terminated"
                    s.commit()

                await run_write_locked(_persist, label="startup.repair")
        await seed()

        # 启动时自动扫描一次外部工具技能目录（skills CLI / Claude / Codex 等），
        # 这样用户无需手动点「刷新扫描」即可在「拓展 → 技能」看到已安装的技能。
        # 失败不阻塞服务启动。
        try:
            import logging

            from app.services import skill_service
            from app.services import project_service

            _projects = await project_service.list_projects(db)
            _ws = (getattr(_projects[0], "path", None) or None) if _projects else None
            _res = await skill_service.sync_scanned_skills(db, _ws)
            logging.getLogger(__name__).info("启动技能扫描完成: %s", _res)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("启动技能扫描失败（不阻塞服务）")

        # plan-230-1144 M1.1: 启动定时任务调度循环（此前该表无任何消费者，任务永不执行）
        try:
            from app.services import scheduler_loop
            await scheduler_loop.start()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("定时任务调度循环启动失败（不阻塞服务）")
        # plan-248-1258 M2.6: 启动 WorkBuddy 每日自动签到循环（打开软件后后台自动签到）
        try:
            from app.services import workbuddy_checkin
            await workbuddy_checkin.start()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("WorkBuddy 自动签到循环启动失败（不阻塞服务）")
        # plan-248-1258 M3.1: 启动符号索引自动增量循环（已开启索引的工作区保持最新）
        try:
            from app.services import symbol_index_manager
            await symbol_index_manager.start()
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception("符号索引自动增量循环启动失败（不阻塞服务）")
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).exception("数据库初始化失败: %s", e)
    yield
    # plan-230-1144 M1.1: 先停调度循环，再排空写缓冲
    try:
        from app.services import scheduler_loop
        await scheduler_loop.stop()
    except Exception:
        import logging
        logging.getLogger(__name__).debug("shutdown scheduler loop failed", exc_info=True)
    # plan-248-1258 M2.6: 停止 WorkBuddy 自动签到循环
    try:
        from app.services import workbuddy_checkin
        await workbuddy_checkin.stop()
    except Exception:
        import logging
        logging.getLogger(__name__).debug("shutdown workbuddy checkin loop failed", exc_info=True)
    # plan-248-1258 M3.1: 停止符号索引自动增量循环
    try:
        from app.services import symbol_index_manager
        await symbol_index_manager.stop()
    except Exception:
        import logging
        logging.getLogger(__name__).debug("shutdown symbol index loop failed", exc_info=True)
    # 优雅关停：排空 write-behind 缓冲，避免未落库消息在进程退出时丢失。
    try:
        from app.persistence.write_behind import write_behind
        await write_behind.shutdown()
    except Exception:
        logging.getLogger(__name__).debug("shutdown drain write-behind failed", exc_info=True)


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
    app.include_router(permission_profiles.router, prefix="/api", tags=["permission-profiles"])
    app.include_router(exec_policy.router, prefix="/api", tags=["exec-policy"])
    app.include_router(hooks.router, prefix="/api", tags=["hooks"])
    app.include_router(memories.router, prefix="/api", tags=["memories"])
    app.include_router(plugins.router, prefix="/api", tags=["plugins"])
    # plan-282-1441（#7/#8）：内置 MCP 的状态宿主端点（调试会话 + 数据库连接配置）
    app.include_router(debug.router, prefix="/api", tags=["debug"])
    app.include_router(debug.db_router, prefix="/api", tags=["db"])
    app.include_router(skills_mcp.router, prefix="/api", tags=["skills-mcp"])
    app.include_router(settings_routes.router, prefix="/api", tags=["settings"])
    app.include_router(usage.router, prefix="/api", tags=["usage"])
    app.include_router(subagents.router, prefix="/api", tags=["subagents"])
    app.include_router(ta3_auth.router, prefix="/api", tags=["ta3"])
    app.include_router(workbuddy_auth.router, prefix="/api", tags=["workbuddy"])
    app.include_router(trae_auth.router, prefix="/api", tags=["trae"])
    app.include_router(upload.router, prefix="/api", tags=["upload"])
    app.include_router(diagnostics.router, prefix="/api", tags=["diagnostics"])
    # plan-248-1258 M3.2: 代码符号索引管理（索引库页面）
    app.include_router(symbol_index.router, prefix="/api", tags=["symbol-index"])
    app.include_router(ws_router, tags=["websocket"])

    # 健康检查
    @app.get("/api/health", tags=["health"])
    async def health():
        # Electron 用稳定标识区分 ChatCoder 后端与同端口的其它本地服务。
        from app.persistence.database import database_info
        info = database_info()
        # plan-248-1273 M1: 诊断当前实际运行实例，避免 Electron 继续启动旧 exe/旧前端。
        try:
            from app.services import symbol_index_manager
            worker_info = symbol_index_manager.worker_diagnostics()
        except Exception:
            worker_info = {"mode": "legacy-thread", "workers": []}
        return {
            "status": "ok",
            "service": "chatcoder",
            "version": "0.4.0",
            "build_revision": os.environ.get("CHATCODER_BUILD_REVISION", "source"),
            "index_worker_mode": os.environ.get("CHATCODER_INDEX_WORKER_MODE", "process"),
            "pid": os.getpid(),
            "database": info,
            "index_worker": worker_info,
        }

    return app


app = create_app()
