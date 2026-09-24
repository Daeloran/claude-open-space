"""Tests d'intention pour #1 : contrôle de l'origine des connexions WebSocket."""
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import backend.app as app_mod
from backend.app import app

EVIL = "https://evil.example"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("OPENSPACE_ALLOWED_ORIGINS", raising=False)
    return TestClient(app)  # pas de `with` : le lifespan (vrais employés) ne démarre pas


def receive_hello(client, headers):
    with client.websocket_connect("/ws", headers=headers) as ws:
        return ws.receive_json()


def assert_refused(client, headers):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers=headers) as ws:
            ws.receive_json()


def test_local_origin_matching_host_gets_hello(client):
    assert receive_hello(client, {"origin": "http://testserver"})["type"] == "hello"


@pytest.mark.parametrize("origin", ["http://127.0.0.1:8000", "http://localhost:8000"])
def test_loopback_origin_on_server_port_gets_hello(client, origin):
    host = origin.removeprefix("http://")
    assert receive_hello(client, {"host": host, "origin": origin})["type"] == "hello"


def test_foreign_origin_refused(client):
    assert_refused(client, {"origin": EVIL})


def test_loopback_origin_on_other_port_refused(client):
    assert_refused(client, {"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:9999"})


def test_missing_origin_refused(client):
    assert_refused(client, {})


def test_allowed_origins_env_adds_origins(client, monkeypatch):
    monkeypatch.setenv("OPENSPACE_ALLOWED_ORIGINS", "http://proxy.local:3000, http://other.local:4000")
    for origin in ("http://proxy.local:3000", "http://other.local:4000"):
        assert receive_hello(client, {"origin": origin})["type"] == "hello"
    assert_refused(client, {"origin": EVIL})


def test_foreign_origin_new_ticket_creates_nothing(client, monkeypatch, tmp_path):
    sessions = []

    class FakeClient:  # par sécurité : aucune vraie session Claude
        def __init__(self, *a, **k):
            sessions.append(self)

    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", tmp_path)
    title = f"pwned-{uuid.uuid4().hex}"
    try:
        with client.websocket_connect("/ws", headers={"origin": EVIL}) as ws:
            ws.send_json({"type": "new_ticket", "title": title, "cwd": str(tmp_path)})
            ws.receive_json()
    except WebSocketDisconnect:
        pass

    with client.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        while (ev := ws.receive_json())["type"] != "snapshot":
            pass
    assert not [t for t in ev["tickets"] if t["title"] == title]
    assert not [a for a in ev["agents"] if a["cwd"] == str(tmp_path)]
    assert not sessions


def test_foreign_origin_permission_decision_is_refused(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"origin": EVIL}) as ws:
            ws.send_json({"type": "permission_decision", "request_id": "x", "allow": True})
            ws.receive_json()
