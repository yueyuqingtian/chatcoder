"""v31.2: 统一 HTTP 客户端工厂 —— 自动注入全局代理。

代理来源优先级：
1. 运行时 settings.http_proxy（设置面板「HTTP 代理」字段，持久化于 ~/.chatcoder/config.json，
   启动时由 load_persisted_workspace 恢复）
2. 环境变量 HTTPS_PROXY / HTTP_PROXY

web 工具（web_search / web_fetch / 各搜索引擎适配器）统一通过 build_http_client()
创建 httpx client，保证「设置里配置了代理 → 搜索引擎自动走代理」。
"""
import os

import httpx

from app.core.config import settings


def get_proxy_url() -> str | None:
    """返回当前生效的代理 URL；未配置返回 None。"""
    if settings.http_proxy:
        return settings.http_proxy
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or None
    )


def build_http_client(
    *,
    timeout: float = 15.0,
    follow_redirects: bool = True,
    headers: dict | None = None,
    **kwargs,
) -> httpx.AsyncClient:
    """创建 httpx.AsyncClient，自动注入全局代理（若有配置）。

    httpx>=0.26 使用单数 `proxy=` 参数；显式传入时不依赖 trust_env。
    """
    proxy = get_proxy_url()
    if proxy:
        kwargs.setdefault("proxy", proxy)
    merged: dict = {"timeout": timeout, "follow_redirects": follow_redirects}
    if headers:
        merged["headers"] = headers
    merged.update(kwargs)
    return httpx.AsyncClient(**merged)
