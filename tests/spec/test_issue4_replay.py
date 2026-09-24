"""Tests d'intention pour l'issue #4 : reprise d'état à la reconnexion d'un client.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.

Contrat public supposé :
- À la connexion sur /ws, le client reçoit `hello` puis `{"type": "snapshot", ...}` contenant au minimum :
  - `tickets` : liste de {id, title, status}, status parmi "queued" / "assigned" / "done" ;
  - `totals` : {"usd": float, "tokens": int} ;
  - `context` : dict agent_id -> ratio ;
  - `pending_permissions` : liste des événements `permission_request` encore en attente.
- L'état est construit à partir des événements passés par `backend.app.hub.emit`.
- Une demande en attente = un Future dans `hub.pending[request_id]` + un `permission_request` émis.
- Sur `permission_decision`, tous les clients reçoivent
  `{"type": "permission_resolved", "request_id": rid, "allow": bool}`, le Future est résolu
  (l'employé est débloqué) et la demande disparaît du snapshot suivant.
- Un ticket créé via `new_ticket` apparaît dans le snapshot d'un client qui se connecte ensuite.
  Depuis #12, le ticket porte une destination (`cwd`) : il recrute un employé dont la session
  (`backend.app.ClaudeSDKClient`) est remplacée par une fausse classe.

Pas de vrai Claude : TestClient sans bloc `with` (le lifespan qui démarre les employés ne tourne pas).
Un portail partagé fait tourner toutes les websockets d'un test dans une seule boucle, comme en prod.
`hub` est un singleton de module : les tests utilisent des IDs uniques et des deltas.
"""
import asyncio
import json
import uuid

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import ResultMessage
from fastapi.testclient import TestClient

import backend.app as app_mod
from backend.app import app, hub

ORIGIN = {"origin": "http://testserver"}
TIMEOUT = 3


class FakeClient:
    """Session Claude factice : répond immédiatement à chaque prompt."""

    def __init__(self, options):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def connect(self, *a, **k):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt, *a, **k):
        pass

    async def receive_response(self):
        await asyncio.sleep(0)
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s-fake", total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1}, result="ok",
        )

    async def get_context_usage(self):
        return {"totalTokens": 1000, "maxTokens": 100000}


@pytest.fixture
def client():
    c = TestClient(app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal  # boucle partagée, sans lancer le lifespan
        yield c
        c.portal = None


def connect(client):
    return client.websocket_connect("/ws", headers=ORIGIN)


def recv(ws, timeout=TIMEOUT):
    async def _r():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()

    msg = ws.portal.call(_r)
    assert msg["type"] == "websocket.send", msg
    return json.loads(msg["text"])


def recv_until(ws, type_, timeout=TIMEOUT):
    while True:
        ev = recv(ws, timeout)
        if ev.get("type") == type_:
            return ev


def handshake(ws):
    """Renvoie (hello, snapshot) : le snapshot doit suivre hello."""
    hello = recv(ws)
    assert hello["type"] == "hello"
    snap = recv(ws)
    assert snap["type"] == "snapshot", f"attendu un snapshot après hello, reçu {snap!r}"
    return hello, snap


def emit(client, ev):
    client.portal.call(hub.emit, ev)


def make_pending(client, rid, agent_id):
    async def _mk():
        import asyncio

        fut = asyncio.get_running_loop().create_future()
        hub.pending[rid] = fut
        return fut

    fut = client.portal.call(_mk)
    emit(client, {"type": "permission_request", "request_id": rid, "agent_id": agent_id,
                  "tool": "Bash", "summary": "ls -la"})
    return fut


def agent_id_of(hello):
    team = hello.get("team") or []
    return team[0]["id"] if team else "a0"


def test_snapshot_suit_hello_avec_les_champs_attendus(client):
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert isinstance(snap["tickets"], list)
    assert set(snap["totals"]) >= {"usd", "tokens"}
    assert isinstance(snap["context"], dict)
    assert isinstance(snap["pending_permissions"], list)


def test_ticket_cree_visible_apres_reconnexion(monkeypatch, tmp_path, client):
    # monkeypatch avant client : le patch reste actif jusqu'à la fermeture du portail
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", tmp_path)
    title = f"ticket-{uuid.uuid4().hex}"
    with connect(client) as ws:
        handshake(ws)
        ws.send_json({"type": "new_ticket", "title": title, "cwd": str(tmp_path)})
        created = recv_until(ws, "ticket_created")
        assert created["ticket"]["title"] == title
    with connect(client) as ws:
        _, snap = handshake(ws)
    match = [t for t in snap["tickets"] if t["title"] == title]
    assert len(match) == 1
    assert match[0]["id"] == created["ticket"]["id"]
    assert match[0]["status"] in ("queued", "assigned", "done")


def test_statuts_de_tickets_assigne_et_termine(client):
    with connect(client) as ws:
        hello, _ = handshake(ws)
    aid = agent_id_of(hello)
    ta, td = f"ta-{uuid.uuid4().hex}", f"td-{uuid.uuid4().hex}"
    for tid in (ta, td):
        emit(client, {"type": "ticket_created", "ticket": {"id": tid, "title": tid}})
        emit(client, {"type": "ticket_assigned", "ticket_id": tid, "agent_id": aid})
    emit(client, {"type": "ticket_done", "ticket_id": td, "agent_id": aid,
                  "usd": 0.0, "tokens": 0, "ok": True})
    with connect(client) as ws:
        _, snap = handshake(ws)
    status = {t["id"]: t["status"] for t in snap["tickets"]}
    assert status[ta] == "assigned"
    assert status[td] == "done"


def test_totaux_et_contexte_dans_le_snapshot(client):
    with connect(client) as ws:
        hello, before = handshake(ws)
    aid = agent_id_of(hello)
    emit(client, {"type": "cost", "agent_id": aid, "usd": 0.25, "tokens": 1234})
    emit(client, {"type": "context", "agent_id": aid, "ratio": 0.42})
    with connect(client) as ws:
        _, after = handshake(ws)
    assert after["totals"]["usd"] == pytest.approx(before["totals"]["usd"] + 0.25)
    assert after["totals"]["tokens"] == before["totals"]["tokens"] + 1234
    assert after["context"][aid] == pytest.approx(0.42)


def test_demande_en_attente_renvoyee_et_decision_debloque_employe(client):
    with connect(client) as ws:
        hello, _ = handshake(ws)
    rid = f"r-{uuid.uuid4().hex}"
    fut = make_pending(client, rid, agent_id_of(hello))

    with connect(client) as ws:
        _, snap = handshake(ws)
        pending = [p for p in snap["pending_permissions"] if p["request_id"] == rid]
        assert len(pending) == 1
        assert pending[0]["type"] == "permission_request"
        ws.send_json({"type": "permission_decision", "request_id": rid, "allow": True})
        resolved = recv_until(ws, "permission_resolved")
        assert resolved["request_id"] == rid
        assert resolved["allow"] is True

    assert fut.done(), "la décision doit débloquer l'employé (Future résolu)"

    with connect(client) as ws:
        _, snap = handshake(ws)
    assert rid not in [p["request_id"] for p in snap["pending_permissions"]]


def test_decision_dans_un_onglet_ferme_la_popup_dans_l_autre(client):
    with connect(client) as ws:
        hello, _ = handshake(ws)
    rid = f"r-{uuid.uuid4().hex}"
    fut = make_pending(client, rid, agent_id_of(hello))

    with connect(client) as a, connect(client) as b:
        handshake(a)
        handshake(b)
        a.send_json({"type": "permission_decision", "request_id": rid, "allow": False})
        for ws in (a, b):
            ev = recv_until(ws, "permission_resolved")
            assert ev == {"type": "permission_resolved", "request_id": rid, "allow": False}

    assert fut.done()
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert rid not in [p["request_id"] for p in snap["pending_permissions"]]
