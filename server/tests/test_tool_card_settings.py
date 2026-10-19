"""会话工具卡设置（plan-20261019-toolcard-v2）：默认形态与逐栏默认展开的读写、取值夹紧、旧配置兼容。"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import settings as app_settings
from app.gateway.routers import settings as settings_router


@pytest.fixture
def client(monkeypatch):
    saved: dict = {}
    monkeypatch.setattr(settings_router, "_load_config", lambda: dict(saved))
    monkeypatch.setattr(settings_router, "_save_config", lambda data: saved.update(data))
    monkeypatch.setattr(app_settings, "tool_card_default_form", "capsule")
    monkeypatch.setattr(app_settings, "tool_card_section_defaults", {})
    monkeypatch.setattr(app_settings, "tool_card_sections_expanded", True)
    app = FastAPI()
    app.include_router(settings_router.router, prefix="/api")
    return TestClient(app), saved


def test_defaults_keep_capsule_and_legacy_flag(client):
    c, _ = client
    body = c.get("/api/settings/global").json()
    assert body["tool_card_default_form"] == "capsule"
    assert body["tool_card_section_defaults"] == {}
    assert body["tool_card_sections_expanded"] is True  # 旧布尔仍在，兼容老版本前端


def test_put_persists_form_and_sections(client):
    c, saved = client
    r = c.put("/api/settings/global", json={
        "tool_card_default_form": "expanded",
        "tool_card_section_defaults": {"git": False, "process": True},
    })
    assert r.status_code == 200
    assert saved["tool_card_default_form"] == "expanded"
    assert saved["tool_card_section_defaults"] == {"git": False, "process": True}
    body = c.get("/api/settings/global").json()
    assert body["tool_card_default_form"] == "expanded"
    assert body["tool_card_section_defaults"] == {"git": False, "process": True}


def test_values_are_clamped(client):
    c, saved = client
    c.put("/api/settings/global", json={
        "tool_card_default_form": "fullscreen",  # 非法形态回落 capsule
        "tool_card_section_defaults": {"git": True, "unknown": True},  # 未知栏目丢弃
    })
    assert saved["tool_card_default_form"] == "capsule"
    assert saved["tool_card_section_defaults"] == {"git": True}


def test_partial_update_keeps_other_field(client):
    c, _ = client
    c.put("/api/settings/global", json={"tool_card_default_form": "expanded"})
    c.put("/api/settings/global", json={"tool_card_section_defaults": {"plan": False}})
    body = c.get("/api/settings/global").json()
    assert body["tool_card_default_form"] == "expanded"
    assert body["tool_card_section_defaults"] == {"plan": False}
