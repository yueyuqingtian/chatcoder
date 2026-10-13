"""端到端验证用的后端：真实的 FastAPI 应用、真实的 SQLite 数据库、真实的 git 仓库与真实的调度循环。

沙箱里没有可用的模型供应商，所以节点的模型调用由确定性的执行器代替；执行器真实地写文件（写在节点
自己的工作区里），并在实现节点执行期间模拟用户在主工作区的一次编辑。其余链路（HTTP、WebSocket、
数据库、工作树、并入、侧栏状态）全部是产品代码。

用法：python e2e_serve.py <数据目录> <端口>
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

DATA = Path(sys.argv[1]).resolve()
PORT = int(sys.argv[2])
# 本文件位于 <仓库>/docs/handoff/e2e/，服务端目录取仓库根下的 server，换电脑后无需修改
SERVER = Path(__file__).resolve().parents[3] / "server"
REPO = DATA / "repo"

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + (DATA / "e2e.db").as_posix()
os.environ["WORKSPACE_ROOT"] = str(DATA / "ws")
os.environ["REDIS_URL"] = "redis://localhost:6379/15"
os.environ["QDRANT_URL"] = "http://localhost:6333"
os.environ["JWT_SECRET"] = "e2e-secret"
sys.path.insert(0, str(SERVER))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.core.enums import MsgType, SenderType  # noqa: E402
from app.main import app  # noqa: E402
from app.orchestration.workflow import service, store  # noqa: E402
from app.orchestration.workflow.executor import NodeResult  # noqa: E402
from app.orchestration.workflow.runtime import WorkflowRuntime  # noqa: E402
from app.persistence.database import Base, async_session_factory, engine  # noqa: E402
from app.persistence.models.agent import Agent  # noqa: E402
from app.persistence.models.turn import Turn  # noqa: E402
from app.services import message_service  # noqa: E402

PASS = (
    '{"decision": "pass", "summary": "评估通过", '
    '"criteria": [{"id": "C1", "result": "met", "evidence": "余额不足时返回 409"}], "issues": []}'
)

PLAN = {
    "title": "积分兑换功能",
    "goal": "实现积分兑换：后端扣减与前端兑换页",
    "rationale": "后端扣减与前端兑换页分开实现，最后联调",
    "flows": [
        {
            "key": "backend",
            "title": "后端：积分扣减",
            "isolation": "worktree",
            "nodes": [
                {"key": "explore", "kind": "explore", "role": "explorer", "task": "探索现有积分接口"},
                {
                    "key": "impl",
                    "kind": "execute",
                    "role": "implementer",
                    "task": "实现扣减逻辑",
                    "inputs": ["explore"],
                    "write_scope": ["server/app/points.py"],
                    "acceptance": ["余额不足时返回 409"],
                },
                {
                    "key": "review",
                    "kind": "review",
                    "role": "reviewer",
                    "task": "评估扣减结果",
                    "inputs": ["impl"],
                    "rework_to": "impl",
                    "max_rework": 1,
                    "acceptance": ["余额不足时返回 409"],
                },
            ],
        },
        {
            "key": "frontend",
            "title": "前端：兑换页",
            "depends_on": ["backend"],
            "nodes": [{"key": "explore", "kind": "explore", "role": "explorer", "task": "探索兑换页"}],
        },
    ],
}

TEMPLATE_PLAN = {
    "title": "两阶段积分流程",
    "goal": "先实现，再做前端",
    "flows": [
        {
            "key": "first",
            "title": "第一阶段",
            "nodes": [
                {"key": "explore", "kind": "explore", "role": "explorer", "task": "探索"},
                {
                    "key": "impl",
                    "kind": "execute",
                    "role": "implementer",
                    "task": "实现",
                    "inputs": ["explore"],
                    "write_scope": ["server/**"],
                    "acceptance": ["功能可用"],
                },
            ],
        },
        {
            "key": "second",
            "title": "第二阶段",
            "depends_on": ["first"],
            "nodes": [{"key": "explore", "kind": "explore", "role": "explorer", "task": "探索界面"}],
        },
    ],
}


def _git(*args: str) -> None:
    subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True)


def make_repo() -> None:
    """建一个真实的 git 仓库（已存在则保留，便于重复验证时继续使用同一份状态）。"""
    if (REPO / ".git").exists():
        return
    (REPO / "server" / "app").mkdir(parents=True)
    (REPO / "docs").mkdir()
    _git("init", "-q")
    _git("config", "user.email", "e2e@example.com")
    _git("config", "user.name", "e2e")
    (REPO / "server" / "app" / "points.py").write_bytes(b"def redeem(points, cost):\n    return points - cost\n")
    (REPO / "README.md").write_bytes("# 积分服务\n".encode("utf-8"))
    _git("add", "-A")
    _git("commit", "-qm", "初始提交")


async def _node_agent(req) -> int | None:
    """为实现节点建立子代理记录（与真实的节点执行器一样，节点与子代理互相关联）。"""
    try:
        async with async_session_factory() as db:
            row = Agent(
                kind="sub",
                name=f"{req.node_key} 子代理",
                session_id=req.session_id,
                turn_id=req.turn_id,
                workflow_node_id=req.node_id,
                status="done",
            )
            db.add(row)
            await db.flush()
            agent_id = int(row.id)
            await db.commit()
        await store.update_node(req.node_id, agent_id=agent_id)
        return agent_id
    except Exception as exc:  # 子代理记录只影响面板展示，失败不影响节点执行
        print("[e2e] 子代理记录失败：", exc, flush=True)
        return None


class E2EExecutor:
    """沙箱中的确定性节点执行器：写文件、记录令牌用量，并在实现节点执行期间模拟用户的一次编辑。"""

    def __init__(self) -> None:
        self.user_edit_done = False

    async def run(self, req) -> NodeResult:
        await asyncio.sleep(4)  # 让界面能看到运行中的状态
        # 测试钩子：文件内容为节点键时，该节点的下一次执行失败（一次性，用于验证失败后的重试）
        marker = DATA / "fail_once.txt"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == req.node_key:
            marker.unlink()
            return NodeResult(status="failed", error="模拟：模型接口返回 500，重试次数已用完")
        if req.kind == "review":
            notes = Path(req.workspace) / "docs" / "notes.md"
            verdict = PASS.replace(
                "评估通过", f"评估通过；评估时工作区里能看到用户的补充说明：{notes.exists()}"
            )
            return NodeResult(status="done", text=verdict, tokens=12000)
        if req.node_key == "impl":
            agent_id = await _node_agent(req)
            target = Path(req.workspace) / "server" / "app" / "points.py"
            target.write_bytes(
                "def redeem(points, cost):\n"
                "    if points < cost:\n"
                "        raise ValueError(409)\n"
                "    return points - cost\n".encode("utf-8")
            )
            if not self.user_edit_done:
                self.user_edit_done = True
                # 用户在主工作区补充说明（运行期间的编辑）：下一个节点开始前应被同步进工作树
                main_notes = REPO / "docs" / "notes.md"
                main_notes.write_bytes("运行期间由用户补充的说明\n".encode("utf-8"))
            return NodeResult(
                status="done",
                output={"summary": "已实现扣减逻辑，余额不足返回 409"},
                text="done",
                tokens=30000,
                agent_id=agent_id,
            )
        return NodeResult(
            status="done",
            output={"summary": f"{req.node_key} 完成", "affected_files": ["server/app/points.py"]},
            text="done",
            tokens=8000,
        )


GATE_PLAN = {
    "title": "闸门与检查",
    "goal": "验证闸门与检查节点",
    "rationale": "先探索，经你确认后再运行检查",
    "flows": [{
        "key": "backend",
        "title": "后端",
        "nodes": [
            {"key": "explore", "kind": "explore", "title": "探索", "task": "探索服务与模型"},
            {"key": "confirm", "kind": "gate", "title": "确认", "inputs": ["explore"],
             "question": "是否继续进入检查？", "task": "确认"},
            {"key": "verify", "kind": "check", "title": "检查", "inputs": ["confirm"],
             "commands": ["pytest -q"], "task": "运行测试"},
        ],
    }],
}


async def seed(client: httpx.AsyncClient) -> int:
    """创建项目、会话、程序与规划消息；返回会话编号。"""
    make_repo()
    # 用户还没提交的改动：开始前会出现确认弹层
    (REPO / "README.md").write_bytes("# 积分服务\n\n开始前由用户补充的说明\n".encode("utf-8"))
    project = (await client.post("/api/projects", json={"path": str(REPO), "name": "E2E 积分服务"})).json()
    session = (
        await client.post("/api/sessions", json={"project_id": project["id"], "title": "积分兑换功能"})
    ).json()
    sid = int(session["id"])
    runtime = service.get_runtime()
    pid = await runtime.create_program(session_id=sid, plan=PLAN, workspace=str(REPO))
    await store.update_run(pid, token_budget=100000)
    await store.save_template(TEMPLATE_PLAN, name="两阶段积分流程", description="先实现，再做界面")
    gate_session = (
        await client.post("/api/sessions", json={"project_id": project["id"], "title": "闸门与检查"})
    ).json()
    gate_pid = await runtime.create_program(session_id=int(gate_session["id"]), plan=GATE_PLAN, workspace=str(REPO))
    print(f"[e2e] gate session={gate_session['id']} program={gate_pid}", flush=True)
    async with async_session_factory() as db:
        turn = Turn(session_id=sid, status="completed")
        db.add(turn)
        await db.flush()
        turn_id = int(turn.id)
        await db.commit()
    async with async_session_factory() as db:
        await message_service.create_message(
            db, session_id=sid, turn_id=turn_id, sender_type=SenderType.USER.value,
            msg_type=MsgType.TEXT.value, content={"text": "帮我实现积分兑换功能，前后端都要，最后联调"},
            broadcast=False,
        )
        await message_service.create_message(
            db, session_id=sid, turn_id=turn_id, sender_type=SenderType.AGENT.value,
            msg_type=MsgType.TOOL_CALL.value,
            content={"tool": "plan_workflow", "args": {"title": PLAN["title"]}, "call_key": "call_plan_1"},
            broadcast=False,
        )
        await message_service.create_message(
            db, session_id=sid, turn_id=turn_id, sender_type=SenderType.AGENT.value,
            msg_type=MsgType.TOOL_RESULT.value,
            content={
                "tool": "plan_workflow", "call_key": "call_plan_1", "ok": True,
                "output": f"已生成工作流计划，program_id={pid}，等待你确认后开始执行。",
                "error": "",
            },
            broadcast=False,
        )
        await message_service.create_message(
            db, session_id=sid, turn_id=turn_id, sender_type=SenderType.AGENT.value,
            msg_type=MsgType.TEXT.value,
            content={"text": "已为你规划好工作流：后端与前端并行，最后联调。"},
            broadcast=False,
        )
    print(f"[e2e] session={sid} program={pid} repo={REPO}", flush=True)
    return sid


async def main() -> None:
    make_repo()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    service._runtime = WorkflowRuntime(
        E2EExecutor(), limits=service._limits_from_settings(), notify=service._notify_main_agent
    )
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    serve = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.1)
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}", timeout=30) as client:
        await seed(client)
    await serve


if __name__ == "__main__":
    asyncio.run(main())
