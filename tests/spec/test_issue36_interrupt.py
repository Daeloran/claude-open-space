"""Tests d'intention pour l'issue #36 : interrompre un employé piloté (équivalent d'Échap).

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; le bouton « Interrompre » et Échap (front) sont vérifiés à la main.

Contrat public supposé :
- Message WebSocket `{"type": "interrupt", "agent_id": A}`.
- A = employé piloté occupé : `interrupt()` de sa session (`backend.app.ClaudeSDKClient`, remplacée
  ici par `FakeClient`) est attendu ; le ticket en cours se termine par `ticket_done` `ok: false`
  (raison mentionnant « interrompu »).
- Une demande de permission/question en attente de cet employé est abandonnée : `permission_resolved`
  émis pour son `request_id`.
- A = employé piloté inactif : rien (pas de `ticket_done`, pas d'appel à `interrupt()`).
- A = employé terminal (« o-xxxxxxxx ») : refus avec une raison envoyé à l'émetteur (événement dont le
  type contient « reject »), rien n'est envoyé à Konsole (`backend.app.konsole.send_prompt`).
- La session est conservée : le ticket suivant est reçu par la même instance de session et se termine.

Hypothèses du faux SDK : après `interrupt()`, `receive_response()` se termine par un `ResultMessage`
non erroné — le `ok: false` doit donc venir de l'interruption elle-même. La demande de permission
est produite par le vrai `options.can_use_tool` de l'employé, appelé pendant la réponse.

Harnais : TestClient sans lifespan, portail partagé (cf. test_issue4_replay / test_issue12_routing).
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
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until

BLOCK = "BLOQUE"   # un prompt contenant ce mot bloque jusqu'à interrupt()
ASK = "DEMANDE"    # ... et demande en plus une permission Bash pendant la réponse
INSTANCES: list["FakeClient"] = []


class FakeClient:
    def __init__(self, options):
        self.options = options
        self.cwd = str(options.cwd)
        self.prompts: list[str] = []
        self.interrupts = 0
        self.started = asyncio.Event()
        self.interrupted = asyncio.Event()
        INSTANCES.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def connect(self, *a, **k):
        pass

    async def disconnect(self):
        pass

    async def query(self, prompt, *a, **k):
        self.prompts.append(prompt if isinstance(prompt, str) else str(prompt))

    async def interrupt(self):
        self.interrupts += 1
        self.interrupted.set()

    async def receive_response(self):
        prompt = self.prompts[-1] if self.prompts else ""
        if BLOCK in prompt:
            if ASK in prompt:
                # la tâche peut rester bloquée ou être annulée : on ne l'attend pas
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
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", cfg)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        yield c
        c.portal = None


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def hire(ws, tmp_path, title):
    """Recrute un employé dans un nouveau dossier avec le ticket `title` ; renvoie (agent_id, ticket_id, session)."""
    cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
    cwd.mkdir()
    ws.send_json({"type": "new_ticket", "title": title, "cwd": str(cwd)})
    aid = recv_until(ws, lambda e: e.get("type") == "agent_hired" and e["agent"]["cwd"] == str(cwd))["agent"]["id"]
    tid = recv_until(ws, lambda e: e.get("type") == "ticket_created" and e["ticket"]["title"] == title)["ticket"]["id"]
    recv_until(ws, is_("ticket_assigned", ticket_id=tid))
    (session,) = [i for i in INSTANCES if i.cwd == str(cwd)]
    return aid, tid, session


def wait_started(client, session):
    async def _w():
        await asyncio.wait_for(session.started.wait(), 3)
    client.portal.call(_w)


def busy(client, ws, tmp_path, extra=""):
    aid, tid, session = hire(ws, tmp_path, f"{BLOCK} {extra} {uuid.uuid4().hex}")
    wait_started(client, session)
    return aid, tid, session


# ---------------------------------------------------------------- employé occupé


def test_interrompre_un_employe_occupe(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = busy(client, ws, tmp_path)
        ws.send_json({"type": "interrupt", "agent_id": aid})
        done = recv_until(ws, is_("ticket_done", ticket_id=tid))
    assert session.interrupts == 1
    assert done["ok"] is False, done
    assert "interromp" in json.dumps(done, ensure_ascii=False).lower(), done


def test_interruption_abandonne_la_permission_en_attente(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = busy(client, ws, tmp_path, ASK)
        req = recv_until(ws, is_("permission_request", agent_id=aid))
        ws.send_json({"type": "interrupt", "agent_id": aid})
        recv_until(ws, is_("permission_resolved", request_id=req["request_id"]))
        recv_until(ws, is_("ticket_done", ticket_id=tid))
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert req["request_id"] not in [p["request_id"] for p in snap["pending_permissions"]]


def test_la_session_est_conservee_apres_interruption(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = busy(client, ws, tmp_path)
        ws.send_json({"type": "interrupt", "agent_id": aid})
        recv_until(ws, is_("ticket_done", ticket_id=tid))
        title = f"suite-{uuid.uuid4().hex}"
        ws.send_json({"type": "new_ticket", "title": title, "agent_id": aid})
        tid2 = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                          and e["ticket"]["title"] == title)["ticket"]["id"]
        assert recv_until(ws, is_("ticket_assigned", ticket_id=tid2))["agent_id"] == aid
        done2 = recv_until(ws, is_("ticket_done", ticket_id=tid2))
    assert done2["ok"] is True, done2
    assert any(title in p for p in session.prompts), "le ticket suivant doit arriver sur la même session"
    assert [i for i in INSTANCES if any(title in p for p in i.prompts)] == [session]


# ---------------------------------------------------------------- no-op


def test_interrompre_un_employe_inactif_ne_fait_rien(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid, tid, session = hire(ws, tmp_path, f"rapide-{uuid.uuid4().hex}")
        recv_until(ws, is_("ticket_done", ticket_id=tid))
        drain(ws)
        ws.send_json({"type": "interrupt", "agent_id": aid})
        after = drain(ws)
    assert session.interrupts == 0
    assert not [e for e in after if e.get("type") in ("ticket_done", "permission_resolved")], after


def test_interrompre_un_employe_terminal_est_refuse(client, tmp_path, monkeypatch):
    sent = []

    async def fake_send(pid, text):
        sent.append(text)

    monkeypatch.setattr(app_mod.konsole, "send_prompt", fake_send)
    agent = {"id": "o-" + uuid.uuid4().hex[:8], "name": "eter-86", "cwd": str(tmp_path),
             "project": tmp_path.name, "status": "busy", "observed": True, "pid": 424242}
    client.portal.call(app_mod.hub.emit, {"type": "observed_joined", "agent": agent})
    try:
        with connect(client) as ws:
            handshake(ws)
            ws.send_json({"type": "interrupt", "agent_id": agent["id"]})
            rej = recv_until(ws, lambda e: "reject" in str(e.get("type", "")))
            after = drain(ws)
    finally:
        client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": agent["id"]})
    assert isinstance(rej.get("reason"), str) and rej["reason"].strip(), rej
    assert sent == []
    assert not [e for e in after if e.get("type") == "ticket_done"], after
