"""Tests d'intention pour l'issue #38 : mode de permission par employé piloté.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; l'affichage (ligne d'équipe, en-tête du chat) est vérifié à la main.

Contrat public supposé :
- WS `{"type": "set_mode", "agent_id": A, "mode": M}` sur un employé piloté dont la session est connectée
  → `await client.set_permission_mode(M)` puis diffusion de `{"type": "mode_changed", "agent_id": A, "mode": M}`.
  (Sans session encore connectée, le mode est mémorisé et appliqué à la connexion via
  `ClaudeAgentOptions.permission_mode` — non testé ici.)
- Modes acceptés : "default", "acceptEdits", "plan", "bypassPermissions", "auto".
  Mode inconnu → `{"type": "mode_rejected", "agent_id", "reason"}` à l'émetteur, SDK non appelé, pas de mode_changed.
- Employé terminal (« o-xxxxxxxx ») → `mode_rejected`.
- `hello.team[*]` et `snapshot.agents[*]` des employés pilotés portent la clé `mode` (None possible au départ).
- ExitPlanMode via `can_use_tool` : `permission_request` porte `plan` (chaîne de l'input) ;
  allow → PermissionResultAllow ; deny → PermissionResultDeny avec un message non vide.
- Le mode survit d'un ticket à l'autre : aucun `set_permission_mode` avec un autre mode, et une éventuelle
  nouvelle session est créée avec `options.permission_mode` = mode choisi.

Harnais : faux ClaudeSDKClient de test_issue36_interrupt, étendu avec `set_permission_mode`.
"""
import uuid

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until
from tests.spec.test_issue33_ask_desk import result, start
from tests.spec.test_issue36_interrupt import INSTANCES, FakeClient, hire, is_


class ModeClient(FakeClient):
    def __init__(self, options):
        super().__init__(options)
        self.modes: list[str] = []

    async def set_permission_mode(self, mode):
        self.modes.append(mode)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-config"
    (cfg / "projects").mkdir(parents=True)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", cfg)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", ModeClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        yield c
        c.portal = None


def ready(ws, tmp_path):
    """Employé piloté avec une session connectée et inactive ; renvoie (agent_id, session)."""
    aid, tid, session = hire(ws, tmp_path, f"rapide-{uuid.uuid4().hex}")
    recv_until(ws, is_("ticket_done", ticket_id=tid))
    drain(ws)
    return aid, session


def agent_in(items, aid):
    (a,) = [a for a in items if a["id"] == aid]
    return a


# ---------------------------------------------------------------- set_mode


def test_set_mode_plan_appelle_le_sdk_et_diffuse_mode_changed(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        ws.send_json({"type": "set_mode", "agent_id": aid, "mode": "plan"})
        ev = recv_until(ws, is_("mode_changed", agent_id=aid))
    assert ev["mode"] == "plan"
    assert session.modes == ["plan"]


@pytest.mark.parametrize("mode", ["default", "acceptEdits", "bypassPermissions", "auto"])
def test_modes_connus_acceptes(client, tmp_path, mode):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        ws.send_json({"type": "set_mode", "agent_id": aid, "mode": mode})
        ev = recv_until(ws, lambda e: e.get("agent_id") == aid
                        and e.get("type") in ("mode_changed", "mode_rejected"))
    assert ev == {"type": "mode_changed", "agent_id": aid, "mode": mode}
    assert session.modes == [mode]


def test_mode_inconnu_rejete_sans_appel_sdk(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        ws.send_json({"type": "set_mode", "agent_id": aid, "mode": "yolo"})
        rej = recv_until(ws, is_("mode_rejected", agent_id=aid))
        after = drain(ws)
    assert isinstance(rej.get("reason"), str) and rej["reason"].strip(), rej
    assert session.modes == []
    assert not [e for e in after if e.get("type") == "mode_changed"], after


def test_employe_terminal_rejete(client, tmp_path):
    agent = {"id": "o-" + uuid.uuid4().hex[:8], "name": "eter-86", "cwd": str(tmp_path),
             "project": tmp_path.name, "status": "busy", "observed": True, "pid": 424242}
    client.portal.call(app_mod.hub.emit, {"type": "observed_joined", "agent": agent})
    try:
        with connect(client) as ws:
            handshake(ws)
            ws.send_json({"type": "set_mode", "agent_id": agent["id"], "mode": "plan"})
            rej = recv_until(ws, lambda e: e.get("type") in ("mode_rejected", "mode_changed"))
            after = drain(ws)
    finally:
        client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": agent["id"]})
    assert rej["type"] == "mode_rejected", rej
    assert rej["agent_id"] == agent["id"]
    assert isinstance(rej.get("reason"), str) and rej["reason"].strip(), rej
    assert not [e for e in after if e.get("type") == "mode_changed"], after


# ---------------------------------------------------------------- hello / snapshot


def test_hello_et_snapshot_exposent_le_mode(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, _ = ready(ws, tmp_path)
    with connect(client) as ws:
        hello, snap = handshake(ws)
    assert "mode" in agent_in(hello["team"], aid)
    assert "mode" in agent_in(snap["agents"], aid)


def test_snapshot_reflete_le_nouveau_mode(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, _ = ready(ws, tmp_path)
        ws.send_json({"type": "set_mode", "agent_id": aid, "mode": "acceptEdits"})
        recv_until(ws, is_("mode_changed", agent_id=aid))
    with connect(client) as ws:
        hello, snap = handshake(ws)
    assert agent_in(snap["agents"], aid)["mode"] == "acceptEdits"
    assert agent_in(hello["team"], aid)["mode"] == "acceptEdits"


# ---------------------------------------------------------------- persistance


def test_le_mode_survit_au_ticket_suivant(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, session = ready(ws, tmp_path)
        ws.send_json({"type": "set_mode", "agent_id": aid, "mode": "plan"})
        recv_until(ws, is_("mode_changed", agent_id=aid))
        title = f"suite-{uuid.uuid4().hex}"
        ws.send_json({"type": "new_ticket", "title": title, "agent_id": aid})
        tid2 = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                          and e["ticket"]["title"] == title)["ticket"]["id"]
        recv_until(ws, is_("ticket_done", ticket_id=tid2))
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert agent_in(snap["agents"], aid)["mode"] == "plan"
    sessions = [i for i in INSTANCES if isinstance(i, ModeClient) and i.cwd == session.cwd]
    assert all(m == "plan" for s in sessions for m in s.modes), [s.modes for s in sessions]
    for s in sessions:
        if s is not session:  # nouvelle session éventuelle : créée directement dans le mode choisi
            assert s.options.permission_mode == "plan"


# ---------------------------------------------------------------- ExitPlanMode

PLAN = "## Plan\n\n1. Lire le code\n2. Corriger le bug"


def exit_plan(client, decision):
    with connect(client) as ws:
        handshake(ws)
        task = start(client, "ExitPlanMode", {"plan": PLAN})
        ev = recv_until(ws, is_("permission_request", tool="ExitPlanMode"))
        ws.send_json({"type": "permission_decision", "request_id": ev["request_id"], **decision})
        recv_until(ws, is_("permission_resolved", request_id=ev["request_id"]))
    return ev, result(client, task)


def test_exit_plan_mode_porte_le_plan(client):
    ev, _ = exit_plan(client, {"allow": False})
    assert ev["plan"] == PLAN


def test_exit_plan_mode_approuve(client):
    _, res = exit_plan(client, {"allow": True})
    assert isinstance(res, PermissionResultAllow), res


def test_exit_plan_mode_rejete_avec_message(client):
    _, res = exit_plan(client, {"allow": False})
    assert isinstance(res, PermissionResultDeny), res
    assert isinstance(res.message, str) and res.message.strip()
