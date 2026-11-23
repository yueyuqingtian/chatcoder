"""队列消息设置（plan-20261120-queue-message-mode）：queue_message_mode 读写、取值夹紧与部分更新保持。"""
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
    monkeypatch.setattr(app_settings, "queue_message_mode", "queue")
    app = FastAPI()
    app.include_router(settings_router.router, prefix="/api")
    return TestClient(app), saved


def test_default_queue_message_mode_is_queue(client):
    c, _ = client
    body = c.get("/api/settings/global").json()
    assert body["queue_message_mode"] == "queue"


def test_put_persists_immediate_and_queue(client):
    c, saved = client
    r = c.put("/api/settings/global", json={"queue_message_mode": "immediate"})
    assert r.status_code == 200
    assert saved["queue_message_mode"] == "immediate"
    assert r.json()["queue_message_mode"] == "immediate"
    assert c.get("/api/settings/global").json()["queue_message_mode"] == "immediate"

    r2 = c.put("/api/settings/global", json={"queue_message_mode": "queue"})
    assert r2.status_code == 200
    assert saved["queue_message_mode"] == "queue"
    assert c.get("/api/settings/global").json()["queue_message_mode"] == "queue"


def test_invalid_queue_message_mode_falls_back_to_queue(client):
    c, saved = client
    r = c.put("/api/settings/global", json={"queue_message_mode": "unknown_mode"})
    assert r.status_code == 200
    assert saved["queue_message_mode"] == "queue"
    assert r.json()["queue_message_mode"] == "queue"
