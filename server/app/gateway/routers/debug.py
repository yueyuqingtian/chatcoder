"""调试路由（plan-282-1441 #8）——内置「开发调试」MCP 的状态宿主端点。

MCP 子进程是无状态的（每次调用即 spawn），调试会话状态由本服务按会话隔离持有；
MCP 通过本组端点代理操作，从而让 `debug.paused` 事件能借主服务的 WS 广播给前端
（这是"用户能看到断点停在哪一行"的实现基础）。

会话上下文：请求体里的 `session_id` 即聊天会话 id，由 MCP 通过
`CHATCODER_SESSION_ID` 环境变量获得（见 mcp_wrapper 的占位符与 env 注入）。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.database import get_db
from app.services import debug_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/debug", tags=["debug"])


class Sid(BaseModel):
    session_id: int


class WebStart(Sid):
    port: int = 9222
    target_url: str | None = None


class WebBreakpoint(Sid):
    url_regex: str
    line: int


class WebWait(Sid):
    timeout_s: int = 60


class WebStep(Sid):
    action: str = "over"
    timeout_s: int = 60


class WebEval(Sid):
    expression: str


class JavaAttach(Sid):
    host: str = "127.0.0.1"
    port: int = 5005


class JavaBreakpoint(Sid):
    class_name: str
    line: int


def _guard(res: dict) -> dict:
    """把服务层错误转成 400（MCP 端会读取 detail 作为可读错误）。"""
    if res.get("ok"):
        return res
    raise HTTPException(400, res.get("error") or "调试操作失败")


# ── Web（CDP）──

@router.post("/web/start", response_model=dict)
async def web_start(body: WebStart):
    return _guard(await debug_service.web_start(
        body.session_id, port=body.port, target_url=body.target_url))


@router.post("/web/breakpoint", response_model=dict)
async def web_breakpoint(body: WebBreakpoint):
    return _guard(await debug_service.web_breakpoint(body.session_id, body.url_regex, body.line))


@router.post("/web/wait", response_model=dict)
async def web_wait(body: WebWait):
    return await debug_service.web_wait(body.session_id, body.timeout_s)


@router.post("/web/step", response_model=dict)
async def web_step(body: WebStep):
    return await debug_service.web_step(body.session_id, body.action, body.timeout_s)


@router.post("/web/resume", response_model=dict)
async def web_resume(body: Sid):
    return _guard(await debug_service.web_resume(body.session_id))


@router.post("/web/eval", response_model=dict)
async def web_eval(body: WebEval):
    return _guard(await debug_service.web_eval(body.session_id, body.expression))


@router.post("/web/stop", response_model=dict)
async def web_stop(body: Sid):
    return await debug_service.web_stop(body.session_id)


@router.post("/web/status", response_model=dict)
async def web_status(body: Sid):
    return debug_service.status(body.session_id, "web")


# ── Java（JDWP）──

@router.post("/java/attach", response_model=dict)
async def java_attach(body: JavaAttach):
    return _guard(await debug_service.java_attach(body.session_id, body.host, body.port))


@router.post("/java/breakpoint", response_model=dict)
async def java_breakpoint(body: JavaBreakpoint):
    return _guard(await debug_service.java_breakpoint(body.session_id, body.class_name, body.line))


@router.post("/java/wait", response_model=dict)
async def java_wait(body: WebWait):
    return await debug_service.java_wait(body.session_id, body.timeout_s)


@router.post("/java/step", response_model=dict)
async def java_step(body: WebStep):
    return await debug_service.java_step(body.session_id, body.action, body.timeout_s)


@router.post("/java/resume", response_model=dict)
async def java_resume(body: Sid):
    return _guard(await debug_service.java_resume(body.session_id))


@router.post("/java/stop", response_model=dict)
async def java_stop(body: Sid):
    return await debug_service.java_stop(body.session_id)


@router.post("/java/status", response_model=dict)
async def java_status(body: Sid):
    return debug_service.status(body.session_id, "java")


# ── plan-308-1542 需求7-A：断点明细 / 删除 / 清空（右侧面板可视化与操作）──


class BreakpointListBody(BaseModel):
    session_id: int
    target: str = "web"


class BreakpointRemoveBody(BaseModel):
    session_id: int
    target: str = "web"
    breakpoint_id: str


@router.post("/breakpoints", response_model=dict)
async def breakpoints(body: BreakpointListBody):
    """列出会话内断点明细（Web=URL 正则+行号；Java=类+行号）。"""
    return debug_service.list_breakpoints(body.session_id, body.target)


@router.post("/breakpoints/remove", response_model=dict)
async def breakpoints_remove(body: BreakpointRemoveBody):
    """删除单个断点（按 breakpointId / requestId）。"""
    return _guard(await debug_service.remove_breakpoint(body.session_id, body.target, body.breakpoint_id))


@router.post("/breakpoints/clear", response_model=dict)
async def breakpoints_clear(body: BreakpointListBody):
    """清空会话内全部断点。"""
    return _guard(await debug_service.clear_breakpoints(body.session_id, body.target))


# ── 面板配置（落库，修「改了不保存」）──
#
# Web 调试端口 / JDWP 主机·端口此前只在前端组件 state 里，重新进入面板即丢。


class DebugSettingsBody(BaseModel):
    web_port: int | None = None
    jdwp_host: str | None = None
    jdwp_port: int | None = None


@router.get("/settings", response_model=dict)
async def get_debug_settings(db: AsyncSession = Depends(get_db)):
    """读「开发调试」面板配置（未保存过时返回默认值 + saved=false）。"""
    return await debug_service.get_settings(db)


@router.put("/settings", response_model=dict)
async def save_debug_settings(body: DebugSettingsBody, db: AsyncSession = Depends(get_db)):
    """保存「开发调试」面板配置到数据库。"""
    return await debug_service.save_settings(db, **body.model_dump(exclude_unset=True))


# ── plan-308-1542 需求7-B：与 IntelliJ IDEA 的双向断点通道 ──
#
# 用户要求：IDEA 里打的断点本软件可见；AI 打的断点 IDEA 里也能看到。
# 通道 = 工程内的 .idea/workspace.xml（IDEA 的断点存储），写前备份、
# 且必须在 UI/工具返回中提示"需重启 IDEA 生效、可能被 IDEA 覆盖"。


class IdeaProjectBody(BaseModel):
    project_path: str


class IdeaBreakpointBody(BaseModel):
    project_path: str
    file: str
    line: int
    # 可选：同步写入 IDEA 配置（AI 下断点想"IDEA 也可见"时传 true）
    sync_idea: bool = False


@router.post("/idea/breakpoints", response_model=dict)
async def idea_breakpoints(body: IdeaProjectBody):
    """读取 IDEA 工程内已配置的断点（纯文件解析，不改动任何东西）。"""
    from app.services import idea_service
    return idea_service.list_breakpoints(body.project_path)


@router.post("/idea/breakpoints/add", response_model=dict)
async def idea_breakpoints_add(body: IdeaBreakpointBody):
    """向 IDEA 工程写入断点（写前备份 workspace.xml）。

    返回的 `warning` 必须让用户看到：需重启 IDEA 生效，且运行期可能被 IDEA 覆盖。
    """
    from app.services import idea_service
    return idea_service.add_breakpoint(body.project_path, body.file, body.line)


@router.post("/idea/breakpoints/remove", response_model=dict)
async def idea_breakpoints_remove(body: IdeaBreakpointBody):
    """从 IDEA 工程移除断点。"""
    from app.services import idea_service
    return idea_service.remove_breakpoint(body.project_path, body.file, body.line)


@router.post("/idea/session", response_model=dict)
async def idea_session(body: IdeaProjectBody):
    """探测 IDEA 调试会话（JDWP 占用）与本机可 attach 的 JVM。"""
    from app.services import idea_service
    return idea_service.detect_debug_session(body.project_path)


@router.post("/idea/method-at-line", response_model=dict)
async def idea_method_at_line(body: IdeaBreakpointBody):
    """由「文件:行」推导 class#method（用于为 IDEA 断点建立 Arthas 观测）。"""
    from app.services import idea_service
    return idea_service.method_at_line(body.project_path, body.file, body.line)


# ── Arthas（现场诊断，可与 IDEA 调试并存）──
#
# 与上面 Java（JDWP）通道的区别：JDWP 一个 JVM 只能被一个调试器连接，IDEA 调试期间
# 第三方连不上（方案 §1.2 实测）；Arthas 走 Attach API，是另一条通道，可共存。


class ArthasProcesses(Sid):
    java_home: str | None = None


class ArthasAttach(Sid):
    pid: int
    http_port: int | None = None
    java_home: str | None = None


class ArthasExec(Sid):
    command: str
    exec_timeout_ms: int = 10000
    # `async` 是 Python 关键字，用别名接收（MCP 侧传 "async"）
    run_async: bool = Field(default=False, alias="async")

    model_config = ConfigDict(populate_by_name=True)


class ArthasPull(Sid):
    job_id: str | int | None = None
    timeout_s: float = 30.0


class ArthasInterrupt(Sid):
    job_id: str | int


class ArthasConfig(BaseModel):
    java_home: str | None = None
    boot_jar: str | None = None
    repo_mirror: str | None = None
    idle_timeout_sec: int | None = None
    http_port: int | None = None
    arthas_dir: str | None = None


@router.post("/arthas/processes", response_model=dict)
async def arthas_processes(body: ArthasProcesses):
    """列出本机可 attach 的 JVM（含是否处于 IDEA 调试中）。"""
    from app.services import arthas_service as svc
    return await svc.list_processes(body.java_home)


@router.post("/arthas/attach", response_model=dict)
async def arthas_attach(body: ArthasAttach):
    from app.services import arthas_service as svc
    return _guard(await svc.attach(body.session_id, body.pid,
                                   http_port=body.http_port, java_home=body.java_home))


@router.post("/arthas/exec", response_model=dict)
async def arthas_exec(body: ArthasExec):
    from app.services import arthas_service as svc
    if body.run_async:
        return _guard(await svc.async_command(body.session_id, body.command,
                                              exec_timeout_ms=body.exec_timeout_ms))
    return _guard(await svc.exec_command(body.session_id, body.command,
                                        exec_timeout_ms=body.exec_timeout_ms))


@router.post("/arthas/pull", response_model=dict)
async def arthas_pull(body: ArthasPull):
    from app.services import arthas_service as svc
    return _guard(await svc.pull(body.session_id, body.job_id, timeout_s=body.timeout_s))


@router.post("/arthas/interrupt", response_model=dict)
async def arthas_interrupt(body: ArthasInterrupt):
    from app.services import arthas_service as svc
    return _guard(await svc.interrupt(body.session_id, body.job_id))


@router.post("/arthas/stop", response_model=dict)
async def arthas_stop(body: Sid):
    from app.services import arthas_service as svc
    return await svc.stop(body.session_id)


@router.post("/arthas/status", response_model=dict)
async def arthas_status(body: Sid):
    from app.services import arthas_service as svc
    return svc.status(body.session_id)


@router.post("/arthas/config", response_model=dict)
async def get_arthas_config():
    """当前 Arthas 配置与环境探测结果（调试面板展示“为何还不能用”）。"""
    from app.services import arthas_service as svc
    return svc.config_info()


@router.put("/arthas/config", response_model=dict)
async def set_arthas_config(body: ArthasConfig):
    from app.services import arthas_service as svc
    return svc.save_config(body.model_dump(exclude_unset=True))


# ── 数据库连接（配置页用）──


class DbConnCreate(BaseModel):
    project_id: int
    name: str
    kind: str
    host: str
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    params: dict | None = None
    is_active: bool = False


class DbConnUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    # 显式传 password 才更新；None 表示不改动
    password: str | None = None
    params: dict | None = None
    is_active: bool | None = None


class DbPolicyBody(BaseModel):
    allow_read: bool | None = None
    allow_write: bool | None = None
    allow_ddl: bool | None = None
    require_approval: bool | None = None
    row_limit: int | None = None
    timeout_s: int | None = None


db_router = APIRouter(prefix="/db", tags=["db"])


@db_router.get("/connections", response_model=list[dict])
async def list_db_connections(project_id: int, db: AsyncSession = Depends(get_db)):
    from app.services import db_connection_service as svc
    return await svc.list_connections(db, project_id)


@db_router.post("/connections", response_model=dict)
async def create_db_connection(body: DbConnCreate, db: AsyncSession = Depends(get_db)):
    from app.services import db_connection_service as svc
    try:
        cid = await svc.create_connection(
            db, project_id=body.project_id, name=body.name, kind=body.kind, host=body.host,
            port=body.port, database=body.database, username=body.username,
            password=body.password, params=body.params, is_active=body.is_active,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": cid}


@db_router.patch("/connections/{conn_id}", response_model=dict)
async def update_db_connection(conn_id: int, body: DbConnUpdate, db: AsyncSession = Depends(get_db)):
    from app.services import db_connection_service as svc
    try:
        ok = await svc.update_connection(db, conn_id, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not ok:
        raise HTTPException(404, "连接不存在")
    return {"ok": True}


@db_router.delete("/connections/{conn_id}", response_model=dict)
async def delete_db_connection(conn_id: int, db: AsyncSession = Depends(get_db)):
    from app.services import db_connection_service as svc
    if not await svc.delete_connection(db, conn_id):
        raise HTTPException(404, "连接不存在")
    return {"ok": True}


@db_router.post("/connections/{conn_id}/test", response_model=dict)
async def test_db_connection(conn_id: int, db: AsyncSession = Depends(get_db)):
    from app.services import db_connection_service as svc
    try:
        return await svc.test_connection(db, conn_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:  # noqa: BLE001 —— 连接失败原因要原样给用户看
        return {"ok": False, "error": str(e)}


@db_router.get("/policy", response_model=dict)
async def get_db_policy(project_id: int, db: AsyncSession = Depends(get_db)):
    from app.services import db_connection_service as svc
    return await svc.get_policy(db, project_id)


@db_router.put("/policy", response_model=dict)
async def set_db_policy(project_id: int, body: DbPolicyBody, db: AsyncSession = Depends(get_db)):
    from app.services import db_connection_service as svc
    return await svc.set_policy(db, project_id, **body.model_dump(exclude_unset=True))
