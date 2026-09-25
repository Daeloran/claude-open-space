"""Tests d'intention pour l'issue #59 : congédier un employé quand la conversation est finie.

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; boutons, confirmation, marche vers la porte et mode démo (front)
sont vérifiés à la main.

Contrat public supposé :
- Message WebSocket `{"type": "dismiss", "agent_id": A}`.
- A = employé piloté : ses tickets en cours et en attente se terminent par `ticket_done` `ok: false` ;
  ses demandes de permission en attente reçoivent `permission_resolved` `allow: false` ; puis
  `{"type": "agent_left", "agent_id": A}` est diffusé. Sa session SDK (`backend.app.ClaudeSDKClient`,
  remplacée ici par `FakeClient`) est fermée (`__aexit__` ou `disconnect()`). Il n'est plus dans
  `snapshot.agents` ; un `new_ticket` vers A → `ticket_rejected`. Le ticket en attente n'est jamais
  envoyé à la session.
- A = employé terminal (« o-xxxxxxxx ») : `{"type": "observed_left", "agent_id": A}` est diffusé ; les
  polls suivants de l'`Observer` n'émettent plus rien pour A (même avec de nouvelles lignes de
  transcript ou un changement de statut) ; A n'est plus dans `snapshot.agents` ; le fichier de
  session (`sessions/<pid>.json`) est inchangé et aucun signal n'est envoyé au processus.
- A inconnu : aucun événement de départ, la connexion reste utilisable.

Hypothèses : le faux SDK est piloté par le prompt (cf. test_issue36_interrupt) ; après `interrupt()`
ou annulation, `receive_response()` peut finir par un `ResultMessage` non erroné — le `ok: false`
doit venir du congédiement. L'`Observer` terminal émet via `backend.app.hub.emit` et est exposé en
`backend.app.observer` (câblage de test_issue54_pr_done).

Harnais : TestClient sans lifespan, portail partagé (test_issue12_routing), faux ~/.claude
(test_issue13_observer).
"""
import asyncio
import os
import uuid

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import ResultMessage
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until
from tests.spec.test_issue13_observer import (
    Alive, aid, append, new_sid, prompt, tool_use, transcript_path, write_session,
)

BLOCK = "BLOQUE"   # un prompt contenant ce mot bloque jusqu'à interrupt() (ou annulation)
ASK = "DEMANDE"    # ... et demande en plus une permission Bash pendant la réponse
PID = 595959
INSTANCES: list["FakeClient"] = []


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.cwd = str(options.cwd)
        self.prompts: list[str] = []
        self.started = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.closed = asyncio.Event()
        INSTANCES.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed.set()
        return False

    async def connect(self, *a, **k):
        pass

    async def disconnect(self):
        self.closed.set()

    async def query(self, prompt, *a, **k):
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))

    async def interrupt(self):
        self.interrupted.set()

    async def receive_response(self):
        prompt = self.prompts[-1] if self.prompts else ""
        if BLOCK in prompt:
            if ASK in prompt:
                self.ask_task = asyncio.create_task(
                    self.options.can_use_tool("Bash", {"command": "rm -rf build"}, None))
            self.started.set()
            await self.interrupted.wait()
            self.interrupted.clear()
        else:
            await asyncio.sleep(0)
        yield ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="s-fake", total_cost_usd=0.0,
            usage={"input_tokens": 1, "output_tokens": 1}, result="ok",
        )

    async def get_context_usage(self):
        return {"totalTokens": 1000, "maxTokens": 100000}


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = tmp_path / "claude-config"
    (cfg / "projects").mkdir(parents=True)
    (cfg / "sessions").mkdir()
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", cfg)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        c.config_dir = cfg
        yield c
        c.portal = None


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def left(agent_id):
    return lambda e: e.get("type") in ("agent_left", "observed_left") and e.get("agent_id") == agent_id


def about(agent_id):
    return lambda e: e.get("agent_id") == agent_id or (e.get("agent") or {}).get("id") == agent_id


def wait_event(client, ev):
    async def _w():
        await asyncio.wait_for(ev.wait(), 3)
    client.portal.call(_w)


def busy(client, ws, tmp_path, extra=""):
    """Recrute un employé occupé (premier ticket bloquant) ; renvoie (agent_id, ticket_id, session)."""
    cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
    cwd.mkdir()
    title = f"{BLOCK} {extra} {uuid.uuid4().hex}"
    ws.send_json({"type": "new_ticket", "title": title, "cwd": str(cwd)})
    a = recv_until(ws, lambda e: e.get("type") == "agent_hired" and e["agent"]["cwd"] == str(cwd))["agent"]["id"]
    tid = recv_until(ws, lambda e: e.get("type") == "ticket_created" and e["ticket"]["title"] == title)["ticket"]["id"]
    recv_until(ws, is_("ticket_assigned", ticket_id=tid))
    (session,) = [i for i in INSTANCES if i.cwd == str(cwd)]
    wait_event(client, session.started)
    return a, tid, session


def queue_ticket(ws, agent_id):
    title = f"attente-{uuid.uuid4().hex}"
    ws.send_json({"type": "new_ticket", "title": title, "agent_id": agent_id})
    ev = recv_until(ws, lambda e: e.get("type") == "ticket_rejected"
                    or (e.get("type") == "ticket_created" and e["ticket"]["title"] == title))
    return title, ev


def snapshot_agents(client):
    with connect(client) as ws:
        _, snap = handshake(ws)
    return [a["id"] for a in snap["agents"]]


# ---------------------------------------------------------------- employé piloté


def test_congedier_un_employe_pilote(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        a, tid1, session = busy(client, ws, tmp_path)
        title2, created = queue_ticket(ws, a)
        assert created["type"] == "ticket_created", created
        tid2 = created["ticket"]["id"]

        seen: list[dict] = []
        ws.send_json({"type": "dismiss", "agent_id": a})
        done1 = recv_until(ws, is_("ticket_done", ticket_id=tid1), seen=seen)
        done2 = recv_until(ws, is_("ticket_done", ticket_id=tid2), seen=seen)
        recv_until(ws, is_("agent_left", agent_id=a), seen=seen)
        seen += drain(ws)

        _, rej = queue_ticket(ws, a)
    assert done1["ok"] is False, done1
    assert done2["ok"] is False, done2
    assert rej["type"] == "ticket_rejected", rej
    assert not any(title2 in p for p in session.prompts), "le ticket en attente ne doit pas être traité"
    wait_event(client, session.closed)  # session SDK fermée
    assert a not in snapshot_agents(client)


def test_congedier_refuse_la_permission_en_attente(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        a, tid, session = busy(client, ws, tmp_path, ASK)
        req = recv_until(ws, is_("permission_request", agent_id=a))
        ws.send_json({"type": "dismiss", "agent_id": a})
        res = recv_until(ws, is_("permission_resolved", request_id=req["request_id"]))
        recv_until(ws, is_("agent_left", agent_id=a))
    assert res.get("allow") is False, res
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert req["request_id"] not in [p["request_id"] for p in snap["pending_permissions"]]


# ---------------------------------------------------------------- employé terminal


@pytest.fixture
def terminal(client, tmp_path, monkeypatch):
    """Session terminal observée ; l'Observer émet vers le hub et est exposé en app_mod.observer."""
    from backend.observer import Observer

    cfg = client.config_dir
    proj = tmp_path / "eter"
    proj.mkdir()
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt("salut"))
    write_session(cfg, PID, sid, proj, status="idle")
    alive = Alive(PID)
    obs = Observer(cfg, app_mod.hub.emit, pid_alive=alive)
    monkeypatch.setattr(app_mod, "observer", obs, raising=False)

    class T:
        pass

    term = T()
    term.cfg, term.sid, term.proj, term.transcript, term.aid = cfg, sid, proj, t, aid(sid)
    term.session_file = cfg / "sessions" / f"{PID}.json"
    term.poll = lambda: client.portal.call(obs.poll)
    term.status = lambda s: write_session(cfg, PID, sid, proj, status=s, key=False)
    yield term
    alive.pids.clear()
    term.session_file.unlink(missing_ok=True)
    try:
        term.poll()
    finally:  # ne pas laisser l'employé dans le hub partagé si le test échoue
        client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": term.aid})


def test_congedier_un_employe_terminal(client, terminal, monkeypatch):
    kills = []
    real_kill = os.kill
    monkeypatch.setattr(os, "kill", lambda pid, sig: kills.append((pid, sig)) if pid == PID else real_kill(pid, sig))

    with connect(client) as ws:
        handshake(ws)
        terminal.poll()
        recv_until(ws, lambda e: e.get("type") == "observed_joined" and e["agent"]["id"] == terminal.aid)
        ws.send_json({"type": "dismiss", "agent_id": terminal.aid})
        recv_until(ws, is_("observed_left", agent_id=terminal.aid))

        append(terminal.transcript, tool_use(tid="toolu_apres", name="Read", inp={"file_path": "/tmp/x"}))
        terminal.poll()
        terminal.status("busy")
        terminal.poll()
        after = drain(ws)
    assert not [e for e in after if about(terminal.aid)(e)], after
    assert terminal.aid not in snapshot_agents(client)
    assert [k for k in kills if k[1] != 0] == [], "le processus Claude ne doit pas être touché"


def test_congedier_un_employe_terminal_ne_touche_pas_le_fichier_de_session(client, terminal):
    with connect(client) as ws:
        handshake(ws)
        terminal.poll()
        recv_until(ws, lambda e: e.get("type") == "observed_joined" and e["agent"]["id"] == terminal.aid)
        before = terminal.session_file.read_bytes()
        ws.send_json({"type": "dismiss", "agent_id": terminal.aid})
        recv_until(ws, is_("observed_left", agent_id=terminal.aid))
        terminal.poll()
        after = drain(ws)
    assert terminal.session_file.read_bytes() == before
    assert not [e for e in after if about(terminal.aid)(e)], after


# ---------------------------------------------------------------- id inconnu


def test_congedier_un_id_inconnu_ne_fait_rien(client):
    ghost = f"inconnu-{uuid.uuid4().hex[:8]}"
    with connect(client) as ws:
        handshake(ws)
        ws.send_json({"type": "dismiss", "agent_id": ghost})
        after = drain(ws)
        _, rej = queue_ticket(ws, ghost)  # la connexion répond toujours normalement
    assert not [e for e in after if e.get("type") in ("agent_left", "observed_left")], after
    assert rej["type"] == "ticket_rejected", rej
