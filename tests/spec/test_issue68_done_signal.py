"""Tests d'intention pour l'issue #68 : signal « a fini de répondre » (agent_done / agent_seen).

Écrits en boîte noire depuis l'issue et son commentaire « Public contract », sans lire le corps des
fonctions de backend/. Seul le backend est couvert ; l'affichage (front mono-fichier) est vérifié à la main.

Contrat public supposé :
- Événement `{"type": "agent_done", "agent_id"}` émis par le backend à la fin d'une réponse :
  - employé piloté : après chaque ticket traité (y compris un ticket clos en avance par une PR, #54 :
    l'`agent_done` vient alors à la fin du tour, pas au `ticket_done` anticipé) ; pas quand le manager
    l'a interrompu (#36) ;
  - employé terminal : `observed_turn_end`, ou `observed_status` `busy` → `idle` ; une seule fois
    tant que l'employé n'est pas redevenu actif.
  - Pas émis si un panneau de discussion est ouvert sur cet employé à ce moment, ni pour un stagiaire.
- Tant qu'il est « fini », l'entrée de l'employé dans `snapshot["agents"]` porte `"done": True`
  (sinon `done` absent ou faux).
- `open_chat {"agent_id"}` sur un employé fini l'efface et émet `{"type": "agent_seen", "agent_id"}`
  à tous les clients.
- L'activité l'efface sans événement : `ticket_assigned`, `observed_status` `busy`/`waiting`,
  `tool_use`, `permission_request` de cet employé.

Hypothèses d'intégration (reprises des autres tests de spec) :
- L'état est tenu par `backend.app.hub` à partir des événements passés par `hub.emit` : les employés
  terminal et leurs événements (`observed_joined`, `observed_status`, `observed_turn_end
  {"agent_id", "at"}`, `tool_use`, `subagent_spawned`…) sont injectés directement par `hub.emit`,
  comme dans test_issue13_observer / test_issue36_interrupt / test_issue51_reload_state.
- Employés pilotés : faux SDK (`backend.app.ClaudeSDKClient` remplacé), façon test_issue36_interrupt
  (BLOQUE → attend `interrupt()`) et test_issue54_pr_done (PR → `gh pr create` réussi puis suite du tour).
- Un panneau est « ouvert » dès l'`open_chat` d'un client, même si l'historique renvoyé est vide ou
  en erreur (pas de transcript dans ces tests), et jusqu'à `close_chat`.
- Stagiaire : `subagent_spawned {"parent_id", "agent": {"id", "name"}, "task"}` puis `subagent_done`.
- Demande de permission : Future dans `hub.pending` + `permission_request` émis (cf. test_issue4_replay),
  résolue ensuite par `permission_decision` pour ne pas polluer le singleton.
- `hub` est un singleton : ids uniques par test. Attente d'un événement bornée à 2 s.
"""
import asyncio
import uuid
from datetime import datetime, timezone

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolResultBlock, ToolUseBlock, UserMessage
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until

WAIT = 2
BLOCK = "BLOQUE"  # le tour attend interrupt()
PR = "FAITPR"     # le tour crée une PR puis continue (Read) avant le ResultMessage
INSTANCES: list["FakeClient"] = []


def _tid():
    return f"toolu_{uuid.uuid4().hex[:8]}"


class FakeClient:
    def __init__(self, options):
        self.cwd = str(getattr(options, "cwd", None))
        self.prompts: list[str] = []
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
        self.interrupted.set()

    async def receive_response(self):
        prompt = self.prompts[-1] if self.prompts else ""
        if BLOCK in prompt:
            self.started.set()
            await self.interrupted.wait()
            self.interrupted.clear()
        elif PR in prompt:
            tid = _tid()
            yield AssistantMessage(content=[ToolUseBlock(id=tid, name="Bash", input={"command": "gh pr create --fill"})],
                                   model="claude-opus-5")
            yield UserMessage(content=[ToolResultBlock(tool_use_id=tid, content="https://github.com/o/r/pull/7",
                                                       is_error=False)])
            await asyncio.sleep(0.05)
            later = _tid()
            yield AssistantMessage(content=[ToolUseBlock(id=later, name="Read", input={"file_path": "/tmp/APRES-PR"})],
                                   model="claude-opus-5")
            yield UserMessage(content=[ToolResultBlock(tool_use_id=later, content="x", is_error=False)])
            await asyncio.sleep(0.05)
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
        yield c
        c.portal = None


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def emit(client, ev):
    client.portal.call(app_mod.hub.emit, ev)


def done_events(seen, aid):
    return [e for e in seen if is_("agent_done", agent_id=aid)(e)]


def snap_agent(client, aid):
    with connect(client) as ws:
        _, snap = handshake(ws)
    (agent,) = [a for a in snap["agents"] if a["id"] == aid]
    return agent


def is_done(client, aid):
    return snap_agent(client, aid).get("done") is True


def index_of(seen, pred):
    return next(i for i, e in enumerate(seen) if pred(e))


# ---------------------------------------------------------------- employé piloté


def send_ticket(ws, title, tmp_path=None, aid=None, seen=None):
    """Envoie un ticket (nouvel employé si `aid` est None) ; renvoie (agent_id, ticket_id)."""
    msg = {"type": "new_ticket", "title": title}
    if aid is None:
        cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
        cwd.mkdir()
        msg["cwd"] = str(cwd)
    else:
        msg["agent_id"] = aid
    ws.send_json(msg)
    tid = recv_until(ws, lambda e: e.get("type") == "ticket_created" and e["ticket"]["title"] == title,
                     seen=seen)["ticket"]["id"]
    assigned = recv_until(ws, is_("ticket_assigned", ticket_id=tid), seen=seen)
    return assigned["agent_id"], tid


def wait_started(client, title):
    (session,) = [i for i in INSTANCES if any(title in p for p in i.prompts)]

    async def _w():
        await asyncio.wait_for(session.started.wait(), WAIT)

    client.portal.call(_w)


def piloted_done(client, ws, tmp_path):
    """Recrute un employé piloté et attend son premier agent_done ; renvoie son id."""
    aid, _ = send_ticket(ws, f"t-{uuid.uuid4().hex}", tmp_path)
    recv_until(ws, is_("agent_done", agent_id=aid), timeout=WAIT)
    return aid


def test_pilote_fin_de_ticket_emet_agent_done_et_snapshot_done(client, tmp_path):
    seen: list[dict] = []
    with connect(client) as ws:
        handshake(ws)
        aid, tid = send_ticket(ws, f"t-{uuid.uuid4().hex}", tmp_path, seen=seen)
        recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
        seen += drain(ws, 0.5)
    assert len(done_events(seen, aid)) == 1, seen
    assert is_done(client, aid)


def test_pilote_un_agent_done_par_ticket(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid = piloted_done(client, ws, tmp_path)
        seen: list[dict] = []
        _, tid2 = send_ticket(ws, f"t2-{uuid.uuid4().hex}", aid=aid, seen=seen)
        recv_until(ws, is_("ticket_done", ticket_id=tid2), seen=seen)
        seen += drain(ws, 0.5)
    assert len(done_events(seen, aid)) == 1, seen


def test_pilote_interrompu_pas_d_agent_done(client, tmp_path):
    title = f"{BLOCK} {uuid.uuid4().hex}"
    seen: list[dict] = []
    with connect(client) as ws:
        handshake(ws)
        aid, tid = send_ticket(ws, title, tmp_path, seen=seen)
        wait_started(client, title)
        ws.send_json({"type": "interrupt", "agent_id": aid})
        recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
        seen += drain(ws, 0.5)
    assert done_events(seen, aid) == [], seen
    assert not is_done(client, aid)


def test_pilote_ticket_clos_par_pr_agent_done_en_fin_de_tour(client, tmp_path):
    seen: list[dict] = []
    with connect(client) as ws:
        handshake(ws)
        aid, tid = send_ticket(ws, f"{PR} {uuid.uuid4().hex}", tmp_path, seen=seen)
        recv_until(ws, is_("agent_done", agent_id=aid), timeout=WAIT, seen=seen)
        seen += drain(ws, 0.5)
    assert len(done_events(seen, aid)) == 1, seen
    later = lambda e: is_("tool_use", agent_id=aid, tool="Read")(e)  # noqa: E731
    assert any(later(e) for e in seen), "le faux SDK a bien joué la suite du tour"
    assert index_of(seen, is_("ticket_done", ticket_id=tid)) < index_of(seen, later) \
        < index_of(seen, is_("agent_done", agent_id=aid)), seen
    assert is_done(client, aid)


def test_pilote_ticket_assigned_efface_done(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid = piloted_done(client, ws, tmp_path)
        assert is_done(client, aid)
        title = f"{BLOCK} {uuid.uuid4().hex}"
        _, tid = send_ticket(ws, title, aid=aid)
        wait_started(client, title)
        assert not is_done(client, aid)
        ws.send_json({"type": "interrupt", "agent_id": aid})
        recv_until(ws, is_("ticket_done", ticket_id=tid))


def test_open_chat_sur_pilote_fini_emet_agent_seen_a_tous(client, tmp_path):
    with connect(client) as other, connect(client) as ws:
        handshake(other)
        handshake(ws)
        aid = piloted_done(client, ws, tmp_path)
        ws.send_json({"type": "open_chat", "agent_id": aid})
        recv_until(ws, is_("agent_seen", agent_id=aid), timeout=WAIT)
        recv_until(other, is_("agent_seen", agent_id=aid), timeout=WAIT)
        ws.send_json({"type": "close_chat", "agent_id": aid})
    assert not is_done(client, aid)


def test_pilote_panneau_ouvert_pas_d_agent_done(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid = piloted_done(client, ws, tmp_path)
        ws.send_json({"type": "open_chat", "agent_id": aid})
        recv_until(ws, is_("chat_history", agent_id=aid))
        seen: list[dict] = []
        _, tid = send_ticket(ws, f"t-{uuid.uuid4().hex}", aid=aid, seen=seen)
        recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
        seen += drain(ws, 0.5)
        assert done_events(seen, aid) == [], seen
        assert not is_done(client, aid)
        # panneau refermé : le ticket suivant redonne le signal (le test n'est pas vide)
        ws.send_json({"type": "close_chat", "agent_id": aid})
        send_ticket(ws, f"t-{uuid.uuid4().hex}", aid=aid)
        recv_until(ws, is_("agent_done", agent_id=aid), timeout=WAIT)


# ---------------------------------------------------------------- employé terminal (via hub.emit)


def ts():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def term(client, tmp_path):
    """Employé terminal observé, occupé (busy), injecté via hub.emit ; retiré en fin de test."""
    aid = "o-" + uuid.uuid4().hex[:8]
    emit(client, {"type": "observed_joined", "agent": {
        "id": aid, "name": "eter-68", "cwd": str(tmp_path), "project": tmp_path.name,
        "status": "busy", "observed": True}})
    yield aid
    emit(client, {"type": "observed_left", "agent_id": aid})


def status(client, aid, s):
    emit(client, {"type": "observed_status", "agent_id": aid, "status": s})


def turn_end(client, aid):
    emit(client, {"type": "observed_turn_end", "agent_id": aid, "at": ts()})


def test_terminal_turn_end_emet_agent_done_une_fois(client, term):
    with connect(client) as ws:
        handshake(ws)
        turn_end(client, term)
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT)
        assert is_done(client, term)
        turn_end(client, term)
        status(client, term, "idle")  # busy → idle juste après la fin du tour : même réponse
        after = drain(ws, 0.5)
    assert done_events(after, term) == [], after
    assert is_done(client, term)


def test_terminal_busy_vers_idle_emet_agent_done(client, term):
    with connect(client) as ws:
        handshake(ws)
        status(client, term, "idle")
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT)
        turn_end(client, term)
        after = drain(ws, 0.5)
    assert done_events(after, term) == [], after
    assert is_done(client, term)


def test_terminal_redevenu_actif_redonne_le_signal(client, term):
    with connect(client) as ws:
        handshake(ws)
        turn_end(client, term)
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT)
        status(client, term, "busy")
        assert not is_done(client, term)
        status(client, term, "idle")
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT)
    assert is_done(client, term)


def make_pending(client, rid, aid):
    async def _mk():
        fut = asyncio.get_running_loop().create_future()
        app_mod.hub.pending[rid] = fut
        return fut

    client.portal.call(_mk)
    emit(client, {"type": "permission_request", "request_id": rid, "agent_id": aid,
                  "tool": "Bash", "summary": "ls -la"})


@pytest.mark.parametrize("activity", ["ticket_assigned", "busy", "waiting", "tool_use", "permission_request"])
def test_terminal_activite_efface_done_sans_evenement(client, term, activity):
    with connect(client) as ws:
        handshake(ws)
        status(client, term, "idle")
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT)
        assert is_done(client, term)
        rid = f"r-{uuid.uuid4().hex}"
        if activity == "ticket_assigned":
            tid = f"t-{uuid.uuid4().hex}"
            emit(client, {"type": "ticket_created", "ticket": {"id": tid, "title": tid}})
            emit(client, {"type": "ticket_assigned", "ticket_id": tid, "agent_id": term})
        elif activity in ("busy", "waiting"):
            status(client, term, activity)
        elif activity == "tool_use":
            emit(client, {"type": "tool_use", "agent_id": term, "tool": "Read", "summary": "app.py"})
        else:
            make_pending(client, rid, term)
        after = drain(ws, 0.3)
        cleared = not is_done(client, term)
        if activity == "permission_request":
            ws.send_json({"type": "permission_decision", "request_id": rid, "allow": False})
            recv_until(ws, is_("permission_resolved", request_id=rid))
    assert cleared, activity
    assert not [e for e in after if e.get("type") in ("agent_done", "agent_seen")], after


def test_terminal_open_chat_efface_done_et_emet_agent_seen(client, term):
    with connect(client) as other, connect(client) as ws:
        handshake(other)
        handshake(ws)
        turn_end(client, term)
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT)
        ws.send_json({"type": "open_chat", "agent_id": term})
        recv_until(ws, is_("agent_seen", agent_id=term), timeout=WAIT)
        recv_until(other, is_("agent_seen", agent_id=term), timeout=WAIT)
        ws.send_json({"type": "close_chat", "agent_id": term})
    assert not is_done(client, term)


def test_terminal_panneau_ouvert_pas_d_agent_done(client, term):
    with connect(client) as ws:
        handshake(ws)
        ws.send_json({"type": "open_chat", "agent_id": term})
        recv_until(ws, is_("chat_history", agent_id=term))
        turn_end(client, term)
        status(client, term, "idle")
        seen = drain(ws, 0.5)
        assert done_events(seen, term) == [], seen
        assert not is_done(client, term)
        # panneau refermé : la réponse suivante redonne le signal
        ws.send_json({"type": "close_chat", "agent_id": term})
        sync = "o-" + uuid.uuid4().hex[:8]
        ws.send_json({"type": "open_chat", "agent_id": sync})  # synchronisation : close_chat traité (cf. #35)
        recv_until(ws, is_("chat_history", agent_id=sync))
        status(client, term, "busy")
        status(client, term, "idle")
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT)


# ---------------------------------------------------------------- stagiaires


def test_stagiaire_jamais_fini(client, term):
    intern = f"i-{uuid.uuid4().hex[:8]}"
    with connect(client) as ws:
        handshake(ws)
        emit(client, {"type": "subagent_spawned", "parent_id": term,
                      "agent": {"id": intern, "name": "Tom"}, "task": "explore le dépôt"})
        emit(client, {"type": "tool_use", "agent_id": intern, "tool": "Read", "summary": "app.py"})
        emit(client, {"type": "subagent_done", "agent_id": intern})
        seen = drain(ws, 0.5)
        # le parent, lui, reçoit bien le signal
        status(client, term, "idle")
        recv_until(ws, is_("agent_done", agent_id=term), timeout=WAIT, seen=seen)
        seen += drain(ws, 0.3)
    assert done_events(seen, intern) == [], seen
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert not [a for a in snap["agents"] if a["id"] == intern and a.get("done")], snap["agents"]
