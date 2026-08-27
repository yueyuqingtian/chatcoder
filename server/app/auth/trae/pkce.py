"""PKCE(S256) 工具 — TRAE SOLO CN 登录契约（对齐 out/main.js `auth/common/util.js` W7e）。

- codeVerifier  = base64url(48 字节随机)（RFC 7636 允许 43~128 字符）
- codeChallenge = base64url(SHA256(verifier)) 无填充
- code_challenge_method = "S256"（授权 URL 参数）
"""
from __future__ import annotations

import base64
import hashlib
import secrets


def gen_code_verifier() -> str:
    """verifier：base64url(48B)（对齐 Node randomBytes(48).toString("base64url")）。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")


def s256_challenge(verifier: str) -> str:
    """challenge = base64url(SHA256(verifier))，无填充。"""
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
