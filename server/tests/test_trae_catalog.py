"""TRAE 目录同步单测：batch_get_detail_param 整数枚举请求体、function_configs 解析。"""
import json

import httpx
import pytest

from app.auth.trae.catalog import (
    TraeCatalogUnauthorized,
    _detail_param_body,
    _extract_function_groups,
    _is_chat_function,
    _parse_entry,
    fetch_catalog_raw,
)

# 实测响应结构（2026-08-25）
SAMPLE_FUNCTION_CONFIG = {
    "function": "solo_agent_lite",
    "config_info_list": [
        {
            "config_name": "Doubao-Seed-Evolving",
            "config_source": 1,
            "config_switch": True,
            "display_config": {
                "display_name": "Seed-Evolving",
                "multimodal": True,
                "model_capability": "reasoning_model",
                "max_mode": True,
                "is_custom_model": False,
            },
            "extra_config": "{\"v3_max_mode_enabled\":true}",
            "is_invisible_to_user": False,
            "model_detail_list": [
                {"model_name": "Doubao-Seed-Evolving", "prompt_max_tokens": 30000,
                 "max_tokens": 16000, "model_extra_config": "{}"},
                {"model_name": "Doubao-Seed-Evolving", "prompt_max_tokens": 1000000,
                 "max_tokens": 32768, "model_extra_config": "{}"},
            ],
        },
        {
            "config_name": "Doubao-Seed-2.1-Pro",
            "display_config": {"display_name": "Doubao-Seed-2.1-Pro", "multimodal": False},
            "model_detail_list": [],
        },
    ],
}


class TestDetailParamBody:
    def test_default_body_uses_integer_enums(self):
        """mode_type/access_type 必须为整数（字符串会 400，实测验证）。"""
        body = _detail_param_body()
        assert "solo_agent_lite" in body["functions"]
        assert isinstance(body["mode_type"], int)
        assert isinstance(body["access_type"], int)
        assert body["mode_type"] == 0
        assert body["access_type"] == 0
        assert body["show_custom_model"] is True

    def test_custom_functions(self):
        body = _detail_param_body(["assistant"])
        assert body["functions"] == ["assistant"]


class TestExtractFunctionGroups:
    def test_function_configs(self):
        payload = {"function_configs": [SAMPLE_FUNCTION_CONFIG]}
        groups = _extract_function_groups(payload)
        assert len(groups) == 1
        func, items = groups[0]
        assert func == "solo_agent_lite"
        assert len(items) == 2
        assert items[0]["config_name"] == "Doubao-Seed-Evolving"

    def test_envelope_unwrap(self):
        payload = {"data": {"function_configs": [SAMPLE_FUNCTION_CONFIG]}}
        groups = _extract_function_groups(payload)
        assert len(groups) == 1

    def test_empty_returns_empty(self):
        assert _extract_function_groups({}) == []
        assert _extract_function_groups({"function_configs": []}) == []
        assert _extract_function_groups(None) == []


class TestChatFunction:
    def test_chat_functions(self):
        assert _is_chat_function("solo_agent_lite") is True
        assert _is_chat_function("assistant") is True
        assert _is_chat_function("builder") is True
        assert _is_chat_function("solo_coder") is True

    def test_non_chat_functions(self):
        assert _is_chat_function("solo_design_lite") is False
        assert _is_chat_function("solo_work_remote") is False
        assert _is_chat_function("") is False


class TestEntryMapping:
    def test_parse_entry_full(self):
        entry = _parse_entry(SAMPLE_FUNCTION_CONFIG["config_info_list"][0], 0)
        assert entry is not None
        assert entry["name"] == "Doubao-Seed-Evolving"
        assert entry["config_name"] == "Doubao-Seed-Evolving"
        assert entry["title"] == "Seed-Evolving"
        assert entry["is_multimodal"] is True
        assert entry["max_mode"] is True
        # context_window 取第一个档位
        assert entry["context_window"] == 30000
        assert entry["max_output_tokens"] == 16000
        # 请求 model_name 用纯配置名；底层 model_detail_list.model_name 单独存
        # provider_model_name（原样保留，可能带 __dev，不参与请求构造）
        assert entry["model_name"] == "Doubao-Seed-Evolving"
        assert entry["provider_model_name"] == "Doubao-Seed-Evolving"
        assert entry["extra_config"] == {"v3_max_mode_enabled": True}
        # 可用模型白名单（Doubao-Seed-Evolving 在客户端可用列表中）
        assert entry["is_available"] is True

    def test_parse_entry_keeps_dev_suffix_in_provider_model_name(self):
        # 目录 model_detail_list[0].model_name 可能带 volcengine dev 后缀：
        # 必须原样存 provider_model_name，且不能污染请求用 model_name。
        item = {
            "config_name": "DeepSeek-V4-Flash-Official",
            "display_config": {"display_name": "DeepSeek-V4-Flash 正式版"},
            "model_detail_list": [
                {"model_name": "DeepSeek-V4-Flash-Official__dev",
                 "prompt_max_tokens": 168000, "max_tokens": 32000},
            ],
        }
        entry = _parse_entry(item, 0)
        assert entry is not None
        assert entry["model_name"] == "DeepSeek-V4-Flash-Official"
        assert entry["provider_model_name"] == "DeepSeek-V4-Flash-Official__dev"
        assert entry["context_window"] == 168000
        assert entry["max_output_tokens"] == 32000

    def test_parse_entry_missing_name(self):
        assert _parse_entry({"display_config": {"display_name": "x"}}, 0) is None

    def test_parse_entry_bad_extra_config(self):
        entry = _parse_entry({"config_name": "m1", "extra_config": "not-json"}, 0)
        assert entry is not None
        assert entry["extra_config"] is None

    def test_parse_reasoning_and_max_context(self):
        # 实测结构：reasoning_effort_config.options 归一化 + context_window_tokens.max + 消耗倍率
        item = {
            "config_name": "DeepSeek-V4-Flash-Official",
            "display_config": {"display_name": "DeepSeek-V4-Flash 正式版",
                               "max_mode": True, "multimodal": False},
            "context_window_tokens": {"dev": 200000, "max": 1000000},
            "reasoning_effort_config": {
                "default_level": "high", "options": ["light", "high", "extra_high"],
                "support_thinking": True,
            },
            "display_contact_config": json.dumps({
                "access": {"data": {"identity_list": [0, 5, 1]}},
                "consumption_rate": {"enable": True, "data": {"rate": 0.72}},
            }, ensure_ascii=False),
            "model_detail_list": [{"prompt_max_tokens": 168000, "max_tokens": 32000}],
        }
        entry = _parse_entry(item, 0)
        assert entry["reasoning_options"] == ["low", "high", "xhigh"]  # light→low, extra_high→xhigh
        assert entry["thinking"] is True
        assert entry["context_window_max"] == 1000000  # 1M 上下文
        assert entry["consumption_rate"] == 0.72       # 积分消耗倍率
        assert entry["is_available"] is True           # 在客户端可用白名单

    def test_parse_reasoning_max_appended_on_max_mode(self):
        item = {
            "config_name": "kimi-k3",
            "display_config": {"max_mode": True},
            "reasoning_effort_config": {"options": ["light", "high", "extra_high"]},
        }
        entry = _parse_entry(item, 0)
        assert entry["reasoning_options"] == ["low", "high", "xhigh"]
        assert entry["max_mode"] is True

    def test_max_mode_without_thinking_keeps_no_reasoning(self):
        # max_mode=True 但无 reasoning_effort_config（如 DeepSeek-V4-Flash）：
        # 客户端不显示思考档位，max 不混入 options
        item = {
            "config_name": "DeepSeek-V4-Flash",
            "display_config": {"max_mode": True},
        }
        entry = _parse_entry(item, 0)
        assert entry["reasoning_options"] == []
        assert entry["max_mode"] is True

    def test_parse_contact_rate_invalid(self):
        entry = _parse_entry(
            {"config_name": "m1", "display_contact_config": "not-json"}, 0)
        assert entry["consumption_rate"] is None


class TestFetchCatalog:
    def test_fetch_ok(self, monkeypatch):
        async def fake_post(self, url, **kw):
            assert "Cloud-IDE-JWT t" in kw["headers"]["Authorization"]
            assert kw["json"]["access_type"] == 0  # 整数枚举
            # 必须带 x-ide-version-code（缺头时服务端不返回内置模型目录，实测）
            assert kw["headers"]["x-ide-version-code"]
            return httpx.Response(200, json={"function_configs": [SAMPLE_FUNCTION_CONFIG]})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        payload = asyncio_run(fetch_catalog_raw(
            "https://trae-api-cn.mchost.guru", "t",
            {"device_id": "d", "machine_id": "m"}))
        assert payload["function_configs"]

    def test_fetch_401_raises_unauthorized(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(401, json={})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(TraeCatalogUnauthorized):
            asyncio_run(fetch_catalog_raw("https://trae-api-cn.mchost.guru", "t", {}))

    def test_fetch_400_raises(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(400, content=b"")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(RuntimeError) as exc:
            asyncio_run(fetch_catalog_raw("https://trae-api-cn.mchost.guru", "t", {}))
        assert "http_400" in str(exc.value)

    def test_fetch_non_json(self, monkeypatch):
        async def fake_post(self, url, **kw):
            return httpx.Response(200, text="<html>")

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with pytest.raises(RuntimeError):
            asyncio_run(fetch_catalog_raw("https://trae-api-cn.mchost.guru", "t", {}))


class TestBuiltinFilter:
    def test_custom_model_excluded(self):
        from app.auth.trae.catalog import _is_builtin_model
        assert _is_builtin_model({"is_custom_model": False}) is True
        assert _is_builtin_model({"is_custom_model": True}) is False
        assert _is_builtin_model({}) is True


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
