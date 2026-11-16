"""社区微应用路由（plan-693-2868 §6.5 / §7 P5）。

只读 + 下载：`/api/community/{list,detail,install,blocklist,identity}`。
发布与撤回**不在这里**——它们走 Electron IPC（`community:publish` / `community:withdraw`），
用户 PAT 不出主进程（§6.5），所以本路由没有任何写 GitHub 的入口。

错误映射（`community_service.CommunityError.code` → HTTP）：
    NOT_FOUND → 404（索引没有该条目 / 已被拉黑）
    NETWORK   → 503（三级读链路全失败、下载失败）——文案「无法连接社区」，本地库不受影响
    INTEGRITY → 422（sha256 / size 不符、解压失败、包与索引不一致）
    DISABLED  → 503（社区未配置；`list` 例外，返回 `{apps: [], enabled: false}` 不报错）
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import community_service
from app.services.community_service import CommunityError

router = APIRouter(prefix="/community", tags=["community"])

_ERROR_STATUS = {
    "NOT_FOUND": 404,
    "NETWORK": 503,
    "INTEGRITY": 422,
    "DISABLED": 503,
}


def _http_error(exc: CommunityError) -> HTTPException:
    """把服务层结构化错误翻成 HTTP 错误（detail 用可读中文，界面直接展示）。"""
    return HTTPException(_ERROR_STATUS.get(exc.code, 500), str(exc))


@router.get("/list", response_model=dict)
async def list_community_apps(q: str = "", refresh: bool = False):
    """社区应用列表。

    - `q`：按 name / appId / desc 模糊匹配（§6.4）；
    - `refresh=true`：跳过 TTL 缓存直接打远端（用户点「刷新」时用，raw 带 `?t=` 击穿 CDN）；
    - 社区未配置时返回 `{apps: [], enabled: false}`，前端据此隐藏入口（§7 P5 第 8 条）；
    - `stale=true` 表示当前数据来自过期本地缓存（远端不可达的降级态），界面应提示。
    """
    try:
        return await community_service.list_apps(q, force=refresh)
    except CommunityError as exc:
        raise _http_error(exc) from exc


@router.get("/detail", response_model=dict)
async def get_community_app(appId: str = "", version: str = ""):
    """社区条目详情：索引条目 + 本机是否已装（不代表已注入/已启用）。"""
    try:
        return await community_service.get_app(appId, version)
    except CommunityError as exc:
        raise _http_error(exc) from exc


class CommunityInstallBody(BaseModel):
    """安装请求：appId + 精确版本（索引里必须存在该条目，§6.4 同版本不覆盖）。"""

    appId: str
    version: str


@router.post("/install", response_model=dict)
async def install_community_app(body: CommunityInstallBody):
    """从社区安装：下载 → 校验 sha256/size → 解压 → 落库 → 写 `.origin.json`。

    装完**默认不启用**（返回值 `enabled: False`）：微应用没有数据库行，是否注入由用户在
    前端决定（§6.4「默认不启用」）。
    """
    try:
        return await community_service.install_app(body.appId, body.version)
    except CommunityError as exc:
        raise _http_error(exc) from exc


class CommunityBlocklistBody(BaseModel):
    """拉黑表（PUT 全量提交）：`users` 按 authorId、`apps` 按 appId。

    字段为 None 表示「本次不改这一档」；空数组表示「清空这一档」。
    """

    users: list[str] | None = None
    apps: list[str] | None = None


@router.get("/blocklist", response_model=dict)
async def get_community_blocklist():
    """读拉黑表（两档，§6.4 拉黑一节）。"""
    return community_service.get_blocklist()


@router.put("/blocklist", response_model=dict)
async def set_community_blocklist(body: CommunityBlocklistBody):
    """写拉黑表；只影响社区列表与安装，不影响已装本地副本。"""
    try:
        return community_service.update_blocklist(users=body.users, apps=body.apps)
    except OSError as exc:
        raise HTTPException(500, f"拉黑表写入失败：{exc}") from exc


@router.get("/identity", response_model=dict)
async def get_community_identity():
    """本机社区身份（`usr_<12 hex>`，首次调用时生成并落盘，幂等）。"""
    try:
        return community_service.identity()
    except OSError as exc:
        raise HTTPException(500, f"社区身份读写失败：{exc}") from exc
