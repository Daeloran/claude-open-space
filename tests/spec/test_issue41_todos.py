"""Tests d'intention pour l'issue #41 : suivre l'avancement (liste de tâches TodoWrite).

Écrits en boîte noire depuis l'issue, sans lire le corps des fonctions de backend/.
Seul le backend est couvert ; le panneau (liste repliable, « n/m tâches ») est vérifié à la main.

Contrat public supposé :
- Un `tool_use` TodoWrite produit `{"type": "todos", "agent_id": A, "todos": [{"content": str,
  "status": "pending"|"in_progress"|"completed"}, ...]}` :
  - employé piloté : `AssistantMessage` du SDK contenant un `ToolUseBlock(name="TodoWrite",
    input={"todos": [{"content", "status", "activeForm"}]})` ;
  - employé terminal : ligne assistant `tool_use` TodoWrite du transcript lue par l'`Observer`.
- Chaque élément garde au moins `content` et `status` (clés en plus tolérées : vérif. par sous-ensemble).
- Un second TodoWrite remplace la liste (pas de fusion).
- Le `snapshot` /ws expose la dernière liste par employé sous `todos` : `{"<agent_id>": [...]}`.
- Entrée malformée (`todos` pas une liste, éléments pas des dicts) : pas de crash ; éléments invalides ignorés.

Hypothèses : le `todos` d'un employé piloté est émis pendant son ticket (avant `ticket_done`) ;
l'état du snapshot vient des événements `todos` passés par `hub.emit`.

Harnais : faux SDK façon test_issue36_interrupt (le prompt choisit le TodoWrite renvoyé),
Observer sur faux ~/.claude de test_issue13_observer.
"""
import asyncio
import uuid

import anyio
import anyio.from_thread
import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock
from fastapi.testclient import TestClient

import backend.app as app_mod
from tests.spec.test_issue12_routing import connect, drain, handshake, recv_until
from tests.spec.test_issue13_observer import (  # noqa: F401
    Alive, aid, append, cfg, harness, new_sid, of, proj, prompt, tool_use, transcript_path, write_session,
)

LIST_A = [
    {"content": "Lire le code", "status": "completed", "activeForm": "Lecture du code"},
    {"content": "Écrire les tests", "status": "in_progress", "activeForm": "Écriture des tests"},
    {"content": "Ouvrir la PR", "status": "pending", "activeForm": "Ouverture de la PR"},
]
LIST_B = [
    {"content": "Corriger le bug", "status": "in_progress", "activeForm": "Correction du bug"},
]

# titre du ticket -> input du TodoWrite renvoyé par la fausse session
SCRIPTS: dict[str, object] = {}


class FakeClient:
    def __init__(self, options):
        self.prompts: list[str] = []

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

    async def receive_response(self):
        prompt = self.prompts[-1] if self.prompts else ""
        for key, inp in SCRIPTS.items():
            if key in prompt:
                yield AssistantMessage(
                    content=[ToolUseBlock(id=f"toolu_{uuid.uuid4().hex[:8]}", name="TodoWrite", input=inp)],
                    model="claude-opus-5")
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
    c_dir = tmp_path / "claude-config"
    (c_dir / "projects").mkdir(parents=True)
    monkeypatch.setattr(app_mod, "CLAUDE_CONFIG_DIR", c_dir)
    monkeypatch.setattr(app_mod, "ClaudeSDKClient", FakeClient)
    c = TestClient(app_mod.app)
    with anyio.from_thread.start_blocking_portal() as portal:
        c.portal = portal
        yield c
        c.portal = None


def is_(type_, **fields):
    return lambda e: e.get("type") == type_ and all(e.get(k) == v for k, v in fields.items())


def core(items):
    """Ce que le contrat garantit pour chaque élément."""
    return [{"content": i["content"], "status": i["status"]} for i in items]


def assert_todos(got, expected):
    assert isinstance(got, list), got
    assert len(got) == len(expected), got
    for g, e in zip(got, expected):
        assert isinstance(g, dict), got
        assert {k: g.get(k) for k in e} == e, got


def ticket(ws, tmp_path, todos_input, aid_=None, seen=None):
    """Envoie un ticket dont la session renvoie un TodoWrite ; renvoie (agent_id, ticket_done)."""
    title = f"todo-{uuid.uuid4().hex}"
    SCRIPTS[title] = todos_input
    msg = {"type": "new_ticket", "title": title}
    if aid_ is None:
        cwd = tmp_path / f"proj-{uuid.uuid4().hex[:8]}"
        cwd.mkdir()
        msg["cwd"] = str(cwd)
    else:
        msg["agent_id"] = aid_
    ws.send_json(msg)
    tid = recv_until(ws, lambda e: e.get("type") == "ticket_created"
                     and e["ticket"]["title"] == title, seen=seen)["ticket"]["id"]
    assigned = recv_until(ws, is_("ticket_assigned", ticket_id=tid), seen=seen)
    done = recv_until(ws, is_("ticket_done", ticket_id=tid), seen=seen)
    return assigned["agent_id"], done


# ---------------------------------------------------------------- employé piloté


def test_todowrite_pilote_emet_un_evenement_todos(client, tmp_path):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        aid_, _ = ticket(ws, tmp_path, {"todos": LIST_A}, seen=seen)
    evs = [e for e in seen if e.get("type") == "todos" and e.get("agent_id") == aid_]
    assert len(evs) == 1, seen
    assert_todos(evs[0]["todos"], core(LIST_A))


def test_second_todowrite_pilote_remplace_la_liste(client, tmp_path):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        aid_, _ = ticket(ws, tmp_path, {"todos": LIST_A})
        ticket(ws, tmp_path, {"todos": LIST_B}, aid_=aid_, seen=seen)
    with connect(client) as ws:
        _, snap = handshake(ws)
    evs = [e for e in seen if e.get("type") == "todos" and e.get("agent_id") == aid_]
    assert len(evs) == 1, seen
    assert_todos(evs[0]["todos"], core(LIST_B))
    assert_todos(snap["todos"][aid_], core(LIST_B))


def test_snapshot_rejoue_les_todos_du_pilote(client, tmp_path):
    with connect(client) as ws:
        handshake(ws)
        aid_, _ = ticket(ws, tmp_path, {"todos": LIST_A})
    with connect(client) as ws:
        _, snap = handshake(ws)
    assert isinstance(snap.get("todos"), dict), snap.keys()
    assert_todos(snap["todos"][aid_], core(LIST_A))


@pytest.mark.parametrize("bad", [
    {"todos": "pas une liste"},
    {"todos": None},
    {},
    {"todos": ["texte", 42, None]},
])
def test_todowrite_pilote_malforme_sans_crash(client, tmp_path, bad):
    seen = []
    with connect(client) as ws:
        handshake(ws)
        aid_, done = ticket(ws, tmp_path, bad, seen=seen)
        seen += drain(ws)
        # la connexion et l'employé restent utilisables
        _, done2 = ticket(ws, tmp_path, {"todos": LIST_B}, aid_=aid_)
    assert done["ok"] is True, done
    assert done2["ok"] is True, done2
    for e in seen:
        if e.get("type") == "todos":
            assert isinstance(e["todos"], list) and e["todos"] == [], e


def test_todowrite_pilote_elements_invalides_ignores(client, tmp_path):
    seen = []
    mixed = ["texte", LIST_A[0], 42, None, LIST_A[1]]
    with connect(client) as ws:
        handshake(ws)
        aid_, _ = ticket(ws, tmp_path, {"todos": mixed}, seen=seen)
    evs = [e for e in seen if e.get("type") == "todos" and e.get("agent_id") == aid_]
    assert len(evs) == 1, seen
    assert_todos(evs[0]["todos"], core(LIST_A[:2]))


# ---------------------------------------------------------------- employé terminal (observer)


def todo_use(items, tid=None):
    return tool_use(tid or f"toolu_{uuid.uuid4().hex[:8]}", name="TodoWrite", inp={"todos": items})


def joined(cfg, proj, harness):
    sid = new_sid()
    t = transcript_path(cfg, sid, proj)
    append(t, prompt())
    write_session(cfg, 101, sid, proj, status="busy")
    h = harness(cfg, Alive(101))
    h.poll()
    return h, t, aid(sid)


def test_todowrite_terminal_emet_un_evenement_todos(cfg, proj, harness):
    h, t, a = joined(cfg, proj, harness)
    append(t, todo_use(LIST_A))
    evs = of(h.poll(), "todos", a)
    assert len(evs) == 1, h.events
    assert_todos(evs[0]["todos"], core(LIST_A))
    assert of(h.poll(), "todos") == [], "une ligne n'est traitée qu'une fois"


def test_second_todowrite_terminal_remplace_la_liste(cfg, proj, harness):
    h, t, a = joined(cfg, proj, harness)
    append(t, todo_use(LIST_A))
    h.poll()
    append(t, todo_use(LIST_B))
    evs = of(h.poll(), "todos", a)
    assert len(evs) == 1, h.events
    assert_todos(evs[0]["todos"], core(LIST_B))


def test_todowrite_terminal_malforme_sans_crash(cfg, proj, harness):
    h, t, a = joined(cfg, proj, harness)
    append(t,
           tool_use("toolu_x1", name="TodoWrite", inp={"todos": "pas une liste"}),
           tool_use("toolu_x2", name="TodoWrite", inp={}),
           todo_use(["texte", LIST_B[0], 42]))
    evs = of(h.poll(), "todos", a)
    for e in evs:
        assert isinstance(e["todos"], list), e
    assert evs, h.events
    assert_todos(evs[-1]["todos"], core(LIST_B))
    # l'observateur continue de fonctionner
    append(t, todo_use(LIST_A))
    assert_todos(of(h.poll(), "todos", a)[-1]["todos"], core(LIST_A))


def test_snapshot_rejoue_les_todos_du_terminal(client, tmp_path):
    agent = {"id": "o-" + uuid.uuid4().hex[:8], "name": "eter-86", "cwd": str(tmp_path),
             "project": tmp_path.name, "status": "busy", "observed": True}
    client.portal.call(app_mod.hub.emit, {"type": "observed_joined", "agent": agent})
    try:
        client.portal.call(app_mod.hub.emit, {"type": "todos", "agent_id": agent["id"], "todos": core(LIST_A)})
        client.portal.call(app_mod.hub.emit, {"type": "todos", "agent_id": agent["id"], "todos": core(LIST_B)})
        with connect(client) as ws:
            _, snap = handshake(ws)
    finally:
        client.portal.call(app_mod.hub.emit, {"type": "observed_left", "agent_id": agent["id"]})
    assert isinstance(snap.get("todos"), dict), snap.keys()
    assert_todos(snap["todos"][agent["id"]], core(LIST_B))
