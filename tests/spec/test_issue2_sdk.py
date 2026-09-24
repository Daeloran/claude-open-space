"""Tests d'intention pour l'issue #2 : prototype contre le vrai Agent SDK.

Hors ligne : les types du SDK exposent les champs utilisés par le backend,
et requirements.txt borne la version minimale du SDK.
Live (-m live) : un vrai serveur uvicorn traite des tickets de bout en bout.
"""

import asyncio
import dataclasses
import importlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIVE_TIMEOUT = 180


# --- Hors ligne ------------------------------------------------------------

def _field_names(cls) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


@pytest.mark.parametrize(
    "type_name, attrs",
    [
        ("ResultMessage", {"total_cost_usd", "usage", "is_error", "subtype"}),
        ("AssistantMessage", {"content", "parent_tool_use_id", "usage"}),
        ("UserMessage", {"content", "parent_tool_use_id"}),
        ("SystemMessage", {"subtype", "data"}),
        ("ToolUseBlock", {"id", "name", "input"}),
        ("ToolResultBlock", {"tool_use_id", "content", "is_error"}),
    ],
)
def test_sdk_types_expose_fields_used_by_backend(type_name, attrs):
    import claude_agent_sdk

    cls = getattr(claude_agent_sdk, type_name)
    missing = attrs - _field_names(cls)
    assert not missing, f"{type_name} n'expose pas {missing}"


def test_sdk_permission_and_client_api_importable():
    from claude_agent_sdk import (  # noqa: F401
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
    )

    assert {"cwd", "allowed_tools", "can_use_tool"} <= _field_names(ClaudeAgentOptions)


def test_requirements_pin_minimum_sdk_version():
    lines = (ROOT / "backend" / "requirements.txt").read_text().splitlines()
    sdk = [l.strip() for l in lines if re.match(r"\s*claude[-_]agent[-_]sdk\b", l, re.I)]
    assert sdk, "claude-agent-sdk absent de backend/requirements.txt"
    assert re.search(r"(>=|~=|==)\s*\d+\.\d+", sdk[0]), (
        f"pas de borne minimale de version : {sdk[0]!r}"
    )


def test_permission_mode_follows_env_variable(monkeypatch):
    import backend.app

    # reload remplace hub, app, employees… : on restaure les objets d'origine,
    # que d'autres tests ont importés par référence
    saved = dict(vars(backend.app))
    try:
        monkeypatch.delenv("OPENSPACE_PERMISSION_MODE", raising=False)
        importlib.reload(backend.app)
        assert backend.app.Employee(0, "Test").options.permission_mode is None

        monkeypatch.setenv("OPENSPACE_PERMISSION_MODE", "default")
        importlib.reload(backend.app)
        assert backend.app.Employee(0, "Test").options.permission_mode == "default"
    finally:
        vars(backend.app).update(saved)


def test_readme_no_longer_lists_sdk_field_check():
    readme = (ROOT / "README.md").read_text()
    assert "Vérifier les noms des champs du SDK" not in readme


# --- Live ------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def server(tmp_path):
    workdir = tmp_path / "chantier"
    workdir.mkdir()
    (workdir / "notes.txt").write_text("bonjour\n")
    (workdir / "hello.py").write_text("print('hello')\n")

    port = _free_port()
    env = {
        **os.environ,
        "OPENSPACE_CWD": str(workdir),
        "OPENSPACE_TEAM": "Léa",
        "OPENSPACE_PERMISSION_MODE": "default",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=ROOT,
        env=env,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            assert proc.poll() is None, "uvicorn s'est arrêté au démarrage"
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "uvicorn n'écoute pas après 30 s"
                time.sleep(0.2)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def _run_ticket(port: int, title: str, cwd, on_permission=None) -> list[dict]:
    """Envoie un ticket (destination : dossier `cwd`) et renvoie les événements jusqu'à ticket_done."""
    import websockets

    url = f"ws://127.0.0.1:{port}/ws"
    origin = f"http://127.0.0.1:{port}"
    events: list[dict] = []
    async with websockets.connect(url, additional_headers={"Origin": origin}) as ws:
        await ws.send(json.dumps({"type": "new_ticket", "title": title, "cwd": str(cwd)}))
        async for raw in ws:
            ev = json.loads(raw)
            events.append(ev)
            if ev.get("type") == "permission_request" and on_permission:
                await ws.send(json.dumps(on_permission(ev)))
            if ev.get("type") == "ticket_done":
                return events
    return events


def _types(events):
    return [e.get("type") for e in events]


@pytest.mark.live
async def test_live_simple_ticket_full_sequence(server, tmp_path):
    events = await asyncio.wait_for(
        _run_ticket(server, "Liste les fichiers de ce dossier sans utiliser Bash", tmp_path / "chantier"),
        LIVE_TIMEOUT,
    )
    types = _types(events)
    for expected in ("hello", "agent_hired", "ticket_created", "ticket_assigned", "tool_use", "tool_result", "cost", "ticket_done"):
        assert expected in types, f"{expected} manquant dans {types}"

    assert types.index("agent_hired") < types.index("ticket_created")
    assert types.index("ticket_created") < types.index("ticket_assigned") < types.index("tool_use")
    assert types.index("tool_use") < types.index("tool_result")
    assert types[-1] == "ticket_done"

    costs = [e for e in events if e["type"] == "cost"]
    assert any((e.get("usd") or 0) > 0 for e in costs), f"aucun coût > 0 : {costs}"
    assert events[-1].get("ok") is True, events[-1]


@pytest.mark.live
async def test_live_bash_mkdir_asks_permission_and_respects_denial(server, tmp_path):
    requests = []

    def deny(ev):
        requests.append(ev)
        return {"type": "permission_decision", "request_id": ev["request_id"], "allow": False}

    events = await asyncio.wait_for(
        _run_ticket(
            server,
            "Crée un dossier nommé nouveau_dossier avec la commande Bash mkdir",
            tmp_path / "chantier",
            on_permission=deny,
        ),
        LIVE_TIMEOUT,
    )
    types = _types(events)
    assert any(r.get("tool") == "Bash" for r in requests), (
        f"aucun permission_request Bash reçu : {requests} / {types}"
    )
    assert "agent_hired" in types and types.index("agent_hired") < types.index("ticket_created"), types
    assert types[-1] == "ticket_done", types
    assert not (tmp_path / "chantier" / "nouveau_dossier").exists()
