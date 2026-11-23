"""antigravity 配额解析与多账号标识单测。

方案 docs/plan-antigravity-provider-integration.md §11.2。
全部为纯函数断言，不发网络。
"""
import pytest

from app.auth.antigravity import quota


# ── 配额：分组摘要 ────────────────────────────────
class TestNormalizeSummary:
    def test_groups_and_buckets(self):
        body = {
            "groups": [
                {
                    "displayName": "Gemini Models",
                    "description": "Models within this group: Gemini 3.8 Flash",
                    "buckets": [
                        {
                            "bucketId": "gemini-weekly",
                            "displayName": "Weekly Limit Remaining",
                            "window": "weekly",
                            "resetTime": "2026-11-28T08:52:58Z",
                            "description": "You have used some of your weekly limit...",
                            "remainingFraction": 0.9542848,
                        },
                        {
                            "bucketId": "gemini-5h",
                            "displayName": "Five Hour Limit Remaining",
                            "window": "5h",
                            "resetTime": "2026-11-21T18:52:58Z",
                            "remainingFraction": 1,
                        },
                    ],
                }
            ],
            "description": "Within each group, models share a weekly limit...",
        }
        out = quota.normalize_summary(body)
        assert len(out["groups"]) == 1
        group = out["groups"][0]
        assert group["display_name"] == "Gemini Models"
        assert len(group["buckets"]) == 2
        weekly = group["buckets"][0]
        assert weekly["bucket_id"] == "gemini-weekly"
        assert weekly["window"] == "weekly"
        assert weekly["reset_time"] == "2026-11-28T08:52:58Z"
        # 比例转百分比，保留一位小数
        assert weekly["remaining_percent"] == 95.4
        assert group["buckets"][1]["remaining_percent"] == 100.0
        assert out["description"].startswith("Within each group")

    def test_missing_fraction_is_none(self):
        out = quota.normalize_summary({
            "groups": [{"displayName": "G", "buckets": [{"bucketId": "b"}]}],
        })
        assert out["groups"][0]["buckets"][0]["remaining_percent"] is None

    def test_bad_fraction_types_ignored(self):
        out = quota.normalize_summary({
            "groups": [{"buckets": [{"bucketId": "b", "remainingFraction": "oops"},
                                    {"bucketId": "c", "remainingFraction": True}]}],
        })
        buckets = out["groups"][0]["buckets"]
        assert buckets[0]["remaining_percent"] is None
        assert buckets[1]["remaining_percent"] is None

    @pytest.mark.parametrize("body", [None, {}, {"groups": None}, {"groups": []}])
    def test_empty_payload(self, body):
        out = quota.normalize_summary(body)
        assert out["groups"] == []

    def test_malformed_group_entries_skipped(self):
        out = quota.normalize_summary({"groups": ["x", {"buckets": ["y", {"bucketId": "ok"}]}]})
        assert len(out["groups"]) == 1
        assert out["groups"][0]["buckets"] == [{
            "bucket_id": "ok", "display_name": "", "window": "",
            "reset_time": None, "description": None, "remaining_percent": None,
        }]


# ── 配额：逐模型明细 ──────────────────────────────
class TestNormalizeModels:
    def test_models_sorted_and_converted(self):
        out = quota.normalize_models({
            "buckets": [
                {"modelId": "gemini-3-flash", "tokenType": "WTUS",
                 "resetTime": "2026-11-21T18:52:58Z", "remainingFraction": 1},
                {"modelId": "claude-opus-5-5-high", "tokenType": "WTUS",
                 "resetTime": "2026-11-21T18:34:03Z", "remainingFraction": 0.9992},
            ],
        })
        # 按 model_id 升序，保证界面顺序稳定
        assert [m["model_id"] for m in out] == ["claude-opus-5-5-high", "gemini-3-flash"]
        assert out[0]["remaining_percent"] == 99.9
        assert out[1]["remaining_percent"] == 100.0
        assert out[0]["token_type"] == "WTUS"

    def test_bucket_without_model_id_dropped(self):
        out = quota.normalize_models({"buckets": [{"remainingFraction": 0.5}, {"modelId": "m"}]})
        assert [m["model_id"] for m in out] == ["m"]

    @pytest.mark.parametrize("body", [None, {}, {"buckets": None}])
    def test_empty_payload(self, body):
        assert quota.normalize_models(body) == []


# ── 配额：缓存与错误类型 ───────────────────────────
class TestQuotaHelpers:
    def test_fingerprint_changes_with_token(self):
        a = quota._fingerprint("token-aaaabbbb")
        b = quota._fingerprint("token-ccccdddd")
        assert a != b
        # 指纹不落完整明文
        assert "token-aaaabbbb" not in a
        assert quota._fingerprint("") == ""

    def test_clear_cache(self):
        try:
            quota._cache[(42, 1)] = ("fp1", 0.0, {"summary": {}})
            quota._cache[(42, 2)] = ("fp2", 0.0, {"summary": {}})
            quota._cache[(42, None)] = ("fp0", 0.0, {"summary": {}})
            quota._cache[42] = ("fp-legacy", 0.0, {"summary": {}})  # type: ignore[index]
            quota._cache[(99, 1)] = ("fp99", 0.0, {"summary": {}})

            # 指定 credential_id 仅清对应条目
            quota.clear_cache(42, 1)
            assert (42, 1) not in quota._cache
            assert (42, 2) in quota._cache
            assert (42, None) in quota._cache
            assert 42 in quota._cache
            assert (99, 1) in quota._cache

            # 未传 credential_id 时清理该 provider 下全部缓存（含兼容裸 int key）
            quota.clear_cache(42)
            assert (42, 2) not in quota._cache
            assert (42, None) not in quota._cache
            assert 42 not in quota._cache
            assert (99, 1) in quota._cache
        finally:
            quota.clear_cache(42)
            quota.clear_cache(99)

    def test_error_carries_kind(self):
        err = quota.AntigravityQuotaError("boom", "network")
        assert err.kind == "network"
        assert str(err) == "boom"

    def test_as_float_rejects_bool(self):
        assert quota._as_float(True) is None
        assert quota._as_float(0.5) == 0.5
        assert quota._as_float("1.5") == 1.5
        assert quota._as_float(None) is None


# ── 多账号：账号标识 ──────────────────────────────
class TestAccountKey:
    def test_prefers_sub_over_email(self):
        from app.auth.antigravity.session import _account_key

        assert _account_key({"sub": "102829547972273838787", "email": "a@b.com"}) \
            == "102829547972273838787"

    def test_falls_back_to_email(self):
        from app.auth.antigravity.session import _account_key

        assert _account_key({"email": "a@b.com"}) == "a@b.com"

    @pytest.mark.parametrize("account", [None, {}, {"sub": "", "email": ""}])
    def test_no_identity(self, account):
        from app.auth.antigravity.session import _account_key

        assert _account_key(account) == ""

    def test_same_account_same_key(self):
        """同账号两次登录必须得到同一标识，否则会产生重复账号。"""
        from app.auth.antigravity.session import _account_key

        first = {"sub": "u1", "email": "a@b.com", "label": "a@b.com"}
        second = {"sub": "u1", "email": "a@b.com", "label": "A"}  # 标签不同但身份相同
        assert _account_key(first) == _account_key(second)

    def test_different_accounts_differ(self):
        from app.auth.antigravity.session import _account_key

        assert _account_key({"sub": "u1"}) != _account_key({"sub": "u2"})


# ── 多账号：配额缓存隔离 ──────────────────────────
class TestMultiAccountQuotaCache:
    async def test_cache_isolated_by_credential_id(self, monkeypatch):
        """同一 provider_id 下不同 credential_id 不会串缓存，且 clear_cache 支持精准与全量清理。"""
        import types

        from app.auth.antigravity import session as ag_session

        pid = 1001
        quota.clear_cache(pid)

        accounts = {
            None: types.SimpleNamespace(
                access_token="tok-default-0", project="p0", tier="free-tier",
                account={"email": "default@example.com"},
            ),
            1: types.SimpleNamespace(
                access_token="tok-account-1", project="p1", tier="free-tier",
                account={"email": "one@example.com"},
            ),
            2: types.SimpleNamespace(
                access_token="tok-account-2", project="p2", tier="pro-tier",
                account={"email": "two@example.com"},
            ),
        }
        rpc_calls: list[str] = []

        async def fake_resolve(db, provider_id, credential_id=None):
            assert provider_id == pid
            return accounts[credential_id]

        async def fake_post_rpc(url, access_token, project, client_kwargs):
            rpc_calls.append(f"{access_token}:{url.rsplit(':', 1)[-1]}")
            frac_map = {
                "tok-default-0": 0.9,
                "tok-account-1": 0.8,
                "tok-account-2": 0.3,
            }
            frac = frac_map[access_token]
            if url.endswith("retrieveUserQuotaSummary"):
                return {
                    "groups": [{
                        "displayName": "Gemini Models",
                        "description": "Models within this group: Gemini",
                        "buckets": [{
                            "bucketId": "gemini-5h",
                            "displayName": "5h",
                            "window": "5h",
                            "remainingFraction": frac,
                        }],
                    }],
                }
            return {
                "buckets": [{
                    "modelId": "gemini-3-flash",
                    "tokenType": "WTUS",
                    "remainingFraction": frac,
                }],
            }

        monkeypatch.setattr(ag_session, "resolve_auth", fake_resolve)
        monkeypatch.setattr(quota, "_post_rpc", fake_post_rpc)

        try:
            d1 = await quota.get_quota(None, pid, None, credential_id=1)
            d2 = await quota.get_quota(None, pid, None, credential_id=2)
            d0 = await quota.get_quota(None, pid, None, credential_id=None)

            assert d1["account"] == "one@example.com"
            assert d2["account"] == "two@example.com"
            assert d0["account"] == "default@example.com"
            assert d1["pools"][0]["remaining_percent"] == 80.0
            assert d2["pools"][0]["remaining_percent"] == 30.0
            assert d0["pools"][0]["remaining_percent"] == 90.0
            # 每个账号各自发了一次 summary + 一次 model 请求，互不命中彼此缓存
            assert len(rpc_calls) == 6

            # 再次读取全部走缓存，不新增 RPC
            assert (await quota.get_quota(None, pid, None, credential_id=1))["account"] == "one@example.com"
            assert (await quota.get_quota(None, pid, None, credential_id=2))["account"] == "two@example.com"
            assert (await quota.get_quota(None, pid, None, credential_id=None))["account"] == "default@example.com"
            assert len(rpc_calls) == 6

            # 精准清理 credential_id=1：仅 1 重建缓存，2 和 None 保持命中
            quota.clear_cache(pid, 1)
            await quota.get_quota(None, pid, None, credential_id=1)
            assert len(rpc_calls) == 8
            await quota.get_quota(None, pid, None, credential_id=2)
            await quota.get_quota(None, pid, None, credential_id=None)
            assert len(rpc_calls) == 8

            # 全量清理 provider：2 和 None 均失效并重新请求
            quota.clear_cache(pid)
            await quota.get_quota(None, pid, None, credential_id=2)
            await quota.get_quota(None, pid, None, credential_id=None)
            assert len(rpc_calls) == 12
        finally:
            quota.clear_cache(pid)


# ── Schema 与路由透出验证 ─────────────────────────
class TestQuotaSchemaAndRoute:
    def test_schema_accepts_pools_and_model_windows_with_loose_defaults(self):
        from app.gateway.schemas import AntigravityQuotaOut

        full = AntigravityQuotaOut(
            ok=True,
            summary={"groups": []},
            models=[{"model_id": "gemini-3-flash", "remaining_percent": 90.0}],
            pools=[{
                "name": "5 小时",
                "pool": "Gemini",
                "used_percent": 10.0,
                "remaining_percent": 90.0,
                "reset_time": "2026-11-21T18:00:00Z",
                "aside": True,
                "span_seconds": 18000,
            }],
            model_windows=[{
                "name": "Gemini 3 Flash",
                "model_id": "gemini-3-flash",
                "vendor": "Gemini",
                "used_percent": 10.0,
                "remaining_percent": 90.0,
                "reset_time": "2026-11-21T18:00:00Z",
                "pool": "Gemini",
                "span_seconds": 18000,
            }],
            project="proj-1",
            tier="free-tier",
            account="a@b.com",
        )
        dumped = full.model_dump()
        assert dumped["pools"][0]["pool"] == "Gemini"
        assert dumped["pools"][0]["span_seconds"] == 18000
        assert dumped["model_windows"][0]["model_id"] == "gemini-3-flash"
        assert dumped["model_windows"][0]["pool"] == "Gemini"

        # 缺省可选字段（如未算出 pool 或缺少 span_seconds）不应触发校验错误
        minimal = AntigravityQuotaOut(
            pools=[{"name": "自定义", "pool": "Other", "used_percent": 50.0}],
            model_windows=[{"name": "Model X", "model_id": "model-x"}],
        )
        assert minimal.pools[0].aside is True
        assert minimal.pools[0].span_seconds is None
        assert minimal.model_windows[0].pool == ""
        assert minimal.model_windows[0].span_seconds is None

        err_out = AntigravityQuotaOut(ok=False, error="not logged in")
        assert err_out.pools == []
        assert err_out.model_windows == []

    async def test_route_passes_credential_id_and_serializes_fields(self, monkeypatch):
        import types

        import httpx
        from fastapi import FastAPI

        from app.auth.antigravity import quota as ag_quota
        from app.gateway.routers import antigravity_auth
        from app.persistence.database import get_db
        from app.services import provider_service

        app = FastAPI()
        app.include_router(antigravity_auth.router, prefix="/api")

        async def override_get_db():
            yield None

        app.dependency_overrides[get_db] = override_get_db

        fake_provider = types.SimpleNamespace(
            id=7, api_format="antigravity", base_url=None,
            proxy_mode="direct", proxy_url=None,
        )

        async def fake_get_provider(db, provider_id):
            assert provider_id == 7
            return fake_provider

        seen_calls: list[dict] = []

        async def fake_get_quota(db, provider_id, provider=None, *,
                                 force=False, credential_id=None, client_kwargs=None):
            seen_calls.append({
                "provider_id": provider_id,
                "force": force,
                "credential_id": credential_id,
            })
            return {
                "summary": {"groups": []},
                "models": [{
                    "model_id": "gemini-3-flash",
                    "token_type": "WTUS",
                    "reset_time": "2026-11-21T18:00:00Z",
                    "remaining_percent": 75.0,
                }],
                "pools": [{
                    "name": "5 小时",
                    "pool": "Gemini",
                    "used_percent": 25.0,
                    "remaining_percent": 75.0,
                    "reset_time": "2026-11-21T18:00:00Z",
                    "aside": True,
                    "span_seconds": 18000,
                }],
                "model_windows": [{
                    "name": "Gemini 3 Flash",
                    "model_id": "gemini-3-flash",
                    "vendor": "Gemini",
                    "used_percent": 25.0,
                    "remaining_percent": 75.0,
                    "reset_time": "2026-11-21T18:00:00Z",
                    "pool": "Gemini",
                    "span_seconds": 18000,
                }],
                "project": "proj-7",
                "tier": "pro-tier",
                "account": f"cred-{credential_id}@example.com",
            }

        monkeypatch.setattr(provider_service, "get_provider", fake_get_provider)
        monkeypatch.setattr(ag_quota, "get_quota", fake_get_quota)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp_default = await client.get("/api/providers/7/antigravity/quota")
            assert resp_default.status_code == 200
            body_default = resp_default.json()
            assert body_default["ok"] is True
            assert body_default["account"] == "cred-None@example.com"
            assert body_default["pools"] == [{
                "name": "5 小时",
                "pool": "Gemini",
                "used_percent": 25.0,
                "remaining_percent": 75.0,
                "reset_time": "2026-11-21T18:00:00Z",
                "aside": True,
                "span_seconds": 18000,
            }]
            assert body_default["model_windows"] == [{
                "name": "Gemini 3 Flash",
                "model_id": "gemini-3-flash",
                "vendor": "Gemini",
                "used_percent": 25.0,
                "remaining_percent": 75.0,
                "reset_time": "2026-11-21T18:00:00Z",
                "pool": "Gemini",
                "span_seconds": 18000,
            }]

            resp_cred = await client.get(
                "/api/providers/7/antigravity/quota",
                params={"credential_id": 88, "force": "true"},
            )
            assert resp_cred.status_code == 200
            body_cred = resp_cred.json()
            assert body_cred["ok"] is True
            assert body_cred["account"] == "cred-88@example.com"
            assert len(body_cred["pools"]) == 1
            assert len(body_cred["model_windows"]) == 1

        assert seen_calls == [
            {"provider_id": 7, "force": False, "credential_id": None},
            {"provider_id": 7, "force": True, "credential_id": 88},
        ]

    async def test_route_end_to_end_multi_account_isolation(self, monkeypatch):
        """路由 → get_quota → build_pools → AntigravityQuotaOut 全链路多账号隔离验证。"""
        import types

        import httpx
        from fastapi import FastAPI

        from app.auth.antigravity import session as ag_session
        from app.gateway.routers import antigravity_auth
        from app.persistence.database import get_db
        from app.services import provider_service

        pid = 2002
        quota.clear_cache(pid)

        app = FastAPI()
        app.include_router(antigravity_auth.router, prefix="/api")

        async def override_get_db():
            yield None

        app.dependency_overrides[get_db] = override_get_db

        fake_provider = types.SimpleNamespace(
            id=pid, api_format="antigravity", base_url=None,
            proxy_mode="direct", proxy_url=None,
        )

        async def fake_get_provider(db, provider_id):
            assert provider_id == pid
            return fake_provider

        rows = {
            11: types.SimpleNamespace(
                access_token="tok-11", project="p-11", tier="free-tier",
                account={"email": "acct11@example.com"},
            ),
            22: types.SimpleNamespace(
                access_token="tok-22", project="p-22", tier="pro-tier",
                account={"email": "acct22@example.com"},
            ),
        }

        async def fake_resolve(db, provider_id, credential_id=None):
            return rows.get(credential_id)

        async def fake_post_rpc(url, access_token, project, client_kwargs):
            frac = 0.85 if access_token == "tok-11" else 0.40
            if url.endswith("retrieveUserQuotaSummary"):
                return {
                    "groups": [{
                        "displayName": "Gemini Models",
                        "description": "Models within this group: Gemini",
                        "buckets": [{
                            "bucketId": "gemini-5h",
                            "displayName": "5h",
                            "window": "5h",
                            "resetTime": "2026-11-21T18:00:00Z",
                            "remainingFraction": frac,
                        }],
                    }],
                }
            return {
                "buckets": [{
                    "modelId": "gemini-3-flash",
                    "tokenType": "WTUS",
                    "resetTime": "2026-11-21T18:00:00Z",
                    "remainingFraction": frac,
                }],
            }

        monkeypatch.setattr(provider_service, "get_provider", fake_get_provider)
        monkeypatch.setattr(ag_session, "resolve_auth", fake_resolve)
        monkeypatch.setattr(quota, "_post_rpc", fake_post_rpc)

        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test",
            ) as client:
                r1 = await client.get(
                    f"/api/providers/{pid}/antigravity/quota",
                    params={"credential_id": 11},
                )
                r2 = await client.get(
                    f"/api/providers/{pid}/antigravity/quota",
                    params={"credential_id": 22},
                )

            assert r1.status_code == 200 and r2.status_code == 200
            j1, j2 = r1.json(), r2.json()
            assert j1["account"] == "acct11@example.com"
            assert j2["account"] == "acct22@example.com"
            assert j1["pools"][0]["remaining_percent"] == 85.0
            assert j2["pools"][0]["remaining_percent"] == 40.0
            assert j1["model_windows"][0]["pool"] == "Gemini"
            assert j2["model_windows"][0]["remaining_percent"] == 40.0
        finally:
            quota.clear_cache(pid)
