"""workbuddy 目录同步单测：/v3/config 解析、对话模型过滤、条目推导。"""
from app.auth.workbuddy.catalog import _config_headers, _keep_model, _parse_entry

# 对齐方案 §1.4 实测的 /v3/config 结构（精简样例）
CONFIG_SAMPLE = {
    "endpoint": "https://copilot.tencent.com",
    "models": [
        {
            "id": "auto", "name": "Auto", "isDefault": True,
            "credits": "x2.00 credits", "vendor": "f",
            "maxInputTokens": 168000, "maxOutputTokens": 32000,
            "supportsToolCall": True, "supportsImages": True,
            "supportsReasoning": True, "onlyReasoning": True,
            "reasoning": {"effort": "high", "summary": "auto"},
            "temperature": 1, "tags": ["craft"],
        },
        {
            "id": "deepseek-v4-pro", "name": "DeepSeek V4 Pro",
            "credits": "x2.00 credits", "vendor": "f",
            "maxInputTokens": 1000000, "maxOutputTokens": 50000,
            "supportsReasoning": True, "reasoning": {"effort": "high", "summary": "auto"},
            "temperature": 1, "tags": ["craft"],
        },
        {
            "id": "completion-gf", "name": "Completion GF",
            "maxInputTokens": 4000, "maxOutputTokens": 2048,
            "tags": ["chat"],
        },
        {
            "id": "custom-local:glm-5.2", "name": "glm-5.2", "vendor": "Custom",
            "maxInputTokens": 512000, "maxOutputTokens": 128000,
            "tags": ["custom"],
        },
    ],
    "agents": [
        {"name": "cli", "models": ["auto", "deepseek-v4-pro", "completion-gf"]},
    ],
}


class TestKeepModel:
    def test_cli_agent_model_kept(self):
        keep = {"auto", "deepseek-v4-pro"}
        assert _keep_model(CONFIG_SAMPLE["models"][0], keep) is True

    def test_craft_tag_model_kept(self):
        # auto 同时带 craft tag，即使不在 cli 列表也保留
        assert _keep_model(CONFIG_SAMPLE["models"][0], set()) is True

    def test_custom_local_excluded(self):
        m = CONFIG_SAMPLE["models"][3]
        assert _keep_model(m, {m["id"]}) is False

    def test_unknown_model_excluded(self):
        keep = set()
        assert _keep_model(CONFIG_SAMPLE["models"][2], keep) is False  # 无 craft tag
        assert _keep_model(CONFIG_SAMPLE["models"][2], {"completion-gf"}) is True


class TestParseEntry:
    def test_full_entry_mapping(self):
        e = _parse_entry(CONFIG_SAMPLE["models"][1])
        assert e["name"] == "deepseek-v4-pro"
        assert e["title"] == "DeepSeek V4 Pro"
        assert e["credits"] == "x2.00 credits"
        assert e["context_window"] == 1000000
        assert e["max_output_tokens"] == 50000
        assert e["is_multimodal"] is False
        assert e["supports_reasoning"] is True
        assert e["reasoning"]["effort"] == "high"
        assert e["temperature"] == 1.0
        assert "high" in e["reasoning_efforts"]

    def test_non_reasoning_model_empty_efforts(self):
        m = {**CONFIG_SAMPLE["models"][2]}
        e = _parse_entry(m)
        assert e["reasoning_efforts"] == []
        assert e["supports_reasoning"] is False

    def test_missing_optional_fields(self):
        e = _parse_entry({"id": "m1"})
        assert e["context_window"] is None
        assert e["temperature"] is None
        assert e["credits"] == ""


class TestConfigHeaders:
    def test_auth_and_identity_headers(self):
        h = _config_headers("tok", "https://copilot.tencent.com", {"uid": "u1"})
        assert h["Authorization"] == "Bearer tok"
        assert h["X-User-Id"] == "u1"
        assert h["X-Domain"] == "copilot.tencent.com"
        assert h["X-Product"] == "SaaS"

    def test_enterprise_headers(self):
        account = {"uid": "u1", "enterpriseId": "e1"}
        h = _config_headers("tok", "https://copilot.tencent.com", account)
        assert h["X-Enterprise-Id"] == "e1"
        assert h["X-Tenant-Id"] == "e1"
