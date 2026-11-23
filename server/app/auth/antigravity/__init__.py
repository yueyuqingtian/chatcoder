"""antigravity（Google Antigravity）认证与协议支持包。

方案 docs/plan-antigravity-provider-integration.md。

模块划分：
- token：端点/客户端凭据/ClientMetadata 枚举与请求头（单一配置源）
- session：登录态存取与 ensure_token（过期自动刷新）
- oauth：导入本机登录态 + 应用内独立 Google OAuth 登录（含 loadCodeAssist）
- catalog：模型目录解析（fetchAvailableModels）与内置兜底清单
- callback：独立登录的本地回环回调服务
"""
from app.auth.antigravity import callback, catalog, oauth, session, token  # noqa: F401

__all__ = ["callback", "catalog", "oauth", "session", "token"]
